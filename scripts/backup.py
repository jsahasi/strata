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
import os
import sqlite3
import stat
import sys
import time
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


def _create_private(path: Path) -> None:
    """Create `path` empty and owner-only, BEFORE anything is written into it.

    THE ORDER IS THE WHOLE POINT, and getting it wrong is the easy mistake. The
    first version of this fix took the snapshot and then chmod'ed the result,
    which reads as correct and is not: between the first page landing and the
    chmod returning, a complete set of scrypt password hashes sits on a shared
    host at whatever the umask gave -- 0644 under the usual 022. A backup of a
    real database takes long enough for that window to be worth having. Worse,
    closing the window does not close a file handle another process opened while
    it was open, so a chmod afterwards cannot undo what it failed to prevent.
    O_CREAT with a mode argument has no window at all: the file has never existed
    with any other permissions.

    The fchmod is not redundant. The mode passed to os.open is masked by the
    process umask, which can only clear bits -- so a hostile-but-legal umask of
    0077 leaves 0600 alone, but 0400 would leave the file at 0200 and a later
    read of our own backup would fail for a reason nobody would guess. fchmod
    sets the bits exactly, and it works on the descriptor rather than the path,
    so nothing can be swapped underneath it between the two calls.

    A file that already exists is left alone rather than re-permissioned. Modes
    on a file this script did not create belong to whoever did create it: for the
    backup copy the case cannot arise, because _refuse_a_collision has already
    established the name is free, and for restore's target the file is a live
    database whose mode and owner were set by whoever deployed it. Narrowing that
    to 0600 under an operator running as root would hand them an outage in place
    of a data loss -- deploy/Dockerfile runs the application as the `strata`
    system user, not as the person most likely to be typing this command.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, COPY_MODE)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, COPY_MODE)
    finally:
        os.close(fd)


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

    The destination is created 0600 before it is opened, not tightened after it
    is written. See _create_private for why the order is the whole of it.
    """
    if not source.exists():
        raise BackupError(f"no database at {source}; nothing was copied and nothing created")
    if not source.is_file():
        raise BackupError(f"{source} is not a file")

    src = sqlite3.connect(_uri(source, "rw"), uri=True, timeout=COPY_BUSY_TIMEOUT_SECONDS)
    try:
        # Inside the try, so that a failure from here on takes the unlink path
        # below with it. An empty 0600 file left behind by a refused copy would
        # collide with the next run at the same second and be read as evidence of
        # a backup that never happened. A zero-length file is a valid empty
        # SQLite database, so connect() below adopts this one rather than
        # replacing it, and the pages land inside a file that was never readable.
        _create_private(destination)
        dst = sqlite3.connect(destination, timeout=COPY_BUSY_TIMEOUT_SECONDS)
        try:
            _copy(src, dst, deadline_seconds)
        finally:
            dst.close()
    except BackupError:
        # A half-written copy is the most dangerous file on the disk. It is
        # removed here rather than left for somebody to find and trust.
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
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

    The target must not be open in another process. Nothing here can tell
    whether it is, which is the honest reason there is no command for this.
    """
    backup_path = Path(backup_path)
    target = Path(target)
    if backup_path.resolve() == target.resolve():
        raise BackupError("the backup and the target are the same file")

    verification = verify_backup(backup_path)
    if not verification.ok:
        raise BackupError(
            f"refusing to restore {backup_path.name}: it failed verification, so "
            "its file, its audit table or its hash chain is not what it claims. "
            "Problems: " + " | ".join(verification.problems)
        )

    src = sqlite3.connect(_uri(backup_path, "rw"), uri=True, timeout=COPY_BUSY_TIMEOUT_SECONDS)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # The restore is the backward path of the same bug, and a fix that
        # covered only the outward one would put the hashes back on disk at 0644
        # the moment anybody used it. A target that already exists keeps the mode
        # it has -- see _create_private -- so this bites on the case that matters,
        # a restore onto a host where the database file is not there any more.
        _create_private(target)
        dst = sqlite3.connect(target, timeout=COPY_BUSY_TIMEOUT_SECONDS)
        try:
            _copy(src, dst, deadline_seconds)
        finally:
            dst.close()
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
