"""The backup, held to the standard the product argues for.

WHY THIS FILE IS SHAPED THE WAY IT IS. A backup is a claim about a file nobody
reads until the day the original is gone. Almost every test you could write for
one passes against a script that copies nothing useful: the file exists, it has
the right name, the run exited zero. So the tests here are built around the two
ways this goes wrong in practice.

ONE. Copying a SQLite file while something is writing to it can produce a file
that looks fine and is torn. Every test that matters below runs the backup with a
write transaction OPEN on the source, and _writer_holds_the_lock proves the
transaction is really open rather than trusting that it is -- a test that quietly
degrades to an idle database would keep passing and stop proving anything.

TWO. A verification that always says "ok" passes every happy-path test ever
written for it. So the chain check is proved by breaking a row and demanding the
check catch it, and the integrity check by corrupting a page. Without those two,
the rest of this file would be evidence of nothing.

WHAT THESE TESTS DO NOT PROVE. They do not demonstrate a plain file copy coming
out torn. That was tried: with a writer holding an uncommitted transaction --
even one large enough to spill its pages to disk -- a copy of the file still
opens clean, because SQLite has not yet rewritten the header page. Tearing needs
the copy to straddle the commit, which is a race, and a test built on a race
either passes for the wrong reason or flakes. So the claim made here is the
narrower one that can be proved: the snapshot is consistent and complete while a
writer holds the file, it excludes what that writer has not committed, and when
the lock is exclusive the script refuses out loud instead of hanging.
"""

import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
# Building a database that is worth backing up
# ---------------------------------------------------------------------------


def _seed(db_path: Path, rows: int = 3, company: str = COMPANY) -> int:
    """A real chained log, written through record_event rather than by INSERT.

    Hand-built rows would carry hashes this suite computed, so the round-trip
    test would prove the tests agree with themselves. These are the rows the
    application writes.
    """
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
                reason="seeded so the backup has a chain to carry",
            )
        session.commit()
    engine.dispose()
    return rows


@contextmanager
def _write_transaction_open(db_path: Path, company: str = COMPANY):
    """Hold an uncommitted write on the source for as long as the block runs.

    record_event flushes, so an INSERT has been issued and SQLite holds a
    RESERVED lock on the file. Nothing is committed, so the rows inside this
    block must not appear in a snapshot taken during it.
    """
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    session = OrmSession(engine, future=True)
    try:
        record_event(
            session,
            company_id=company,
            actor="rae@meridian.example",
            action="action.approved",
            subject_type="obligation",
            subject_id="obligation-uncommitted",
            reason="written but never committed, so no snapshot may contain it",
        )
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _writer_holds_the_lock(db_path: Path) -> bool:
    """True when somebody else has the file locked for writing, right now.

    The guard on this whole file. An exclusive begin against an idle database
    succeeds immediately; against one with a write transaction open it is
    refused with no wait, because the timeout is zero.
    """
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()


def _audit_rows(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The guard on the guard
# ---------------------------------------------------------------------------


def test_the_lock_probe_can_tell_the_two_states_apart(tmp_path):
    """If this probe answered True always, every test below would be theatre."""
    db = tmp_path / "strata.db"
    _seed(db)
    assert _writer_holds_the_lock(db) is False, "an idle database is not locked"
    with _write_transaction_open(db):
        assert _writer_holds_the_lock(db) is True, "an open write must lock the file"


# ---------------------------------------------------------------------------
# The backup itself, against a database being written to
# ---------------------------------------------------------------------------


def test_a_backup_taken_mid_write_verifies(tmp_path):
    """The whole point: a live database, and a copy that stands up afterwards."""
    db = tmp_path / "strata.db"
    _seed(db, rows=4)
    dest = tmp_path / "backups"

    with _write_transaction_open(db):
        assert _writer_holds_the_lock(db) is True, "the source was idle; test proves nothing"
        result = backup.run(db, dest, keep=5)

    assert result.ok, result.verification
    assert result.written is not None and result.written.exists()
    assert result.verification.integrity == "ok"
    assert result.verification.chains_verified == 1


def test_the_snapshot_holds_the_committed_rows_and_not_the_open_ones(tmp_path):
    """A snapshot is a point in time, and the point is before the open write.

    Four rows are committed and a fifth is in flight. Five would mean the copy
    read a transaction that may yet roll back; three would mean it lost one.
    """
    db = tmp_path / "strata.db"
    _seed(db, rows=4)
    dest = tmp_path / "backups"

    with _write_transaction_open(db):
        result = backup.run(db, dest, keep=5)

    assert result.verification.audit_rows == 4
    assert _audit_rows(result.written) == 4


def test_the_source_is_untouched_by_being_backed_up(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db, rows=3)
    before = db.read_bytes()

    backup.run(db, tmp_path / "backups", keep=5)

    assert db.read_bytes() == before, "the backup wrote to the database it was reading"


def test_an_exclusive_lock_is_refused_out_loud_rather_than_waited_on_for_ever(tmp_path):
    """sqlite3.Connection.backup retries a locked source with no timeout of its
    own. Without the deadline this call never returns, and a scheduled job that
    hangs is indistinguishable from one nobody set up."""
    db = tmp_path / "strata.db"
    _seed(db)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    dest = tmp_path / "copy.db"

    started = time.monotonic()
    try:
        with pytest.raises(backup.BackupError) as raised:
            backup.snapshot(db, dest, deadline_seconds=0.4)
    finally:
        holder.rollback()
        holder.close()
    elapsed = time.monotonic() - started

    assert "locked" in str(raised.value).lower()
    assert elapsed < 20, f"waited {elapsed:.1f}s; the deadline did not bite"
    assert not dest.exists(), "left a half-written file behind"


# ---------------------------------------------------------------------------
# The test that matters most: the chain survives the round trip
# ---------------------------------------------------------------------------


def test_the_audit_chain_verifies_on_the_restored_copy(tmp_path):
    """This product's claim rests on the chain. A backup that breaks it is worse
    than none, because it looks like a record and is not one."""
    db = tmp_path / "strata.db"
    _seed(db, rows=6)
    _seed(db, rows=2, company=OTHER_COMPANY)
    dest = tmp_path / "backups"

    with _write_transaction_open(db):
        result = backup.run(db, dest, keep=5)
    assert result.ok, result.verification

    restored = tmp_path / "restored.db"
    verification = backup.restore(result.written, restored)
    assert verification.ok, verification.problems

    # Not the script's own verifier this time: the application's, on the
    # restored file, exactly as a person disputing the record would run it.
    engine = create_engine(f"sqlite:///{restored}", future=True)
    with OrmSession(engine, future=True) as session:
        assert verify_chain(session, COMPANY) is True
        assert verify_chain(session, OTHER_COMPANY) is True
    engine.dispose()

    assert _audit_rows(restored) == 8


def test_a_restore_refuses_a_backup_that_does_not_verify(tmp_path):
    """Installing an unverified backup over a live database is the one mistake
    that turns a bad day into an unrecoverable one."""
    db = tmp_path / "strata.db"
    _seed(db, rows=3)
    result = backup.run(db, tmp_path / "backups", keep=5)
    _break_a_chain_row(result.written)

    with pytest.raises(backup.BackupError) as raised:
        backup.restore(result.written, tmp_path / "restored.db")

    assert "chain" in str(raised.value).lower()
    assert not (tmp_path / "restored.db").exists(), "refused and still wrote the target"


# ---------------------------------------------------------------------------
# Proving the verifier is not a no-op
# ---------------------------------------------------------------------------


def _break_a_chain_row(db_path: Path) -> None:
    """Edit a row by raw SQL, which is the only way in: the ORM refuses."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE audit_events SET reason = 'edited' WHERE seq = 2")
        conn.commit()
    finally:
        conn.close()


def test_verification_catches_a_row_altered_in_the_copy(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db, rows=4)
    result = backup.run(db, tmp_path / "backups", keep=5)
    assert result.ok

    _break_a_chain_row(result.written)
    again = backup.verify_backup(result.written)

    assert again.ok is False
    assert again.integrity == "ok", "the file is intact; it is the record that is not"
    assert any("seq 2" in problem for problem in again.problems), again.problems


def test_verification_catches_a_corrupted_file(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db, rows=40)
    result = backup.run(db, tmp_path / "backups", keep=5)

    with open(result.written, "r+b") as handle:
        handle.seek(4096 * 2)
        handle.write(b"\xff" * 1200)

    again = backup.verify_backup(result.written)
    assert again.ok is False
    assert again.integrity != "ok"
    assert again.problems


def test_a_database_without_an_audit_table_is_refused(tmp_path):
    """A file that carries no trail is not a backup of this product."""
    stranger = tmp_path / "stranger.db"
    conn = sqlite3.connect(stranger)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    verification = backup.verify_backup(stranger)
    assert verification.ok is False
    assert verification.audit_rows is None, "no table is not a count of zero"
    assert any("audit_events" in problem for problem in verification.problems)


def test_an_empty_log_verifies_nothing_and_says_so(tmp_path):
    """Absence is denial. Zero chains checked is not a chain proved sound."""
    empty = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{empty}", future=True)
    Base.metadata.create_all(engine)
    engine.dispose()

    verification = backup.verify_backup(empty)
    assert verification.audit_rows == 0
    assert verification.chains_verified == 0
    assert any("no audit rows" in note.lower() for note in verification.notes), (
        "an empty log has to announce itself, not pass quietly"
    )


# ---------------------------------------------------------------------------
# Naming, retention, dry run
# ---------------------------------------------------------------------------


def test_the_filename_carries_a_utc_timestamp(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    when = datetime(2026, 8, 4, 9, 30, 15, tzinfo=timezone.utc)

    result = backup.run(db, tmp_path / "backups", keep=5, now=when)

    assert result.written.name == "strata-20260804T093015Z.db"


def test_a_second_backup_in_the_same_second_refuses_rather_than_overwrites(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    when = datetime(2026, 8, 4, 9, 30, 15, tzinfo=timezone.utc)
    dest = tmp_path / "backups"
    first = backup.run(db, dest, keep=5, now=when)

    with pytest.raises(backup.BackupError):
        backup.run(db, dest, keep=5, now=when)

    assert first.written.exists(), "the earlier backup was destroyed"


def test_retention_keeps_the_newest_and_deletes_the_rest(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"
    start = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

    written = [
        backup.run(db, dest, keep=3, now=start + timedelta(days=n)).written
        for n in range(5)
    ]

    left = sorted(p.name for p in dest.glob("strata-*.db"))
    assert left == sorted(p.name for p in written[-3:])


def test_retention_never_deletes_a_backup_that_failed(tmp_path):
    """Deleting the evidence of a bad backup to make room for a good one is
    exactly backwards."""
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"
    start = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

    bad = backup.run(db, dest, keep=1, now=start).written
    _break_a_chain_row(db)  # every backup from here on cannot verify
    with pytest.raises(backup.BackupError):
        backup.run(db, dest, keep=1, now=start + timedelta(days=1))

    kept = sorted(p.name for p in dest.iterdir())
    assert bad.name in kept, "pruned the good backup on the strength of a bad one"
    assert any(name.endswith(backup.UNVERIFIED_SUFFIX) for name in kept), kept


def test_a_backup_that_fails_verification_is_renamed_so_nobody_trusts_it(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db, rows=3)
    _break_a_chain_row(db)
    dest = tmp_path / "backups"

    with pytest.raises(backup.BackupError):
        backup.run(db, dest, keep=5)

    files = list(dest.iterdir())
    assert len(files) == 1
    assert files[0].name.endswith(backup.UNVERIFIED_SUFFIX)
    assert not files[0].name.endswith(".db"), "a bad copy must not look like a backup"


def test_dry_run_writes_nothing_at_all(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"
    start = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
    keeper = backup.run(db, dest, keep=1, now=start).written
    before = sorted(p.name for p in dest.iterdir())

    result = backup.run(db, dest, keep=1, now=start + timedelta(days=1), dry_run=True)

    assert result.dry_run is True
    assert result.verification is None, "a dry run cannot verify a file it did not write"
    assert sorted(p.name for p in dest.iterdir()) == before
    assert keeper.exists(), "a dry run pruned a real backup"
    assert result.would_prune == (keeper,), result.would_prune


def test_a_dry_run_predicts_the_refusal_it_would_hit(tmp_path):
    """Found by running the command twice in one second: the dry run reported
    nothing at all while the real run would have refused. A dry run that says
    "this would be fine" where the real one stops is the quiet lie."""
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"
    when = datetime(2026, 8, 4, 9, 30, 15, tzinfo=timezone.utc)
    backup.run(db, dest, keep=2, now=when)

    with pytest.raises(backup.BackupError):
        backup.run(db, dest, keep=2, now=when, dry_run=True)


def test_a_dry_run_will_not_overwrite_a_quarantined_copy_either(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    _break_a_chain_row(db)
    dest = tmp_path / "backups"
    when = datetime(2026, 8, 4, 9, 30, 15, tzinfo=timezone.utc)
    with pytest.raises(backup.BackupError):
        backup.run(db, dest, keep=2, now=when)

    with pytest.raises(backup.BackupError) as raised:
        backup.run(db, dest, keep=2, now=when, dry_run=True)
    assert backup.UNVERIFIED_SUFFIX in str(raised.value)


def test_a_dry_run_against_a_missing_destination_creates_no_directory(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"

    backup.run(db, dest, keep=2, dry_run=True)

    assert not dest.exists()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_missing_source_is_refused_and_not_created(tmp_path):
    """The trap that makes an empty file look like a clean backup for ever."""
    missing = tmp_path / "not-here.db"

    with pytest.raises(backup.BackupError):
        backup.run(missing, tmp_path / "backups", keep=2)

    assert not missing.exists(), "opened the source for writing and created it"


def test_a_destination_that_is_not_a_directory_is_refused(tmp_path):
    """An operator mistake leaves by the same door as every other failure."""
    db = tmp_path / "strata.db"
    _seed(db)
    blocker = tmp_path / "backups"
    blocker.write_text("this is a file, not a directory")

    with pytest.raises(backup.BackupError):
        backup.run(db, blocker, keep=2)


def test_keeping_nothing_is_refused(tmp_path):
    db = tmp_path / "strata.db"
    _seed(db)
    with pytest.raises(backup.BackupError):
        backup.run(db, tmp_path / "backups", keep=0)


def test_a_non_sqlite_url_is_refused_rather_than_guessed(tmp_path):
    with pytest.raises(backup.BackupError) as raised:
        backup.database_path_from_url("postgresql://user@host/strata")
    assert "sqlite" in str(raised.value).lower()


def test_a_sqlite_url_resolves_to_its_file():
    assert backup.database_path_from_url("sqlite:///strata.db") == Path("strata.db")
    assert backup.database_path_from_url("sqlite:////var/lib/strata.db") == Path(
        "/var/lib/strata.db"
    )


def test_an_in_memory_url_is_refused(tmp_path):
    """There is nothing on disk to back up, and an empty file would say there was."""
    with pytest.raises(backup.BackupError):
        backup.database_path_from_url("sqlite://")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_command_exits_zero_and_names_what_it_wrote(tmp_path, capsys):
    db = tmp_path / "strata.db"
    _seed(db, rows=3)
    dest = tmp_path / "backups"

    with _write_transaction_open(db):
        code = backup.main(["--db", str(db), "--into", str(dest), "--keep", "2"])

    out = capsys.readouterr().out
    assert code == 0
    assert "strata-" in out
    assert "3" in out, "the row count it verified is the reason to believe it"


def test_the_command_exits_non_zero_and_shouts_when_the_chain_is_broken(tmp_path, capsys):
    db = tmp_path / "strata.db"
    _seed(db, rows=4)
    _break_a_chain_row(db)
    dest = tmp_path / "backups"

    code = backup.main(["--db", str(db), "--into", str(dest), "--keep", "2"])

    captured = capsys.readouterr()
    assert code != 0
    assert "BACKUP FAILED" in captured.err
    assert "seq 2" in captured.err, captured.err


def test_the_command_exits_non_zero_when_the_source_is_missing(tmp_path, capsys):
    code = backup.main(["--db", str(tmp_path / "gone.db"), "--into", str(tmp_path / "b")])
    assert code != 0
    assert "BACKUP FAILED" in capsys.readouterr().err


def test_the_command_will_not_guess_a_destination(tmp_path, capsys):
    """A default would drop database copies inside this working tree, which is
    published with its history and does not ignore them."""
    db = tmp_path / "strata.db"
    _seed(db)

    with pytest.raises(SystemExit) as raised:
        backup.main(["--db", str(db)])

    assert raised.value.code != 0
    assert "--into" in capsys.readouterr().err


def test_the_dry_run_command_says_what_it_would_do_and_does_none_of_it(tmp_path, capsys):
    db = tmp_path / "strata.db"
    _seed(db)
    dest = tmp_path / "backups"

    code = backup.main(["--db", str(db), "--into", str(dest), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "dry run" in out.lower()
    assert not dest.exists()


# ---------------------------------------------------------------------------
# The claim on the tin
# ---------------------------------------------------------------------------


def test_the_module_says_where_the_backup_does_not_reach(tmp_path):
    """The honesty this repository is built on, pinned so it cannot be edited out.

    A backup on the same disk as the database survives a bad deploy and not a
    lost host. Anyone who reads this module has to meet that sentence before
    they decide what it covers.
    """
    text_of = backup.__doc__.lower()
    assert "same disk" in text_of
    assert "off-host" in text_of or "off host" in text_of
    assert "not disaster recovery" in text_of


def test_the_deployment_page_does_not_claim_a_schedule():
    """deploy/site/security.html must not say a backup runs, because none does.

    Whitespace is collapsed first: the claim is in the words, and an HTML line
    wrap falling between two of them is not a change of meaning.
    """
    page = Path(__file__).resolve().parents[1] / "deploy" / "site" / "security.html"
    words = " ".join(page.read_text(encoding="utf-8").split())

    assert "scripts/backup.py" in words, "the page has to name the script it describes"
    assert "Nothing schedules it" in words, "the page has to say no backup runs"
    assert "same disk as the database" in words
    assert "not disaster recovery" in words
