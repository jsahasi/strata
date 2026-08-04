"""Take a verified snapshot of the SQLite database, on the same disk it lives on.

WHAT THIS IS NOT. It writes to a directory on the local filesystem, and no
off-host destination is configured anywhere in this repository. A copy on the
same disk as the database survives a bad deploy, a bad migration and a mistaken
DELETE. It does not survive a lost host, a stolen laptop, a wiped volume or a
region going away. That is not disaster recovery and this file will not call it
that. Nothing schedules this script either: there is no cron entry, no timer and
no hosted job in this repository, so no backup exists anywhere until a person
runs it. deploy/site/security.html says the same in the same words.

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
    """
    if not source.exists():
        raise BackupError(f"no database at {source}; nothing was copied and nothing created")
    if not source.is_file():
        raise BackupError(f"{source} is not a file")

    src = sqlite3.connect(_uri(source, "rw"), uri=True, timeout=COPY_BUSY_TIMEOUT_SECONDS)
    try:
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

    engine = create_engine(f"sqlite:///{path}", future=True)
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
        into.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # An operator pointing --into at an existing file, or at a directory
        # they cannot write. Either way it is a failed backup and must leave by
        # the same door as every other one, not as a traceback.
        raise BackupError(f"cannot use {into} as a backup directory: {exc}") from exc
    _refuse_a_collision(planned)

    snapshot(source, planned, deadline_seconds=busy_seconds)
    verification = verify_backup(planned)

    if not verification.ok:
        quarantined = planned.with_name(planned.name + UNVERIFIED_SUFFIX)
        planned.rename(quarantined)
        raise BackupError(
            f"the copy at {quarantined} did not verify, so it is not a backup. "
            "It has been renamed out of the backup naming pattern and kept. "
            "Nothing was pruned. Problems: " + " | ".join(verification.problems)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Take a verified snapshot of the Strata SQLite database.",
        epilog=(
            "Writes to local disk. No off-host destination is configured, and "
            "nothing schedules this. It is not disaster recovery."
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
