"""A restore either lands whole or does not land at all, and says which.

WHY THIS FILE EXISTS. restore() wrote straight into the live database. Every
failure between the first page and the last therefore left a file that was
neither the old database nor the new one -- while the refusal the operator read
said "Nothing was written". Measured before the fix, on a target that did not
exist yet: the refusal printed that sentence and left a zero-length file at the
target path. A zero-length file is a valid empty SQLite database, so the next
thing to open it finds a database with no tables rather than an error. The whole
product rests on the claim that a refusal names a true reason. A refusal that
lies about what is on the disk is worse than no refusal, because the person who
believes it retries, and the retry is the thing that loses the original.

WHAT IS REAL HERE AND WHAT IS INJECTED. Read this before trusting any of it.

  Real. test_a_refusal_that_says_nothing_was_written_leaves_nothing_behind takes
  a genuine exclusive lock on the backup file, so sqlite3_backup_step really
  returns BUSY, _copy's real deadline really fires, and the message asserted is
  the one an operator reads. The lock is taken between verification and the copy
  because verify_backup opens the same file and would otherwise sit on the lock
  for its own thirty-second timeout; the failure is not staged, only its moment.

  Injected. The tests that need bytes on the disk before the failure patch _copy
  to write into whatever file SQLite was handed and then raise. That is a
  simulation of a torn write, and the honest limit is this: it does not prove
  SQLite can be interrupted in that state, and it does not prove a power cut
  behaves this way. It proves the thing that was actually wrong -- that restore()
  pointed the write at the live database, so any writer that stops partway takes
  the original with it. Measured before the fix: the live database came back
  different bytes. Measured after: identical.

  A natural interruption was tried first and does not tear this file. With the
  source locked, SQLite's own rollback in sqlite3_backup_finish put the existing
  target back byte for byte. That path is worth having (the first test) and it is
  not evidence about a write that stops after pages have landed, because no page
  had landed. Saying so is the point of this paragraph.

  Real, and on the SUCCESS path.
  test_the_old_databases_write_ahead_log_does_not_replay_into_the_restored_one
  patches nothing at all. os.replace moves one file, and staging applied that
  reasoning only to the file it was moving and never to the file it was moving
  ONTO. A -wal the dying process left beside the live database is not carried by
  the rename: it stays, SQLite reads it as belonging to whatever now holds that
  name, and replays the old database's frames into the restored one. The result
  passes PRAGMA integrity_check, restore() returns normally, and the contents
  are in no backup. Writing into the target could not do this, because the write
  went through a connection on the target and SQLite recovered the log first.
  The test damages the main file for a reason the helper explains: an intact
  target gets its stray log cleared by SQLite the moment the lock guard connects
  to it, so an intact target would measure SQLite's housekeeping instead.

NOTHING BELOW NAMES A TEMPORARY FILE. The tests ask the destination connection
which file it is writing into (PRAGMA database_list), so they hold for whatever
the implementation calls it, and they fail if it turns out to be the live
database. A test that knew the temporary file's name would pass for a rename
that dropped it in another directory, where the rename cannot be atomic. Where
a staged name is unavoidable -- the orphan a killed run leaves -- it is built
from the module's own STAGING_PREFIX and STAGING_SUFFIX rather than typed out.
"""

import inspect
import os
import sqlite3
import stat
import sys
import traceback
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backup  # noqa: E402

from app.state.audit import record_event, verify_chain  # noqa: E402
from app.state.models import Base  # noqa: E402

COMPANY = "meridian-power"
OTHER_COMPANY = "cascade-water"


# ---------------------------------------------------------------------------
# Building something worth losing
# ---------------------------------------------------------------------------


def _seed(db_path: Path, rows: int = 3, company: str = COMPANY) -> None:
    """A real chained log, written through record_event rather than by INSERT."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    with OrmSession(engine, future=True) as session:
        for n in range(rows):
            record_event(
                session,
                company_id=company,
                actor="dana@meridian.example",
                action="login.succeeded",
                subject_type="session",
                subject_id=f"session-{n}",
                reason="seeded so there is something to lose",
            )
        session.commit()
    engine.dispose()


def _put_into_wal_mode(db_path: Path) -> None:
    """Make the file a WAL database, which is a durable property of the file.

    journal_mode=WAL is written into the header and survives every later open,
    so a host whose database was ever put into WAL mode stays in WAL mode
    whatever this application does or does not set.
    """
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode == "wal", f"the file would not go into WAL mode: {mode}"
        conn.commit()
    finally:
        conn.close()


def _a_verified_backup(tmp_path: Path, rows: int = 4) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    db = source / "strata.db"
    _seed(db, rows=rows)
    result = backup.run(db, tmp_path / "backups", keep=5)
    assert result.ok, result.verification
    return result.written


def _listing(directory: Path) -> list[str]:
    """Every name in the directory, journals and half-written copies included."""
    return sorted(p.name for p in directory.iterdir())


def _audit_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    finally:
        conn.close()


def _companies_in(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return sorted(
            row[0]
            for row in conn.execute("SELECT DISTINCT company_id FROM audit_events")
        )
    finally:
        conn.close()


def _destination_of(conn: sqlite3.Connection) -> Path:
    """The file this connection writes into, asked of the connection itself.

    Not guessed from a name invented here. If the implementation renames its
    temporary file tomorrow these tests still hold, and if it goes back to
    writing into the live database they still fail.
    """
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _a_torn_write(marker: bytes = b"TORN"):
    """A _copy that really puts bytes on the disk and then stops.

    Injected, and the module docstring says what that does and does not prove.
    """

    def torn(src, dst, deadline_seconds):
        path = _destination_of(dst)
        with open(path, "r+b") as handle:
            handle.write(marker * 64)
            handle.flush()
            os.fsync(handle.fileno())
        raise backup.BackupError(
            "the write stopped partway, with pages already on the disk. "
            "Nothing was written."
        )

    return torn


# ---------------------------------------------------------------------------
# The sentence has to be true
# ---------------------------------------------------------------------------


def test_a_refusal_that_says_nothing_was_written_leaves_nothing_behind(
    tmp_path, monkeypatch
):
    """The real deadline, the real message, and the disk it leaves behind.

    Before the fix this failed with a zero-length strata.db at the target path:
    the refusal said nothing was written and something was.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "strata.db"

    holders: list[sqlite3.Connection] = []
    real_verify = backup.verify_backup

    def verify_then_lock(path):
        # After verification, because verify_backup reads this same file and
        # would wait out its own timeout on the lock. The copy that follows is
        # entirely real and really cannot proceed.
        verification = real_verify(path)
        holder = sqlite3.connect(written, isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")
        holders.append(holder)
        return verification

    monkeypatch.setattr(backup, "verify_backup", verify_then_lock)

    try:
        with pytest.raises(backup.BackupError) as raised:
            backup.restore(written, target, deadline_seconds=0.4)
    finally:
        for holder in holders:
            holder.rollback()
            holder.close()

    assert holders, "the lock was never taken; this test proved nothing"
    assert "nothing was written" in str(raised.value).lower(), str(raised.value)
    assert _listing(out) == [], (
        f"the refusal said nothing was written and left {_listing(out)} behind"
    )


def test_an_interrupted_restore_leaves_the_live_database_byte_for_byte(
    tmp_path, monkeypatch
):
    """The file the operator cannot afford to lose is the one being replaced."""
    written = _a_verified_backup(tmp_path, rows=4)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=7, company=OTHER_COMPANY)
    before = live.read_bytes()
    before_listing = _listing(out)

    monkeypatch.setattr(backup, "_copy", _a_torn_write())

    with pytest.raises(backup.BackupError):
        backup.restore(written, live)

    assert live.read_bytes() == before, (
        "the restore stopped partway and took the live database with it"
    )
    assert _listing(out) == before_listing, (
        f"a failed restore left {sorted(set(_listing(out)) - set(before_listing))} "
        "beside the live database"
    )


def test_a_failed_restore_leaves_no_file_where_there_was_none(tmp_path, monkeypatch):
    """No target, no temporary, no journal. The directory is as it was.

    This is the case the old code got wrong in the plainest way: it created the
    target before it could fill it, so a failure left an empty database that
    every later reader would take for a real one.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "restored.db"

    monkeypatch.setattr(backup, "_copy", _a_torn_write())

    with pytest.raises(backup.BackupError):
        backup.restore(written, target)

    assert _listing(out) == [], f"a failed restore left {_listing(out)}"
    assert not target.exists()


def test_a_keyboard_interrupt_partway_leaves_the_live_database_alone(
    tmp_path, monkeypatch
):
    """Cleanup on an exception is not cleanup on ctrl-c unless it is written so.

    An `except Exception` reads as thorough and misses the one interruption an
    operator produces on purpose, at the moment they realise they typed the
    wrong path.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=5, company=OTHER_COMPANY)
    before = live.read_bytes()

    def interrupted(src, dst, deadline_seconds):
        path = _destination_of(dst)
        with open(path, "r+b") as handle:
            handle.write(b"HALF" * 64)
            handle.flush()
        raise KeyboardInterrupt()

    monkeypatch.setattr(backup, "_copy", interrupted)

    with pytest.raises(KeyboardInterrupt):
        backup.restore(written, live)

    assert live.read_bytes() == before, "ctrl-c partway took the live database"
    assert _listing(out) == ["strata.db"], f"left {_listing(out)}"


# ---------------------------------------------------------------------------
# Where the write actually goes
# ---------------------------------------------------------------------------


def test_the_live_database_is_never_the_file_sqlite_writes_into(tmp_path, monkeypatch):
    """The mechanism, not the symptom: same directory, private, not the target.

    Same directory because a rename is only atomic within one filesystem, and a
    temporary in /tmp is on another one often enough that the copy would fall
    back to a byte-by-byte move -- which is the in-place write again, wearing a
    different name. Private from the first byte, because the pages being written
    are password hashes and the whole audit chain, and a mode widened afterwards
    cannot take back a handle somebody already opened.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=5, company=OTHER_COMPANY)
    live.chmod(0o644)

    seen: list[tuple[Path, int]] = []
    real_copy = backup._copy

    def watch(src, dst, deadline_seconds):
        path = _destination_of(dst)
        seen.append((path, stat.S_IMODE(path.stat().st_mode)))
        return real_copy(src, dst, deadline_seconds)

    monkeypatch.setattr(backup, "_copy", watch)
    backup.restore(written, live)

    assert len(seen) == 1, seen
    path, mode = seen[0]
    assert path.resolve() != live.resolve(), (
        "the restore wrote straight into the live database; any failure from "
        "here on destroys it"
    )
    assert path.resolve().parent == live.resolve().parent, (
        f"the copy went to {path.parent}, not to {live.parent}; a rename across "
        "filesystems is not atomic"
    )
    assert mode == 0o600, (
        f"the copy was mode {oct(mode)} while the hashes were landing in it"
    )


def test_a_restore_replaces_what_was_there(tmp_path):
    """The happy path, which the atomic rewrite has to keep."""
    written = _a_verified_backup(tmp_path, rows=4)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=7, company=OTHER_COMPANY)

    verification = backup.restore(written, live)

    assert verification.ok, verification.problems
    assert _audit_rows(live) == 4
    assert _companies_in(live) == [COMPANY], (
        "the old contents survived the restore"
    )
    assert _listing(out) == ["strata.db"], f"left {_listing(out)} beside it"

    # The application's own checker on the restored file, not the script's.
    engine = create_engine(f"sqlite:///{live}", future=True)
    with OrmSession(engine, future=True) as session:
        assert verify_chain(session, COMPANY) is True
    engine.dispose()


def test_a_restore_keeps_the_mode_the_target_already_had(tmp_path):
    """A rename carries the temporary file's mode, which is not the target's.

    The old code opened the existing file and left its mode alone, deliberately:
    the live database's owner and mode were set by whoever deployed it, and
    narrowing them under an operator running as root hands them an outage in
    place of a data loss. Writing a new file and renaming it over the top loses
    that for free unless it is put back on purpose.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=3, company=OTHER_COMPANY)
    live.chmod(0o640)

    backup.restore(written, live)

    assert stat.S_IMODE(live.stat().st_mode) == 0o640, (
        f"the restore changed the live database to {oct(stat.S_IMODE(live.stat().st_mode))}"
    )


def test_a_restore_onto_nothing_creates_a_private_file(tmp_path):
    """Where there was no database, the new one is readable by nobody else."""
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "restored.db"

    backup.restore(written, target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600, (
        f"restored at {oct(stat.S_IMODE(target.stat().st_mode))}; every account "
        "on this host can read the password hashes in it"
    )


# ---------------------------------------------------------------------------
# A target somebody else has open
# ---------------------------------------------------------------------------


def test_a_target_another_connection_holds_is_refused_by_name(tmp_path):
    """Renaming over a live database is silent where writing into it was not.

    Before the fix this was refused by accident: SQLite reported the destination
    busy and the deadline turned that into a message about a lock. A rename does
    not care what has the file open, so the accidental guard would have gone.
    The refusal is now deliberate, and it names the file. The honest limit stays
    in restore()'s docstring: this catches a target that is locked right now, not
    one a process has open and idle.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=6, company=OTHER_COMPANY)
    before = live.read_bytes()

    holder = sqlite3.connect(live, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(backup.BackupError) as raised:
            backup.restore(written, live)
    finally:
        holder.rollback()
        holder.close()

    message = str(raised.value)
    assert str(live) in message, f"the refusal does not say which file: {message}"
    assert "open" in message.lower() or "lock" in message.lower(), message
    assert live.read_bytes() == before
    assert _listing(out) == ["strata.db"], f"left {_listing(out)}"


def test_a_write_ahead_log_left_beside_the_copy_is_refused_not_renamed(
    tmp_path, monkeypatch
):
    """A rename moves one file. Content in a sidecar would be left behind.

    Closing the connection checkpoints and removes the -wal today, so this
    should never fire -- which is the description of every control nobody
    tested. If a future SQLite keeps the log, a restore that renamed only the
    main file would install a database missing its most recent pages and report
    success.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=4, company=OTHER_COMPANY)
    before = live.read_bytes()

    real_copy = backup._copy

    def copy_then_strand_a_log(src, dst, deadline_seconds):
        path = _destination_of(dst)
        result = real_copy(src, dst, deadline_seconds)
        Path(str(path) + "-wal").write_bytes(b"pages nobody checkpointed")
        return result

    monkeypatch.setattr(backup, "_copy", copy_then_strand_a_log)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(written, live)

    assert "-wal" in str(raised.value) or "write-ahead" in str(raised.value).lower()
    assert live.read_bytes() == before
    assert _listing(out) == ["strata.db"], f"left {_listing(out)}"


def test_a_cleanup_that_fails_says_so_rather_than_reporting_a_clean_failure(
    tmp_path, monkeypatch
):
    """Removing the staged copy can fail too, and then the sentence changes.

    The refusal underneath ends with "Nothing was written", which was true of the
    destination and stops being true of the disk the moment the cleanup fails.
    Passing that message straight through would rebuild the defect one layer up.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=3, company=OTHER_COMPANY)
    before = live.read_bytes()

    real_unlink = Path.unlink

    def refuse_to_remove(self, *args, **kwargs):
        if self.name.startswith(backup.STAGING_PREFIX):
            raise OSError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(backup, "_copy", _a_torn_write())
    monkeypatch.setattr(Path, "unlink", refuse_to_remove)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(written, live)

    message = str(raised.value)
    monkeypatch.undo()
    stranded = [name for name in _listing(out) if name.startswith(backup.STAGING_PREFIX)]

    assert stranded, "the cleanup was not actually blocked; this test proved nothing"
    assert stranded[0] in message, f"the leftover file is not named: {message}"
    assert "not a database" in message, message
    assert live.read_bytes() == before, "the live database went anyway"


def test_a_symlinked_target_is_followed_to_the_database_it_means(tmp_path):
    """/var/lib/strata/strata.db pointing at a mounted volume is ordinary.

    Writing into the target went through the link to the far end. A rename would
    replace the LINK instead: a new file where the link was, the real database
    untouched, and an operator who has restored nothing and been told it worked.
    """
    written = _a_verified_backup(tmp_path, rows=4)
    real_home = tmp_path / "volume"
    real_home.mkdir()
    real_db = real_home / "strata.db"
    _seed(real_db, rows=9, company=OTHER_COMPANY)

    link_home = tmp_path / "var"
    link_home.mkdir()
    link = link_home / "strata.db"
    link.symlink_to(real_db)

    backup.restore(written, link)

    assert link.is_symlink(), "the restore replaced the link instead of the database"
    assert _audit_rows(real_db) == 4, (
        "the database the link points at was not restored"
    )
    assert _companies_in(real_db) == [COMPANY]
    assert _listing(link_home) == ["strata.db"], f"left {_listing(link_home)}"
    assert _listing(real_home) == ["strata.db"], f"left {_listing(real_home)}"


def test_a_target_that_is_not_a_file_is_refused_before_anything_is_copied(tmp_path):
    """A directory cannot be replaced, and finding that out after the copy is late."""
    written = _a_verified_backup(tmp_path)
    target = tmp_path / "out"
    target.mkdir()

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(written, target)

    assert "not a regular file" in str(raised.value)
    assert _listing(target) == [], f"left {_listing(target)} inside it"


def test_a_staged_file_is_never_mistaken_for_a_backup(tmp_path):
    """Retention globs the backup directory, and a staged copy lives in it.

    If the staging name ever matched PREFIX*SUFFIX, existing_backups() would
    count a half-written file towards --keep and prune() would be free to delete
    a real backup to make room for it. Built from the module's own constants, so
    renaming either one cannot quietly slip past this.
    """
    directory = tmp_path / "backups"
    directory.mkdir()
    staged = directory / f"{backup.STAGING_PREFIX}whatever{backup.STAGING_SUFFIX}"
    staged.write_bytes(b"half a database")
    real = directory / f"{backup.PREFIX}20260805T101500Z{backup.SUFFIX}"
    real.write_bytes(b"a finished one")

    assert backup.existing_backups(directory) == [real], (
        "a staged file was globbed as a backup"
    )
    assert backup.prune(directory, keep=1) == ()
    assert staged.exists(), "prune deleted a file that is not a backup"


# ---------------------------------------------------------------------------
# The class, derived from the module rather than listed here
# ---------------------------------------------------------------------------

#: A writer is any module function whose second parameter is the place it writes
#: to. Reading it off the signatures rather than naming the functions is what
#: makes this a guard on the class: a third writer added next year is held to the
#: same rule without anybody remembering this file exists.
DESTINATION_PARAMETERS = {"destination", "target"}


def _writers_that_take_a_destination() -> list[tuple[str, object]]:
    found = []
    for name, value in vars(backup).items():
        if not inspect.isfunction(value) or value.__module__ != backup.__name__:
            continue
        parameters = list(inspect.signature(value).parameters)
        if len(parameters) >= 2 and parameters[1] in DESTINATION_PARAMETERS:
            found.append((name, value))
    return sorted(found, key=lambda pair: pair[0])


def test_the_writers_are_found_by_reading_the_module(tmp_path):
    """The guard on the guard. An empty list would make the test below vacuous."""
    names = [name for name, _ in _writers_that_take_a_destination()]
    assert "restore" in names, names
    assert "snapshot" in names, names


@pytest.mark.parametrize(
    "name", [name for name, _ in _writers_that_take_a_destination()]
)
def test_no_writer_leaves_a_file_behind_when_the_copy_fails(
    name, tmp_path, monkeypatch
):
    """Every function that takes a destination cleans up after a failed write.

    snapshot() already did this by unlinking; restore() did not. The rule is
    written once, here, against whatever the module contains.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    destination = out / "written-by-a-failure.db"
    writer = dict(_writers_that_take_a_destination())[name]

    monkeypatch.setattr(backup, "_copy", _a_torn_write())

    with pytest.raises(backup.BackupError):
        writer(written, destination)

    assert _listing(out) == [], (
        f"{name}() failed and left {_listing(out)} behind"
    )


# ---------------------------------------------------------------------------
# The other end of the same sentence: a rename moves one file, and the
# DESTINATION has sidecars too
# ---------------------------------------------------------------------------


def _a_damaged_database_with_its_log_beside_it(directory: Path) -> tuple[Path, Path]:
    """What a process killed mid-write leaves: a bad main file and a real log.

    The damage is not decoration. A target this module can still open gets its
    stray log cleared by SQLite the moment the lock guard connects to it, so a
    test built on an intact target would be measuring SQLite's housekeeping and
    not this module's. A file too damaged to open is also the case restore()
    says it exists for.
    """
    live = directory / "strata.db"
    _seed(live, rows=9, company=OTHER_COMPANY)
    _put_into_wal_mode(live)
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE only_in_the_old_database (x)")
    conn.executemany(
        "INSERT INTO only_in_the_old_database VALUES (?)", [(n,) for n in range(500)]
    )
    conn.commit()
    log = Path(str(live) + "-wal")
    frames = log.read_bytes()
    assert frames, "the log was checkpointed away; this would prove nothing"
    conn.close()
    live.write_bytes(b"\x00" * 4096)
    log.write_bytes(frames)
    return live, log


def test_the_old_databases_write_ahead_log_does_not_replay_into_the_restored_one(
    tmp_path,
):
    """The success path, and nothing here is injected or patched.

    A crashed process leaves a -wal beside the database it was writing. That is
    the case restore() says it exists for. os.replace moves one file: the new
    database lands under the old name and the old database's log is still lying
    next to it. The next open finds a log whose header says it belongs to this
    file, replays those frames into it, and hands back a database that is
    neither the backup nor the old database -- with PRAGMA integrity_check
    saying "ok" and restore() having returned normally.

    Writing into the target could not do this: the write went through a
    connection on the target, so SQLite recovered the log and checkpointed it
    before the first page landed. Staging removed that for free, on the success
    path, which is the worst place to lose something.
    """
    source = tmp_path / "source"
    source.mkdir()
    source_db = source / "strata.db"
    _seed(source_db, rows=4)
    _put_into_wal_mode(source_db)
    written = backup.run(source_db, tmp_path / "backups", keep=5).written

    out = tmp_path / "out"
    out.mkdir()
    live, _log = _a_damaged_database_with_its_log_beside_it(out)

    backup.restore(written, live)

    assert _listing(out) == ["strata.db"], (
        f"the restore left {sorted(set(_listing(out)) - {'strata.db'})} beside the "
        "restored database, and a log SQLite thinks belongs to it will be replayed "
        "into it"
    )
    conn = sqlite3.connect(live)
    try:
        survivor = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='only_in_the_old_database'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert survivor == 0, (
        "the old database's own table is inside the restored file. It is in no "
        "backup and integrity_check calls the result ok"
    )
    assert _audit_rows(live) == 4
    assert _companies_in(live) == [COMPANY]


@pytest.mark.parametrize("suffix", list(backup.SIDECAR_SUFFIXES))
@pytest.mark.parametrize(
    "name", [name for name, _ in _writers_that_take_a_destination()]
)
def test_no_writer_leaves_the_destinations_own_sidecar_beside_it(
    name, suffix, tmp_path
):
    """The class, from the module's own list of what SQLite leaves lying about.

    The rule is not "clean up the -wal", it is "os.replace moves one file, so
    every name the destination owns is this function's problem". Written against
    SIDECAR_SUFFIXES and against the writers found by signature, so adding a
    fourth sidecar or a third writer is covered without editing this file.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    destination = out / "strata.db"
    _seed(destination, rows=6, company=OTHER_COMPANY)
    stale = Path(str(destination) + suffix)
    stale.write_bytes(b"whatever the process that died was part way through")
    writer = dict(_writers_that_take_a_destination())[name]

    writer(written, destination)

    assert not stale.exists(), (
        f"{name}() renamed onto {destination.name} and left {stale.name} beside "
        "it. SQLite reads that file as belonging to the new one"
    )
    assert _listing(out) == ["strata.db"], f"left {_listing(out)}"
    assert _audit_rows(destination) == 4


def test_a_sidecar_that_cannot_be_taken_away_refuses_before_the_swap(
    tmp_path, monkeypatch
):
    """It cannot go, so nothing goes. The target keeps its bytes and its log.

    Removing the destination's log and renaming over the destination are two
    operations and cannot be made one. The order is chosen so the window fails
    the safe way: the log goes first, and if it will not go, the rename does not
    happen and the sentence "nothing was written" is still true. The other order
    would leave a machine that died in the window holding the new database with
    the old log beside it, which is silent and passes every check there is.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live, stale = _a_damaged_database_with_its_log_beside_it(out)
    before = live.read_bytes()
    before_log = stale.read_bytes()

    real_unlink = Path.unlink

    def refuse_the_targets_sidecars(self, *args, **kwargs):
        if self.name.startswith(live.name) and self.name != live.name:
            raise OSError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_the_targets_sidecars)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(written, live)

    message = str(raised.value)
    monkeypatch.undo()

    assert stale.name in message, f"the refusal does not name the file: {message}"
    assert "nothing was written" in message.lower(), message
    assert live.read_bytes() == before, "the target was replaced anyway"
    assert stale.read_bytes() == before_log, "the log went and the database did not"
    staged = [n for n in _listing(out) if n.startswith(backup.STAGING_PREFIX)]
    assert staged == [], f"the refusal left {staged}"


def test_a_refusal_that_already_took_the_log_away_does_not_claim_nothing_was_written(
    tmp_path, monkeypatch
):
    """Removing the destination's journal is itself a write, so it has to be said.

    Clearing the old log and renaming over the old file are two operations. A
    failure caught between them leaves the destination file untouched and its
    journal gone -- and a database that died inside a transaction cannot roll it
    back without its journal. "Nothing was written" would be false in exactly the
    way this whole file exists to stop, one size smaller.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live, log = _a_damaged_database_with_its_log_beside_it(out)
    before = live.read_bytes()

    real_replace = os.replace

    def refuse_the_rename(src, dst, **kwargs):
        if Path(dst) == live:
            raise OSError(13, "Permission denied")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(backup.os, "replace", refuse_the_rename)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(written, live)

    message = str(raised.value)
    assert not log.exists(), "the log survived; this test is measuring nothing"
    assert "nothing was written" not in message.lower(), (
        "the destination lost its write-ahead log and the refusal said nothing "
        f"was written: {message}"
    )
    assert log.name in message, f"what went is not named: {message}"
    assert live.read_bytes() == before, "the target was replaced anyway"


# ---------------------------------------------------------------------------
# What a killed run leaves, and what the next run does about it
# ---------------------------------------------------------------------------


def _an_orphaned_staged_file(directory: Path) -> Path:
    """What SIGKILL, an OOM kill or a power cut leaves half way through a copy.

    Named from the module's own constants. The leading dot keeps it out of a
    plain ls, and it matches neither PREFIX*SUFFIX nor anything prune() looks
    at, so nothing else in this module can ever remove it.
    """
    orphan = directory / f"{backup.STAGING_PREFIX}killedrun{backup.STAGING_SUFFIX}"
    orphan.write_bytes(b"most of a database" * 1000)
    Path(str(orphan) + "-journal").write_bytes(b"and its journal")
    return orphan


def test_a_staged_file_a_killed_run_left_is_taken_away_by_the_next_one(tmp_path):
    """Nothing else can see this file, so the next writer through has to reap it.

    existing_backups() cannot glob it, by design and by test above, so prune()
    can never delete it. Without a reap they collect on the backup volume until
    it is full and every backup after that fails -- and one sits in the live
    database's own directory after a killed restore. The old code left a file
    inside PREFIX*SUFFIX here, which retention would eventually have taken; the
    staging traded a visible leftover for an invisible permanent one.
    """
    written = _a_verified_backup(tmp_path)
    directory = tmp_path / "backups"
    orphan = _an_orphaned_staged_file(directory)
    assert backup.existing_backups(directory) == [written], (
        "the orphan was globbed as a backup; this test is measuring the wrong thing"
    )

    backup.snapshot(written, directory / "second.db")

    assert not orphan.exists(), (
        "a full-size partial database no other code path can see is still there"
    )
    assert not Path(str(orphan) + "-journal").exists()


def test_a_restore_reaps_the_orphan_left_beside_the_live_database(tmp_path):
    """A killed restore leaves its staged file in the live database's directory."""
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=5, company=OTHER_COMPANY)
    orphan = _an_orphaned_staged_file(out)

    backup.restore(written, live)

    assert _listing(out) == ["strata.db"], f"left {_listing(out)}"
    assert not orphan.exists()


def test_a_backup_reaps_the_orphan_a_killed_restore_left_beside_the_database(tmp_path):
    """Otherwise the one in the live directory waits for the next restore.

    A backup runs nightly and a restore runs about never, so an orphan left in
    the live database's own directory by a killed restore would sit there for
    months -- and it is a full copy of the database, on the volume that can least
    afford one. The nightly run reads that directory anyway; it can take its own
    dead with it.
    """
    live = tmp_path / "live"
    live.mkdir()
    db = live / "strata.db"
    _seed(db, rows=3)
    orphan = _an_orphaned_staged_file(live)

    result = backup.run(db, tmp_path / "backups", keep=5)

    assert result.ok, result.verification
    assert not orphan.exists(), (
        "a killed restore's copy of the whole database is still sitting beside "
        "the live one, and only another restore would ever remove it"
    )
    assert _listing(live) == ["strata.db"], f"left {_listing(live)}"


def test_a_dry_run_reaps_nothing(tmp_path):
    """A dry run that leaves the directory different is not one.

    Reaping is a deletion, and --dry-run promises none. The promise is checked
    here rather than trusted, because the reap was added after the promise.
    """
    live = tmp_path / "live"
    live.mkdir()
    db = live / "strata.db"
    _seed(db, rows=3)
    orphan = _an_orphaned_staged_file(live)
    into = tmp_path / "backups"

    result = backup.run(db, into, keep=5, dry_run=True)

    assert result.dry_run
    assert orphan.exists(), "a dry run deleted a file"
    assert not into.exists(), "a dry run created the backup directory"


def test_a_staged_file_a_run_still_in_flight_holds_is_left_alone(tmp_path):
    """Reaping by age or by name would eat a copy another run is part way through.

    Two backups into one directory at once is ordinary -- a cron job and an
    operator, or two hosts on one mount. The file being reaped has to be shown
    to be dead, not merely to look old, and the only thing that can show it is
    that no live process holds it.
    """
    written = _a_verified_backup(tmp_path)
    directory = tmp_path / "backups"

    with backup._staged_beside(directory / "in-flight.db") as staged:
        staged.write_bytes(b"a copy another run is part way through")
        backup.snapshot(written, directory / "second.db")
        assert staged.exists(), (
            "a run in flight had its staged file taken away by another run"
        )
        assert staged.read_bytes() == b"a copy another run is part way through"

    assert (directory / "in-flight.db").exists()


# ---------------------------------------------------------------------------
# The messages an operator actually reads
# ---------------------------------------------------------------------------


def test_a_cleanup_that_fails_after_ctrl_c_still_names_the_leftover(
    tmp_path, monkeypatch
):
    """Ctrl-c is the one branch that used to leave a file and say nothing.

    The type must stay KeyboardInterrupt: swapping it for a BackupError would
    turn a deliberate interrupt into an ordinary failure and break every caller
    that treats the two differently. So the leftover is added to the exception
    rather than in place of it, and what an operator reads is the traceback.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    live = out / "strata.db"
    _seed(live, rows=3, company=OTHER_COMPANY)
    before = live.read_bytes()

    real_unlink = Path.unlink

    def refuse_to_remove(self, *args, **kwargs):
        if self.name.startswith(backup.STAGING_PREFIX):
            raise OSError(13, "Permission denied")
        return real_unlink(self, *args, **kwargs)

    def interrupted(src, dst, deadline_seconds):
        path = _destination_of(dst)
        with open(path, "r+b") as handle:
            handle.write(b"HALF" * 64)
            handle.flush()
        raise KeyboardInterrupt()

    monkeypatch.setattr(backup, "_copy", interrupted)
    monkeypatch.setattr(Path, "unlink", refuse_to_remove)

    with pytest.raises(KeyboardInterrupt) as raised:
        backup.restore(written, live)

    # Everything the operator sees, message and notes together, so this holds
    # whichever of the two the implementation uses.
    shown = "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    monkeypatch.undo()
    stranded = [n for n in _listing(out) if n.startswith(backup.STAGING_PREFIX)]

    assert stranded, "the cleanup was not actually blocked; this test proved nothing"
    assert stranded[0] in shown, (
        "ctrl-c left a partial database on the disk and told the operator nothing: "
        + shown
    )
    assert live.read_bytes() == before, "ctrl-c partway took the live database"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a directory it cannot")
@pytest.mark.parametrize(
    "name", [name for name, _ in _writers_that_take_a_destination()]
)
def test_a_directory_that_cannot_be_written_is_refused_by_its_own_name(
    name, tmp_path
):
    """The refusal names the file the operator asked for, not one they never saw.

    The temporary file is an implementation detail with a random name that no
    longer exists by the time anybody reads the message. A bare PermissionError
    quoting it tells an operator nothing they can act on.
    """
    written = _a_verified_backup(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    destination = out / "strata.db"
    writer = dict(_writers_that_take_a_destination())[name]
    out.chmod(0o500)

    try:
        with pytest.raises(backup.BackupError) as raised:
            writer(written, destination)
    finally:
        out.chmod(0o700)

    message = str(raised.value)
    assert str(destination) in message or str(out) in message, message
    assert backup.STAGING_PREFIX not in message, (
        "the refusal names a temporary file the operator has never seen and "
        f"which no longer exists: {message}"
    )
    assert "nothing was written" in message.lower(), message
    assert _listing(out) == [], f"left {_listing(out)}"
