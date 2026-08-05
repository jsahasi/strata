"""Take a verified snapshot of the SQLite database, on the same disk it lives on.

WHAT THIS IS NOT. It writes to a directory on the local filesystem, and no
off-host destination is configured anywhere in this repository. A copy on the
same disk as the database survives a bad deploy, a bad migration and a mistaken
DELETE. It does not survive a lost host, a stolen laptop, a wiped volume or a
region going away. That is not disaster recovery and this file will not call it
that. Nothing schedules this script either: there is no cron entry, no timer and
no hosted job in this repository, so no backup exists anywhere until a person
runs it. deploy/site/security.html says the same in the same words.

AND IT IS NOT ENCRYPTED. The copy is the database in the clear: password hashes,
token digests, the audit chain, every filing a customer loaded. What stands
between it and another account on the host is file permissions and nothing else.
The directory is created 0700 and the copy is created 0600 -- created, not
chmod'ed afterwards, because a chmod that follows the write leaves the finished
hashes readable for as long as the copy takes and cannot take back a handle
somebody opened in that time. The mode is then read back off the file rather
than assumed, and a copy that did not come out 0600 is deleted rather than kept.

That is enough on a machine with one operator. It is not enough to call the
file protected, and permissions are not encryption: anyone who can become root,
read the volume, or recover the disk gets everything. A production deployment
needs an encrypted destination. Nothing in this repository provides one, and
this paragraph is not a substitute for one -- it is here so that nobody decides
where to put this file while believing it is protected. All of this was true and
none of it was written down until 2026-08-04, when the permissions were also
actually set.

WHY IT DOES NOT COPY THE FILE. Copying a SQLite database while something is
writing to it can hand you a file that looks perfectly ordinary and is torn --
pages from before a transaction next to pages from during it -- and you find out
on the day you restore. Whether it tears depends on where the copy falls against
the commit, which is to say it depends on luck. So the copy goes through SQLite's
own backup call (sqlite3.Connection.backup), which takes a consistent snapshot of
a live database while the writers carry on. Committed rows come across, an open
transaction does not, and there is no in-between state to land in.

Nor does it shell out to the sqlite3 binary. That binary is not installed
everywhere, and a backup script that fails on a host missing a command-line tool
fails exactly when nobody is watching. The module in the standard library is
already there.

AND IT REFUSES RATHER THAN HANGS. sqlite3.Connection.backup retries a locked
source FOR EVER -- no timeout argument reaches that loop, and the `timeout` on
the connection does not govern it. A writer holding an exclusive lock (any
transaction big enough to spill its pages to disk takes one) therefore stops
this script dead, silently, with no file and no message. A scheduled job that
hangs looks exactly like one that is not scheduled at all, which is the worst of
the failure modes because nobody finds out. So the copy carries a deadline: past
it, the run gives up loudly and exits non-zero. tests/test_backup.py holds the
lock and proves the refusal.

AN UNVERIFIED BACKUP IS A BELIEF, NOT A BACKUP. Every copy is opened afterwards
and put through three checks: PRAGMA integrity_check on the file, a count of the
audit table, and a full re-verification of the audit hash chain per company. The
third is the one that matters. This product's argument rests on a tamper-evident
record, so a backup that silently breaks the chain would be worse than no backup
at all -- it would look like the record and prove nothing. A copy that fails any
of the three is renamed so nobody can mistake it for a backup, and the script
exits non-zero and says why.

AND EVERY WRITE LANDS WHOLE OR NOT AT ALL. Nothing here writes into the file it
is producing. The pages go into a private temporary file in the same directory,
which is flushed and then renamed onto the name with os.replace -- atomic on
POSIX, so every reader sees the old file or the new one and never a half of
either. Until 2026-08-05 restore() wrote straight into the live database, so any
failure partway left a file that was neither the old database nor the new one,
while the refusal the operator read said "Nothing was written". Measured on a
target that did not exist yet: that refusal left a zero-length file behind, and a
zero-length file is a valid empty SQLite database, so the next thing to open it
finds no tables rather than an error. A refusal that lies about the disk is worse
than no refusal, because the person who believes it retries, and the retry is
what loses the original.

Run it:

    .venv/bin/python scripts/backup.py --into /var/backups/strata --keep 7
    .venv/bin/python scripts/backup.py --into /var/backups/strata --dry-run

--into is required and has no default. A default would have put database copies
somewhere inside this working tree, where .gitignore does not cover them and the
repository is published with its history intact -- one `git add -A` and a copy of
the database is public for good. Choosing the destination is the operator's job
and the script will not guess at it.

There is no restore command, on purpose. restore() below is a function, covered
by tests/test_backup.py, and it refuses a copy that does not verify. Putting a
flag on the command that overwrites a live database is a foot-gun. Say plainly
what that leaves: restore() has been run against test files and against a
developer's own database copied to a scratch path. No restore into a running
system has been practised by anyone.
"""

import argparse
import fcntl
import os
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fixed width and UTC, so the names sort chronologically as plain strings and a
# host in another timezone writes names that still interleave correctly with
# these. Retention reads the order off the filename rather than off mtime, which
# a copy or a restore rewrites.
STAMP = "%Y%m%dT%H%M%SZ"
PREFIX = "strata-"
SUFFIX = ".db"

# A failed copy keeps its bytes and loses its name. Anything that looks for
# backups globs PREFIX*SUFFIX and will not see this, which is the point: the one
# file you must never restore is the one that looks like all the others.
UNVERIFIED_SUFFIX = ".UNVERIFIED"

DEFAULT_KEEP = 7

# Reading the finished copy back. Nothing else is writing to it, so this only
# ever covers an unlucky moment.
READ_TIMEOUT_SECONDS = 30.0

# THE TWO NUMBERS BELOW ARE DELIBERATELY DIFFERENT, AND THE SMALL ONE IS LOAD
# BEARING. A connection's `timeout` is how long SQLite waits inside a single
# call before reporting that the database is locked, and sqlite3_backup_step
# runs that wait before it ever calls the progress callback. Set it to the same
# 30s the reader uses and the deadline below cannot bite until 30s have already
# gone, whatever it says. One second here, and the deadline means what it says.
COPY_BUSY_TIMEOUT_SECONDS = 1.0

# How long the copy will wait on a locked database in total before giving up and
# saying so. See the module docstring: the alternative is not "wait a bit
# longer", it is "wait for ever with nothing on the terminal".
BUSY_DEADLINE_SECONDS = 60.0

# The two return codes that mean "somebody else has it", as sqlite3_backup_step
# hands them to a progress callback.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


class BackupError(RuntimeError):
    """A backup that did not happen, or one that cannot be trusted.

    Raised rather than returned. A boolean handed back to a caller who does not
    look at it is how "the backup ran fine" gets believed for six months.
    """


@dataclass(frozen=True)
class Verification:
    """What was actually checked on the copy, and what it said.

    audit_rows is None when the table could not be read at all. That is not a
    count of zero and must never be flattened into one: no table means this is
    not a Strata database, an empty table means it is a new one.
    """

    ok: bool
    integrity: str
    audit_rows: int | None
    chains_verified: int
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackupResult:
    """What one run did. There is no `problems` field on purpose.

    A failure raises, so a result object always describes a run that got where
    it was going. A field that was empty on every result anyone could hold would
    read as "no problems found" while carrying nothing -- the shape of claim
    this product exists to refuse. The problems live on the Verification, and on
    the exception.
    """

    source: Path
    written: Path | None
    verification: Verification | None
    pruned: tuple[Path, ...] = ()
    would_prune: tuple[Path, ...] = ()
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """A verified copy is on disk. False for a dry run, which took none."""
        return self.verification is not None and self.verification.ok


# ---------------------------------------------------------------------------
# Where the database is
# ---------------------------------------------------------------------------


def database_path_from_url(url: str) -> Path:
    """The file behind a SQLAlchemy URL, or a refusal.

    A Postgres URL is refused rather than quietly falling back to the local
    strata.db. That fallback would back up a stale development file every night,
    report success, and leave the real database unprotected -- the failure this
    whole repository is written against.
    """
    if not url:
        raise BackupError("no database URL given")
    scheme, _, rest = url.partition("://")
    if not scheme.split("+")[0] == "sqlite":
        raise BackupError(
            f"this script backs up SQLite files and {url!r} is not one. "
            "Backing up whatever local file happens to exist instead would "
            "report a success that protects nothing."
        )
    if rest in ("", "/", "/:memory:", ":memory:"):
        raise BackupError(
            f"{url!r} is an in-memory database. There is no file to copy, and "
            "writing an empty one would look like a backup for ever."
        )
    # sqlite:///relative.db -> relative.db;  sqlite:////abs/path.db -> /abs/path.db
    return Path(rest[1:] if rest.startswith("/") else rest)


def default_database() -> Path:
    """What the application would open, or the repository's own file."""
    url = os.environ.get("STRATA_DATABASE_URL")
    if url:
        return database_path_from_url(url)
    return ROOT / "strata.db"


# ---------------------------------------------------------------------------
# The copy
# ---------------------------------------------------------------------------


def _uri(path: Path, mode: str) -> str:
    """A file URI sqlite3 will accept, with the path escaped rather than glued."""
    return f"{path.resolve().as_uri()}?mode={mode}"


COPY_MODE = 0o600

# What a write is called before it earns the destination's name. The leading dot
# keeps it out of a plain `ls`, and neither end matches PREFIX*SUFFIX, so
# existing_backups() cannot see one: a staged file must never be globbed as a
# backup, counted towards --keep, or deleted by prune() as an old one.
STAGING_PREFIX = ".strata-incoming-"
STAGING_SUFFIX = ".part"

# The claim beside a staged copy, saying a live run still wants it. It is a
# separate file rather than a lock on the copy itself because SQLite takes its
# own whole-file lock on the database it is writing -- measured, on macOS, where
# a flock held on the staged file makes SQLite wait out its busy timeout and the
# backup fails. See _hold_the_staged_run.
STAGING_LOCK_SUFFIX = ".lock"

# What SQLite can leave beside a database file. os.replace moves one file, so a
# staged copy is only self-contained once these are gone, and cleaning up means
# cleaning up all four names rather than the obvious one.
#
# The same fact cuts the other way and that is easier to miss: the DESTINATION
# owns these names too. A -wal or -journal a dead process left beside the file
# being replaced is not moved by the rename, so it survives the swap and lands
# next to the new database, where SQLite reads it as belonging to whatever holds
# that name. See _take_the_sidecars_away.
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _sidecars_of(path: Path) -> tuple[Path, ...]:
    return tuple(Path(str(path) + suffix) for suffix in SIDECAR_SUFFIXES)


def _lock_for(staged: Path) -> Path:
    return staged.with_name(staged.name[: -len(STAGING_SUFFIX)] + STAGING_LOCK_SUFFIX)


def _staged_names(staged: Path) -> tuple[Path, ...]:
    """Every name one staged run owns: the copy, its sidecars, and its claim.

    The copy comes first and the claim last, so a cleanup that gets part way
    through has dropped the thing worth disk before the thing that says it is
    still wanted, and never the other way round.
    """
    return (staged,) + _sidecars_of(staged) + (_lock_for(staged),)


def _take_the_sidecars_away(path: Path) -> tuple[list[Path], list[Path]]:
    """Remove the journal, log and index beside `path`. Return what went and what did not.

    SQLite decides whether a log belongs to a database by the NAME beside it, not
    by anything inside either file. So a -wal left over from the process that
    died holding the old database is read as belonging to the new one the moment
    a rename puts a new file under that name: the frames are replayed into it and
    the result is a database that is neither the old one nor the new one, that
    PRAGMA integrity_check calls "ok", after a call that returned normally.

    Writing straight into the destination never had this problem, which is why
    staging had to be given it back on purpose: the in-place write went through a
    connection on the destination, so SQLite recovered and checkpointed the log
    itself before the first page landed.

    Nothing is raised from here, and BOTH lists come back. What could not go
    decides whether the caller refuses; what did go decides what the refusal is
    allowed to say. Removing a journal is itself a write -- a database whose hot
    journal has gone cannot roll back the transaction it died inside -- so a
    refusal that took one away and then said "Nothing was written" would be the
    same false sentence this module exists to stop, one size smaller.
    """
    gone: list[Path] = []
    left: list[Path] = []
    for sidecar in _sidecars_of(path):
        if not os.path.lexists(sidecar):
            continue
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            left.append(sidecar)
        else:
            gone.append(sidecar)
    return gone, left


def _remove_staged(staged: Path) -> list[Path]:
    """Take the copy, its sidecars and its claim away. Return what would not go.

    Nothing is raised from here. This runs while another exception is already on
    its way up, and a cleanup that throws replaces the reason the operator needs
    with the reason the cleanup failed. What it could not remove is handed back
    instead, so the caller can say it out loud rather than let a partial file sit
    on the disk under a message claiming nothing was written.
    """
    left: list[Path] = []
    for path in _staged_names(staged):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Only what is really still there. Naming a file that is not on the
            # disk sends an operator looking for something they cannot find, and
            # teaches them to discount the next message that names one.
            if os.path.lexists(path):
                left.append(path)
    return left


def _staged_for(lock: Path) -> Path:
    """The copy a lock file speaks for. Derived from the name, not from a registry.

    The two share the random middle that mkstemp chose, so anything that finds
    one can find the other without this module remembering anything across a
    kill.
    """
    return lock.with_name(lock.name[: -len(STAGING_LOCK_SUFFIX)] + STAGING_SUFFIX)


def _hold_the_staged_run(directory: Path) -> tuple[int, Path, Path]:
    """Claim a lock file first, then the copy it speaks for. Return both and the fd.

    WHY THERE IS A CLAIM AT ALL. A staged file is invisible to everything else in
    this module on purpose: it matches neither PREFIX*SUFFIX nor anything prune()
    looks at, so no retention rule can remove one. That is right while a run is
    using it and wrong the moment the run dies -- SIGKILL, the OOM killer, a
    power cut -- because what is left is a full-size partial database that
    nothing will ever reap, under a dotted name that does not show in a plain ls.
    They collect on the backup volume until it is full and every backup after
    that fails, and after a killed restore one sits in the live database's own
    directory.

    WHY IT IS A SEPARATE FILE. An advisory whole-file lock is the only thing that
    tells a copy in flight from a corpse -- it is a fact about the live processes
    on this host rather than a threshold on a timestamp, which would eat a slow
    copy running beside this one. It cannot be taken on the staged database
    itself: SQLite takes its own whole-file lock on the file it is writing.
    Measured on macOS, where an flock held on the staged file makes SQLite wait
    out its whole busy timeout and the copy fails outright.

    WHY THE LOCK FILE IS CREATED FIRST. It means a staged copy always has its
    claim already beside it, so a reaper that finds a copy with no claim knows it
    is looking at the dead, not at a run half a millisecond old. mkstemp reserves
    the middle, so the copy's own name cannot collide either.

    A filesystem that will not take the lock is not an error. The write does not
    depend on it, and _reap_orphaned_staging removes nothing it cannot lock, so
    there nobody locks and nobody reaps -- never the case where a live copy is
    deleted out from under the run writing it.
    """
    fd, name = tempfile.mkstemp(
        dir=directory, prefix=STAGING_PREFIX, suffix=STAGING_LOCK_SUFFIX
    )
    lock = Path(name)
    staged = _staged_for(lock)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass
        # O_EXCL for the same reason mkstemp uses it: the file must never have
        # existed under another process's hand. 0600 is the mode it is created
        # with, so the password hashes never land in a file that was briefly
        # wider; fchmod after it because the umask can only clear bits and a
        # umask of 0400 would otherwise leave this at 0200.
        staged_fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, COPY_MODE)
        try:
            os.fchmod(staged_fd, COPY_MODE)
        finally:
            os.close(staged_fd)
    except BaseException:
        # Nothing half-claimed is left in the directory. The descriptor goes too:
        # leaking it would hold a lock on a file that is no longer there and no
        # later run could ever explain the leak.
        os.close(fd)
        _remove_staged(staged)
        raise
    return fd, lock, staged


def _reap_orphaned_staging(directory: Path) -> None:
    """Remove staged copies no live run claims. Never touch one that is claimed.

    Two kinds go. A claim nobody holds, with whatever copy it speaks for; and a
    copy with no claim beside it at all, which is either a run killed in the gap
    between the two or a leftover from a version of this file that staged without
    claiming.

    The conservative direction is the only safe one: a claim that cannot be
    locked counts as alive and is left exactly where it is, whether another run
    really holds it or the filesystem does not support the lock. Deleting a copy
    in flight would break a run doing nothing wrong, and this is housekeeping --
    it earns no right to do that.

    Nothing is raised. A failure to reap costs disk; a failure to back up costs
    the database.
    """
    try:
        locks = sorted(directory.glob(f"{STAGING_PREFIX}*{STAGING_LOCK_SUFFIX}"))
        copies = sorted(directory.glob(f"{STAGING_PREFIX}*{STAGING_SUFFIX}"))
    except OSError:
        return

    claimed: set[Path] = set()
    for lock in locks:
        try:
            fd = os.open(lock, os.O_RDONLY)
        except OSError:
            claimed.add(_staged_for(lock))
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            claimed.add(_staged_for(lock))
            continue
        finally:
            os.close(fd)
        _remove_staged(_staged_for(lock))

    for copy in copies:
        if copy in claimed:
            continue
        _remove_staged(copy)


def _fsync_file(path: Path) -> None:
    """Push the bytes to the disk before the name changes.

    Without this the rename can reach the platter first, and a machine that
    loses power in that window comes back with the destination's name pointing
    at a file whose contents were never written. os.fsync is the portable ask;
    on macOS F_FULLFSYNC is stronger and is not exposed here, so what this
    guarantees on that platform is that the write left the OS, not that the
    drive flushed its own cache.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Make the rename itself durable, and do not raise if the filesystem refuses.

    This runs after os.replace has already succeeded. The new file is in place
    and every process on the host can see it, so there is nothing left to undo
    and nothing untrue to report; what an unsynced directory risks is losing the
    rename to a power cut in the next moment. Raising here would report a failed
    restore that in fact happened -- the same class of false sentence this whole
    change exists to remove -- so the failure is swallowed rather than dressed up.
    Some filesystems do not allow opening a directory for fsync at all.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _what_the_destination_lost(destination: Path, gone: list[Path]) -> str:
    """The true half of "Nothing was written" for a refusal that reached the swap.

    By the time a refusal can fire between clearing the destination's sidecars
    and renaming over it, a log may already have gone. The destination file
    itself is still untouched, and saying only that would be true; saying
    "Nothing was written" would not, because a database whose hot journal has
    been removed cannot roll back the transaction it died inside. So the sentence
    is assembled from what actually happened rather than asserted.
    """
    if not gone:
        return "Nothing was written."
    return (
        f"{destination} itself is untouched, but "
        + ", ".join(p.name for p in gone)
        + " had already been taken from beside it. A database that died inside a "
        "transaction cannot roll it back without its journal, so check that file "
        "before trusting it."
    )


@contextmanager
def _staged_beside(destination: Path) -> Iterator[Path]:
    """Yield a private file to write into, then move it onto `destination`.

    NOTHING WRITES INTO THE FILE IT IS PRODUCING. A write that lands in the
    destination has to survive every step to leave a file worth having, and the
    steps it cannot survive -- a full volume, a lock that never clears, a
    machine that goes away, ctrl-c -- are exactly the ones that happen on the
    day somebody is restoring a database. The pages go into a temporary file
    first and take the destination's name in one operation at the end, so a
    reader sees the old file or the new one and never a half of either.

    THE SAME DIRECTORY, WHICH IS NOT A DETAIL. os.replace is atomic only within
    one filesystem. A temporary in /tmp or in tempfile's default directory is on
    another mount often enough to matter, and the fallback there is a
    byte-by-byte move -- which is the in-place write again, wearing a different
    name and now with no verification behind it.

    0600 BEFORE THE FIRST BYTE, NOT AFTER THE LAST. Between the first page
    landing and a chmod returning, a complete set of scrypt password hashes sits
    on a shared host at whatever the umask gave -- 0644 under the usual 022 --
    and closing that window does not close a handle another process opened while
    it was open. _hold_the_staged_run creates the copy with O_CREAT|O_EXCL at
    0600, so the file has never existed with any other permissions. The fchmod
    after it is not redundant: the mode passed to open is masked by the umask,
    which can only clear bits, so a legal umask of 0400 would leave the file at
    0200 and a later read of our own copy would fail for a reason nobody would
    guess. fchmod sets the bits exactly, on the descriptor rather than the path,
    so nothing can be swapped underneath it.

    The caller may widen the mode before the block ends -- restore() does, to
    keep the mode the file it replaces already had -- and the pages still spend
    the whole copy at 0600.

    THE DESTINATION'S OWN SIDECARS GO BEFORE THE RENAME, NOT AFTER. Two
    operations cannot be made one, so the order is chosen so the window fails the
    safe way. Removing the old log first and then renaming means a machine that
    dies in between has an unreplaced database that lost its log -- visible, and
    repaired by running the restore that was already in progress. Renaming first
    and removing after means a machine that dies in between has the NEW database
    with the OLD log beside it, which is silent, passes every check, and is in no
    backup. Loud damage beats quiet damage.
    """
    _reap_orphaned_staging(destination.parent)
    try:
        fd, lock, staged = _hold_the_staged_run(destination.parent)
    except OSError as exc:
        # Naming the temporary file here would name something the operator has
        # never seen, chosen at random, and gone by the time they read it. What
        # they can act on is the directory they asked this to write into.
        raise BackupError(
            f"nothing could be written next to {destination}: no private copy can "
            f"be created in {destination.parent} ({exc.strerror}). The pages have "
            "to land in that directory, because a rename is atomic only within one "
            "filesystem. Nothing was written."
        ) from exc

    try:
        yield staged

        # A rename moves one file. Anything SQLite left in a sidecar would be
        # left behind, so a database installed with its write-ahead log stranded
        # is a database missing its most recent pages, reported as a success.
        # Closing the connection checkpoints and removes the log today, which is
        # the description of every control nobody tested, so it is checked.
        stranded = [p for p in _sidecars_of(staged) if p.exists()]
        if stranded:
            raise BackupError(
                "the copy left "
                + ", ".join(p.name[len(staged.name):] for p in stranded)
                + f" beside it, so renaming it onto {destination} would install a "
                "database without the pages in them. Nothing was written."
            )

        _fsync_file(staged)

        # The other end of the same fact. What is beside the DESTINATION is not
        # moved by the rename either, and it does not belong to the file about to
        # arrive.
        gone, clinging = _take_the_sidecars_away(destination)
        if clinging:
            raise BackupError(
                f"{destination} has " + ", ".join(p.name for p in clinging)
                + " beside it and they cannot be removed. SQLite reads those as "
                "belonging to whatever holds that name, so renaming a database "
                "into place would have a dead process's pages replayed into it, "
                "and the result would pass every integrity check there is. "
                + _what_the_destination_lost(destination, gone)
                + " Remove them by hand and run this again."
            )
        try:
            os.replace(staged, destination)
        except OSError as exc:
            raise BackupError(
                f"the copy was complete but could not take the name {destination} "
                f"({exc.strerror}). " + _what_the_destination_lost(destination, gone)
            ) from exc
    except BaseException as exc:
        # BaseException, not Exception. Ctrl-c is the interruption an operator
        # produces on purpose, at the moment they realise they typed the wrong
        # path, and an `except Exception` reads as thorough while missing it.
        left = _remove_staged(staged)
        # Every name that would not go, not the first of them. A message naming
        # one of three leaves two on the disk that nobody has been told about,
        # which is the same false sentence one size smaller.
        names = ", ".join(str(p) for p in left)
        if left and isinstance(exc, Exception):
            # The leading clause overrides the quoted one on purpose. The failure
            # below is very likely to end with the sentence "Nothing was
            # written", which was true of the destination and is not true of the
            # disk once the cleanup has also failed. Quoting it without saying so
            # would rebuild the defect this staging exists to remove.
            raise BackupError(
                f"the write failed AND this run's own files could not be removed: "
                f"{names}. Whatever the reason below says about nothing being "
                "written, those are on the disk, and the copy among them is not a "
                f"database: delete them by hand. The reason was: {exc}"
            ) from exc
        if left:
            # Ctrl-c, and anything else that is not an Exception. Raising a
            # BackupError in its place would turn a deliberate interrupt into an
            # ordinary failure and take the operator's own signal away from every
            # caller that tells the two apart. So the leftover is added TO the
            # exception rather than instead of it, and it reaches the operator in
            # the traceback the interrupt already prints. Until 2026-08-05 this
            # was the one cleanup path that left a file and named nothing.
            exc.add_note(
                f"strata backup: this run's own files could not be removed: "
                f"{names}. They are on the disk, and the copy among them is not a "
                "database: delete them by hand."
            )
        raise
    finally:
        # The claim goes last and unconditionally. It is dropped after the copy
        # it speaks for has either taken the destination's name or been removed,
        # so no window exists in which a reaper in another process sees an
        # unclaimed copy this one is still using.
        os.close(fd)
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    _fsync_directory(destination.parent)

    # Between the removal above and the rename, only a process holding the
    # destination open could put a sidecar back -- the case the lock guard
    # concedes it cannot always see. Saying nothing here would leave exactly the
    # silent corruption this whole helper exists to stop, so it is swept again
    # and, if the file will not go, said out loud. This runs after the swap, so
    # the message says the database IS installed: reporting a failed write that
    # in fact happened is the same class of false sentence.
    _, still_there = _take_the_sidecars_away(destination)
    if still_there:
        raise BackupError(
            f"{destination} was written, and then " + ", ".join(
                p.name for p in still_there
            )
            + " appeared beside it and cannot be removed. Something has that "
            "database open. SQLite will read those files as belonging to the "
            "restored one and replay them into it. The restore DID happen: stop "
            "whatever holds the database, remove those files by hand, and check "
            "the result before trusting it."
        )


def _copy(src: sqlite3.Connection, dst: sqlite3.Connection, deadline_seconds: float) -> None:
    """One consistent snapshot, or a refusal once the lock has held long enough.

    The copy runs in a single step, so the whole file is read under one lock and
    the result is one point in time rather than a stitch of several.

    The progress callback is the only place a deadline can be enforced: it is
    called on every retry, and an exception raised from it is the one thing that
    breaks the library's endless busy loop. Only time spent LOCKED counts, so a
    large database that is simply slow to copy is never cut off.
    """
    started_waiting: float | None = None

    def watch(status: int, remaining: int, total: int) -> None:
        nonlocal started_waiting
        if status not in (_SQLITE_BUSY, _SQLITE_LOCKED):
            started_waiting = None
            return
        now = time.monotonic()
        if started_waiting is None:
            started_waiting = now
        elif now - started_waiting > deadline_seconds:
            raise BackupError(
                f"the database stayed locked by another connection for "
                f"{deadline_seconds:.0f}s, so no snapshot could be taken. "
                "Nothing was written. Retry when the writer has finished, or "
                "raise --busy-seconds if a long transaction is expected."
            )

    src.backup(dst, progress=watch)


def snapshot(
    source: Path,
    destination: Path,
    *,
    deadline_seconds: float = BUSY_DEADLINE_SECONDS,
) -> None:
    """Copy a live database consistently, through SQLite's own backup call.

    mode=rw, never mode=rwc. A typo in the source path would otherwise CREATE an
    empty database, copy it, and hand back a backup that passes every integrity
    check ever written. mode=ro is not used either: a WAL database refuses a
    read-only open when it cannot build its shared-memory index, and a backup
    that fails on WAL hosts is a backup nobody has.

    The pages go into a private temporary file beside the destination and take
    its name at the end. See _staged_beside for why, and for why the 0600 is on
    the file before the first byte rather than after the last.

    THE DESTINATION NAME ONLY EVER APPEARS FINISHED. The earlier version wrote
    into the destination and unlinked it on failure, which covered the file this
    process left behind and not the window: for as long as the copy ran, a
    half-written file sat under a name matching PREFIX*SUFFIX, where
    existing_backups() globs it, prune() counts it towards --keep, and anybody
    reading the directory takes it for a backup. Staging closes the window
    rather than cleaning up after it, and it stops a failed copy deleting a
    destination this script did not create.
    """
    if not source.exists():
        raise BackupError(f"no database at {source}; nothing was copied and nothing created")
    if not source.is_file():
        raise BackupError(f"{source} is not a file")

    src = sqlite3.connect(_uri(source, "rw"), uri=True, timeout=COPY_BUSY_TIMEOUT_SECONDS)
    try:
        with _staged_beside(destination) as staged:
            dst = sqlite3.connect(staged, timeout=COPY_BUSY_TIMEOUT_SECONDS)
            try:
                _copy(src, dst, deadline_seconds)
            finally:
                dst.close()
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"could not copy {source} to {destination}: {exc}") from exc
    finally:
        src.close()


# ---------------------------------------------------------------------------
# The verification
# ---------------------------------------------------------------------------


def _verify_chains(path: Path) -> tuple[int, list[str], list[str]]:
    """Re-verify every company's audit chain on the copy. The check that matters.

    The application's own verify_chain runs here, not a reimplementation of it,
    so this cannot drift from the thing it is checking.

    The import sits inside the function so that the copy above never depends on
    the application tree being importable -- you want a backup most on the day a
    deploy went wrong. When the import does fail, this reports a problem and the
    backup is marked unverified. It never reports a pass it could not establish.
    """
    problems: list[str] = []
    notes: list[str] = []
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from app.state.audit import verify_chain
        from app.state.models import AuditEvent
    except Exception as exc:  # pragma: no cover - needs a broken tree to reach
        return 0, [f"cannot verify the audit chain: {exc}"], notes

    # THE PATH IS ESCAPED, NOT GLUED, which is the entire reason _uri exists one
    # screen above -- and this line ignored it until 2026-08-04. It read
    # create_engine(f"sqlite:///{path}"). SQLAlchemy reads everything after a "?"
    # as a query string, so a backup directory with one in the name was truncated
    # and this opened a DIFFERENT file. Measured, not guessed: with a database at
    # "we ird?x.db" the glued form opened "we ird", reported "no such table:
    # audit_events", and a backup that was entirely sound got quarantined on the
    # strength of a chain check that had never looked at it. Choosing the
    # directory is the operator's job and "?" is a legal character in it.
    #
    # AND IT CREATED THE FILE IT INVENTED, which is the half worth naming
    # separately, because the obvious fix does not cover it. Passing the path
    # through sqlalchemy.engine.URL.create() escapes it correctly and stops the
    # truncation -- that was tried here first -- but URL.create renders a plain
    # sqlite:/// URL, and a plain sqlite:/// URL opens rwc. Point it at a path
    # that is not there and it makes an empty database, finds no audit_events in
    # it, and reports that as a problem with your backup. Same wrong answer,
    # arrived at down the other road, plus a stray file nothing cleans up. Going
    # through _uri with an explicit mode closes both: the escaping stops the
    # truncation and mode=rw refuses to create.
    #
    # WHY rw AND NOT ro, given this only ever reads. Two reasons, and the second
    # is the one that decides it. First, verify_backup opens the very same file
    # rw twenty lines below for the integrity check, and two halves of one
    # verification disagreeing about how to open a file is how a check comes to
    # pass on one path and fail on the other. Second, a copy can be a WAL
    # database, and a read-only open of one has to build a shared-memory index it
    # is not always allowed to build -- the same trap snapshot() already records
    # for the source, and a chain check that fails on WAL hosts is a chain check
    # nobody keeps. mode=rw already buys the property this bug was about: it will
    # not create. ro would only add protection against this function issuing a
    # write, and it issues none.
    engine = create_engine("sqlite:///" + _uri(path, "rw") + "&uri=true", future=True)
    verified = 0
    try:
        with Session(engine, future=True) as session:
            companies = (
                session.execute(
                    select(AuditEvent.company_id)
                    .distinct()
                    .order_by(AuditEvent.company_id)
                )
                .scalars()
                .all()
            )
            if not companies:
                notes.append(
                    "no audit rows in the copy, so no chain was verified: this "
                    "backup carries no evidence that the chain is sound"
                )
            for company in companies:
                try:
                    # Anything but True is a refusal to certify, not a pass. It
                    # raises today; a future version that returns False must not
                    # be read here as a chain that verified.
                    if verify_chain(session, company) is not True:
                        problems.append(f"{company}: verify_chain did not confirm the chain")
                    else:
                        verified += 1
                except Exception as exc:
                    problems.append(f"audit chain broken: {exc}")
    except Exception as exc:
        problems.append(f"could not read the audit log in the copy: {exc}")
    finally:
        engine.dispose()
    return verified, problems, notes


def verify_backup(path: Path) -> Verification:
    """Open the copy and put it through the three checks. Never guesses a pass."""
    path = Path(path)
    if not path.exists():
        raise BackupError(f"no backup at {path}")

    problems: list[str] = []
    notes: list[str] = []
    integrity = "not read"
    rows: int | None = None

    conn = sqlite3.connect(_uri(path, "rw"), uri=True, timeout=READ_TIMEOUT_SECONDS)
    try:
        try:
            reported = conn.execute("PRAGMA integrity_check").fetchall()
            integrity = (
                "ok"
                if reported == [("ok",)]
                else "; ".join(str(row[0]) for row in reported)
            )
        except sqlite3.DatabaseError as exc:
            # A file damaged badly enough cannot even be asked whether it is
            # damaged. That is a failure, not an absence of one.
            integrity = f"unreadable: {exc}"

        if integrity != "ok":
            problems.append(f"integrity_check: {integrity}")
        else:
            try:
                rows = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                problems.append(
                    f"cannot count audit_events in the copy ({exc}); this does "
                    "not look like a Strata database"
                )
    finally:
        conn.close()

    verified = 0
    if rows is not None:
        verified, chain_problems, chain_notes = _verify_chains(path)
        problems.extend(chain_problems)
        notes.extend(chain_notes)

    return Verification(
        ok=not problems,
        integrity=integrity,
        audit_rows=rows,
        chains_verified=verified,
        problems=tuple(problems),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def existing_backups(directory: Path) -> list[Path]:
    """Verified backups, oldest first. Name order is time order by construction."""
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{PREFIX}*{SUFFIX}"))


def prune(directory: Path, keep: int, *, dry_run: bool = False) -> tuple[Path, ...]:
    """Delete all but the newest `keep` backups. Returns what went, or would go.

    Only files matching the verified naming pattern are candidates. A copy that
    failed verification has been renamed out of that pattern and is never
    deleted here: destroying the evidence of a bad backup to make room for
    another is exactly backwards.
    """
    if keep < 1:
        raise BackupError("keep must be at least 1; keeping none deletes the backup just taken")
    doomed = tuple(existing_backups(directory)[:-keep])
    if not dry_run:
        for old in doomed:
            old.unlink()
    return doomed


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _refuse_a_collision(planned: Path) -> None:
    """Never write over a backup. Both names are checked, not only the good one.

    A quarantined copy carries the planned name with a suffix on the end, so
    checking only the plain name would let a second run at the same second
    overwrite the evidence of the first one's failure.
    """
    for path in (planned, planned.with_name(planned.name + UNVERIFIED_SUFFIX)):
        if path.exists():
            raise BackupError(
                f"{path.name} already exists; refusing to overwrite it. Backups "
                "are stamped to the second, so this is a second run inside one "
                "second."
            )


def _whose_fault(source: Path, quarantined: Path, verification: Verification) -> str:
    """Say which of the two files is the broken one, having actually looked.

    THE BUG THIS EXISTS FOR. run() used to verify the copy and nothing else, and
    report every failure as "the copy at ... did not verify". But the copy is a
    faithful reproduction of the source, so the commonest way for it to fail the
    chain check is that the SOURCE already failed it -- somebody edited a row in
    the live database, and this script dutifully copied the edit across. The
    operator was then handed a message blaming the one file in the story that had
    behaved perfectly, and pointed at a backup directory while the tampered
    database carried on serving traffic. That is not a small wording problem. It
    is the difference between "your disk is flaky" and "your audit log has been
    edited", and the script knew enough to tell them apart and did not look.

    WHY THE SOURCE IS CHECKED HERE AND NOT BEFORE THE COPY. Verifying the source
    up front would answer the same question, and it would cost a full extra pass
    over the whole database on every run, including the overwhelming majority
    that are fine. The chain check is the expensive one -- it rehashes every
    audit row for every company. Doing it here costs nothing on a good day and a
    second pass on a bad one, when a few seconds of extra work is the cheapest
    thing on offer and the diagnosis is worth far more.

    Nothing is missed by waiting. A tampered source cannot produce a clean copy:
    the backup carries the committed rows across exactly, so a broken chain in
    the source is a broken chain in the copy, and the copy always fails first.
    The only rows the copy leaves behind are uncommitted ones, which are not yet
    part of the record and may still roll back.

    Taking the copy first also keeps the quarantined file. Refusing before the
    copy exists would leave the operator with a message and no artefact; this way
    the evidence is on disk under a name nothing will restore.

    ABSENCE IS DENIAL applies to this function too. If the source cannot be
    checked at all, that is not a source that passed, and the message says the
    fault is unknown rather than picking the likelier of the two.
    """
    try:
        source_verification = verify_backup(source)
    except Exception as exc:
        return (
            f"the copy at {quarantined} did not verify, and the live database at "
            f"{source} could not be checked either ({exc}), so which of the two "
            "is at fault is not known. The copy has been renamed out of the "
            "backup naming pattern and kept. Do not restore it, and do not treat "
            "the live database as sound until it has been checked by hand."
        )

    if not source_verification.ok:
        # The source's own problems are spelled out only when they differ from
        # the copy's. In the ordinary case they are the same list -- the copy
        # reproduced the source, so it failed the same way -- and printing it
        # twice in one paragraph trains the reader to skim the paragraph. Where
        # they do differ, that difference is the most informative thing on the
        # screen and it gets said.
        extra = ""
        if source_verification.problems != verification.problems:
            extra = (
                " On the source specifically: "
                + " | ".join(source_verification.problems)
                + "."
            )
        return (
            f"the live database at {source} does not verify, so the fault is "
            f"there and not in the backup. The copy at {quarantined} reproduced "
            "it faithfully, which is why it failed the same checks. Taking "
            "another backup will not help and will produce another bad copy. The "
            "record itself is what needs looking at, not the backup directory. "
            "The copy has been renamed out of the backup naming pattern and kept "
            f"as evidence.{extra}"
        )

    return (
        f"the copy at {quarantined} did not verify, and the live database at "
        f"{source} does verify, so the fault is in the copy and not in the "
        "source. Something went wrong between reading the database and writing "
        "the file -- a failing disk, a full volume, a filesystem that lied about "
        "a write. The copy has been renamed out of the backup naming pattern and "
        "kept. The live database is sound; retry the backup, and if it fails "
        "again look at the destination volume rather than at the database."
    )


def run(
    source: Path,
    into: Path,
    *,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    now: datetime | None = None,
    busy_seconds: float = BUSY_DEADLINE_SECONDS,
) -> BackupResult:
    """Take one backup, verify it, prune the old ones. Raises if any of that fails."""
    source = Path(source)
    into = Path(into)
    if keep < 1:
        raise BackupError("keep must be at least 1; keeping none deletes the backup just taken")
    if not source.exists():
        raise BackupError(f"no database at {source}; nothing was copied and nothing created")

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(STAMP)
    planned = into / f"{PREFIX}{stamp}{SUFFIX}"

    if dry_run:
        # Nothing is created, not even the destination directory. A dry run that
        # leaves something behind is not one.
        #
        # It also has to predict the refusals, not just the happy path. A dry run
        # that reports "this would be fine" where the real run would stop is the
        # kind of quiet lie this repository is written against -- and this one
        # fires whenever a dry run lands in the same second as a real backup.
        _refuse_a_collision(planned)
        candidates = sorted(existing_backups(into) + [planned])
        would_go = tuple(p for p in candidates[:-keep] if p != planned)
        return BackupResult(
            source=source,
            written=None,
            verification=None,
            would_prune=would_go,
            dry_run=True,
        )

    try:
        # 0o700, not the umask. This file IS the database: every scrypt password
        # hash, every session row, every invitation and share token digest, and
        # the whole audit chain. Until 2026-08-04 the directory and the copy took
        # whatever the process umask gave them -- typically 0755 and 0644 -- and
        # the docstring told the operator to put it in /var/backups while
        # conceding only that the copy is local, never that it was readable by
        # every account on the host. On the shared droplet this deploys to
        # (decisions.html ADR-10) that is every neighbour. mkdir's mode applies
        # only when it creates the directory, so an existing one is tightened
        # below rather than left as found.
        into.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(into, 0o700)
    except OSError as exc:
        # An operator pointing --into at an existing file, or at a directory
        # they cannot write. Either way it is a failed backup and must leave by
        # the same door as every other one, not as a traceback.
        raise BackupError(f"cannot use {into} as a backup directory: {exc}") from exc
    _refuse_a_collision(planned)

    # The live database's own directory, which nothing else here would ever
    # sweep. A restore stages beside the database it is replacing, so a restore
    # killed part way leaves a full copy of the database there -- and a restore
    # runs about never, while this runs nightly. Only this module's own dead are
    # touched, and only ones no live run holds; see _reap_orphaned_staging. It
    # sits below the dry-run return on purpose: a dry run deletes nothing.
    _reap_orphaned_staging(source.parent)

    snapshot(source, planned, deadline_seconds=busy_seconds)

    # THE MODE IS CHECKED, NOT ASSUMED. snapshot() created the file 0600 before a
    # byte went into it, so this should never fire -- but "should never fire" is
    # the description of every control nobody tested. os.chmod and os.open both
    # return success on filesystems that do not carry Unix modes at all: a CIFS
    # or exFAT mount, or a container bind-mount with a fixed fmask, takes the
    # request, reports no error, and leaves the file readable by the world. An
    # earlier draft of this fix simply called os.chmod here and trusted the
    # return code, which would have reported a protected backup on exactly the
    # hosts where it was not one. Reading the mode back is the only way to know.
    actual = stat.S_IMODE(planned.stat().st_mode)
    if actual != COPY_MODE:
        # A copy nobody can restrict is not one to keep quietly. It is removed
        # rather than left readable, for the reason snapshot() removes a
        # half-written one: the dangerous file is the one that looks ordinary.
        planned.unlink(missing_ok=True)
        raise BackupError(
            f"copied {source} but {planned} came out mode {oct(actual)} rather "
            f"than {oct(COPY_MODE)}, so every account on this host can read its "
            "password hashes and its audit chain. The copy was deleted rather "
            "than left readable. This filesystem does not appear to carry Unix "
            "permissions; back up to one that does, or encrypt the destination."
        )
    verification = verify_backup(planned)

    if not verification.ok:
        quarantined = planned.with_name(planned.name + UNVERIFIED_SUFFIX)
        planned.rename(quarantined)
        raise BackupError(
            f"{_whose_fault(source, quarantined, verification)} Nothing was "
            "pruned. Problems: " + " | ".join(verification.problems)
        )

    pruned = prune(into, keep)
    return BackupResult(
        source=source,
        written=planned,
        verification=verification,
        pruned=pruned,
    )


def _refuse_a_target_somebody_holds(target: Path) -> None:
    """Refuse a database another connection is holding, and say so by name.

    WHY THIS EXISTS AT ALL. Until the write was staged, this refusal happened by
    accident: SQLite reported the destination busy, and the deadline in _copy
    turned that into a message about a lock. A rename does not care what has the
    file open, so writing atomically would have quietly removed the only thing
    standing between an operator and pulling a database out from under a running
    server -- and the result is worse than the old corruption, because it is
    silent. The server keeps its handle on a file that no longer has a name: it
    serves the pre-restore data for ever, its writes go where nobody will look
    for them, and neither side reports a problem. So the guard is now deliberate,
    it runs before the copy and again at the swap, and it names the file.

    WHAT IT CANNOT DO. An idle connection holds no lock. This catches a process
    writing to the target now, not one that has it open and quiet, and no call
    inside this process can close that gap. restore()'s docstring says the same,
    and that limit is the honest reason there is still no command for this.

    A target that cannot answer at all is not a holder. A file too damaged to
    open is the case a restore exists for, and refusing it here would lock the
    door on the one morning somebody needs to open it.
    """
    if not target.exists():
        return
    try:
        conn = sqlite3.connect(_uri(target, "rw"), uri=True, timeout=0)
    except sqlite3.Error:
        return
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        wording = str(exc).lower()
        if "lock" in wording or "busy" in wording:
            raise BackupError(
                f"{target} is open in another process and locked right now. "
                "Restoring over it would leave that process reading a file with "
                "no name and writing where nobody will look for it, and nothing "
                "on either side would report a problem. Nothing was written. "
                "Stop whatever has the database open and run this again."
            ) from exc
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()


def _match_the_file_being_replaced(staged: Path, standing: os.stat_result, target: Path) -> None:
    """Give the staged copy the mode and owner of the file it is about to replace.

    A rename carries the staged file's mode and this process's ownership, so
    writing a new file and moving it on loses both for free. The old code opened
    the existing file and left them alone on purpose: the mode and owner of a
    live database were set by whoever deployed it, deploy/Dockerfile runs the
    application as the `strata` system user, and narrowing that file to 0600
    root:root under an operator running as root hands them an outage in place of
    a data loss.

    The mode is widened here rather than at creation, so the pages spend the
    whole copy at 0600 whatever the target turns out to be.

    Ownership that cannot be reproduced is a refusal, not a warning. A restore
    that lands a database the service cannot open has traded one outage for
    another and called it a success. This is a narrower rule than the old code
    had -- writing in place never changed an owner -- and it only fires where the
    operator is not the owner, which is the case it is about.
    """
    os.chmod(staged, stat.S_IMODE(standing.st_mode))
    mine = staged.stat()
    if (mine.st_uid, mine.st_gid) == (standing.st_uid, standing.st_gid):
        return
    try:
        os.chown(staged, standing.st_uid, standing.st_gid)
    except OSError as exc:
        raise BackupError(
            f"{target} is owned by uid {standing.st_uid} gid {standing.st_gid}, and "
            f"this process cannot write a replacement owned by them ({exc}). The "
            f"restored file would be owned by uid {mine.st_uid}, and the service "
            "that opens the database would lose it. Nothing was written and the "
            "file on disk is untouched. Run this as its owner, or as root."
        ) from exc


def restore(
    backup_path: Path,
    target: Path,
    *,
    deadline_seconds: float = BUSY_DEADLINE_SECONDS,
) -> Verification:
    """Write a verified backup out to `target`, replacing whatever is there.

    Verified FIRST, and refused if it does not pass. Restoring a copy nobody
    checked is how a bad day becomes an unrecoverable one, and the check is
    cheap next to the alternative.

    IT LANDS WHOLE OR NOT AT ALL. The pages go into a private temporary file in
    the target's own directory and take the target's name in one os.replace at
    the end, so every failure before that point leaves the file that is there
    exactly as it was -- byte for byte, mode and owner included -- and every
    failure after it is impossible. Until 2026-08-05 this wrote straight into the
    target, and the refusal it printed said "Nothing was written" while a partial
    file sat under the name. The whole product rests on a refusal naming a true
    reason; that one named a false one.

    THE TARGET'S OWN LOG GOES WITH IT. A rename moves one file, so a -wal or
    -journal a dying process left beside the target is not carried away by the
    swap: it stays, and SQLite reads it as belonging to whatever now holds that
    name. The restored database would have the OLD database's frames replayed
    into it, integrity_check would call the result ok, and this function would
    return normally. Writing into the target hid this, because the write went
    through a connection on the target and SQLite recovered the log first. The
    log is now cleared on purpose, immediately before the rename -- see
    _staged_beside for why that order and not the other one.

    The target must not be open in another process. A target another connection
    holds a lock on is refused by name, but an idle connection holds no lock and
    nothing in this process can see one, so the guard is not a proof. That is the
    honest reason there is still no command for this.
    """
    backup_path = Path(backup_path)
    target = Path(target)
    # A SYMLINKED DATABASE PATH IS A DEPLOYMENT SHAPE, NOT AN ODDITY:
    # /var/lib/strata/strata.db pointing at a mounted volume is the ordinary way
    # to put the data somewhere other than where the package expects it. The
    # write this replaces went through the link to the far end. os.replace would
    # replace the LINK -- leaving the real database untouched, a new file where
    # the link was, and an operator who has restored nothing and been told it
    # worked. So the link is followed here, and everything below works on the
    # file the link means. The staged copy then lands on the far end's own
    # filesystem too, which is where the rename has to be atomic.
    if target.is_symlink():
        target = Path(os.path.realpath(target))
    if backup_path.resolve() == target.resolve():
        raise BackupError("the backup and the target are the same file")

    verification = verify_backup(backup_path)
    if not verification.ok:
        raise BackupError(
            f"refusing to restore {backup_path.name}: it failed verification, so "
            "its file, its audit table or its hash chain is not what it claims. "
            "Problems: " + " | ".join(verification.problems)
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # Read before anything is copied, because it describes the file being
    # replaced and that file is about to stop existing. None means there is
    # nothing to reproduce: a restore onto a host where the database is not
    # there any more, where the staged file's own 0600 is the right answer.
    standing = target.stat() if target.exists() else None
    if standing is not None and not stat.S_ISREG(standing.st_mode):
        # A directory, a fifo, a device. os.replace onto one fails, but only
        # after a whole database has been copied for nothing, and the mode
        # matching further down would briefly widen the staged copy to the
        # directory's own 0755 on the way. Refused here, before any of that.
        raise BackupError(
            f"{target} is not a regular file, so there is nothing here to "
            "replace. Nothing was written."
        )
    # Once here so a locked target costs a second rather than a whole copy, and
    # again at the swap, which is the moment it has to be true.
    _refuse_a_target_somebody_holds(target)

    src = sqlite3.connect(_uri(backup_path, "rw"), uri=True, timeout=COPY_BUSY_TIMEOUT_SECONDS)
    try:
        with _staged_beside(target) as staged:
            dst = sqlite3.connect(staged, timeout=COPY_BUSY_TIMEOUT_SECONDS)
            try:
                _copy(src, dst, deadline_seconds)
            finally:
                dst.close()
            if standing is not None:
                _match_the_file_being_replaced(staged, standing, target)
            _refuse_a_target_somebody_holds(target)
    finally:
        src.close()
    return verification


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _report(result: BackupResult, out) -> None:
    if result.dry_run:
        print("dry run: nothing was written and nothing was deleted.", file=out)
        print(f"  would copy   {result.source}", file=out)
        for path in result.would_prune:
            print(f"  would delete {path}", file=out)
        return

    verification = result.verification
    chains = verification.chains_verified
    print(f"wrote {result.written}", file=out)
    print(
        f"  integrity_check {verification.integrity}, "
        f"{verification.audit_rows} audit rows, "
        f"{chains} audit {'chain' if chains == 1 else 'chains'} re-verified",
        file=out,
    )
    for note in verification.notes:
        print(f"  NOTE: {note}", file=out)
    for path in result.pruned:
        print(f"  deleted {path}", file=out)
    print(
        "  local disk only. This survives a bad deploy, not a lost host.",
        file=out,
    )
    # THE REPORT IS WHERE THIS BELONGS, and it is the line that was missing. The
    # module docstring and --help are read once, by whoever sets the command up.
    # This is read by whoever runs it, on the day they run it, and it is the only
    # one of the three that a person sees while deciding where to put the file
    # and who may reach it. Saying "local disk only" and stopping there tells an
    # operator the copy is at risk from a lost host and lets them infer that
    # nothing else is wrong with it. What is on the line below is specific on
    # purpose: not "unencrypted" on its own, which reads as a checkbox, but what
    # the file actually is and what reading it would get you.
    print(
        "  not encrypted: a plain SQLite file holding password hashes and the "
        "audit chain,",
        file=out,
    )
    print(
        "  readable by anyone who can read the file. File permissions are the "
        "only thing",
        file=out,
    )
    print("  protecting it.", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Take a verified snapshot of the Strata SQLite database.",
        epilog=(
            "Writes to local disk. No off-host destination is configured, and "
            "nothing schedules this. It is not disaster recovery. The copy is "
            "NOT ENCRYPTED: it is a plain SQLite file holding password hashes "
            "and the audit chain, readable by anyone who can read the file. It "
            "is written 0600 in a 0700 directory, and that is the whole of the "
            "protection. Choose --into accordingly."
        ),
    )
    parser.add_argument("--db", default=None, help="database file (default: the one the app opens)")
    parser.add_argument(
        "--into",
        required=True,
        help="destination directory. No default: see the module docstring. Keep it "
        "outside this repository, which is published with its history.",
    )
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, help=f"backups to keep (default {DEFAULT_KEEP})"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would happen and touch nothing"
    )
    parser.add_argument(
        "--busy-seconds",
        type=float,
        default=BUSY_DEADLINE_SECONDS,
        help=f"give up if the database stays locked this long (default {BUSY_DEADLINE_SECONDS:.0f})",
    )
    args = parser.parse_args(argv)

    try:
        source = Path(args.db) if args.db else default_database()
        result = run(
            source,
            Path(args.into),
            keep=args.keep,
            dry_run=args.dry_run,
            busy_seconds=args.busy_seconds,
        )
    except BackupError as exc:
        # Loud, on stderr, non-zero. A backup failure that scrolls past in green
        # is the same as no backup, discovered later.
        print("BACKUP FAILED", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1

    _report(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
