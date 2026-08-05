"""The migration that runs on every deploy, held against real databases.

WHY THIS FILE EXISTS. app/state/migrate.py measured no coverage at all, and it
is the last thing standing between a redeploy and a site that answers every
screen with

    (sqlite3.OperationalError) no such column: document_versions.source_url

That is not a theory. deploy/entrypoint.sh seeded only when there was no
database file and ran NOTHING when there was one, so the first deploy after a
column was added would have kept yesterday's table under today's SELECT. The
suite could not catch it then and cannot catch it now by any other route: every
other test builds its schema from the current models, so no test in this
repository has ever seen yesterday's database except the ones below.

REAL FILES, NOT MOCKS. Every test here builds a SQLite database in tmp_path and
runs the real migration against it. A mocked migration proves that the mock was
called; the only question worth asking is what the file looks like afterwards,
and the only way to ask it is to open the file. tests/conftest.py points the
suite at a scratch database before any app import, and nothing here goes near
the developer's strata.db -- every engine below is built from a tmp_path.

WHAT "YESTERDAY'S DATABASE" MEANS HERE. It is built by creating today's schema
and then taking something away: dropping a column, dropping a table, dropping an
index. That is the same shape as a database written by older code and it is
honest about what it proves -- the migration is asked to close a gap it did not
create and cannot see coming.

THE THREE RULES BEING GUARDED, in the order they cost most when broken:
  1. Additive only. It never drops, renames, retypes or backfills. The audit
     chain is append-only and hash-linked; a rewrite invalidates it silently.
  2. It refuses rather than guesses, and the refusal reaches the exit code, so a
     deploy stops instead of starting against a schema the code cannot read.
  3. It is idempotent, because it runs on every start and an unsafe second run
     is the same as no migration at all.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.state.audit import DIGEST_V1, verify_chain
from app.state.migrate import migrate
from app.state.models import Base

# The golden pre-attribution log, reused rather than rebuilt. A second hand-made
# copy of that DDL and those hashes would drift from the first, and the day it
# drifted the test that matters would pass for the wrong reason.
from tests.test_audit_v2 import _legacy_database

REPO_ROOT = Path(__file__).resolve().parents[1]

# The five NOT NULL columns document_versions needs before it will take a row,
# plus the provenance ones this test drops and restores.
_VERSION_ROW = (
    "INSERT INTO document_versions "
    "(id, company_id, docket, label, status, source_text, source_sha256) "
    "VALUES ('VER-OLD', 'meridian-power', '2024-00123', 'Order', 'FINAL', "
    "'the text as filed', 'a1b2c3')"
)

_ITEM_ROW = (
    "INSERT INTO improvement_items "
    "(id, company_id, category, status, review_decision, priority, detail, created_at) "
    "VALUES ('IMP-OLD', 'meridian-power', 'defect', 'open', 'pending', 'high', "
    "'written before the column existed', '2026-01-01T00:00:00Z')"
)


# ---------------------------------------------------------------------------
# Building databases that predate the code
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_for(tmp_path):
    """Hand out engines on tmp_path files and dispose of them afterwards.

    Disposal matters on Windows and in any run that inspects the file after the
    test: an undisposed SQLite engine holds the handle open, and the failure it
    produces is a permissions error a long way from its cause.
    """
    made = []

    def factory(name: str = "strata.db"):
        engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
        made.append(engine)
        return engine

    yield factory
    for engine in made:
        engine.dispose()


def _current(engine):
    """Today's schema, exactly as a fresh install would have it."""
    Base.metadata.create_all(engine)
    return engine


def _yesterday(engine):
    """Today's schema with the last release taken back out of it.

    Four separate gaps in one file, because a deploy meets them together:
      - document_versions has no source_url. This is the real near miss.
      - passages does not exist at all. A new table is a feature arriving.
      - proceedings carries legacy_flag, which no model has ever heard of.
      - legacy_notes is a whole table no model has ever heard of.
    The last two are the additive-only guard: they must both come through the
    migration untouched, rows and all.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(_VERSION_ROW))
        connection.execute(text("ALTER TABLE document_versions DROP COLUMN source_url"))
        connection.execute(text("DROP TABLE passages"))
        connection.execute(text("ALTER TABLE proceedings ADD COLUMN legacy_flag TEXT"))
        connection.execute(
            text(
                "INSERT INTO proceedings "
                "(id, company_id, docket, commission, subject, legacy_flag) "
                "VALUES ('PRC-OLD', 'meridian-power', '2024-00123', 'KY PSC', "
                "'Rate case', 'set by code nobody has now')"
            )
        )
        connection.execute(
            text("CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, note TEXT)")
        )
        connection.execute(
            text("INSERT INTO legacy_notes (id, note) VALUES (1, 'written by hand in 2025')")
        )
    return engine


def _columns(engine, table: str) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(table)}


def _changes(report) -> dict[str, list[str]]:
    """The three keys this file is about, lifted out of whatever else is there.

    The report has grown a fourth key for the passage index and may grow more.
    Pinning the whole dictionary would make every test here fail the next time
    somebody adds a key that has nothing to do with the schema -- and worse, it
    would tempt whoever hits that failure to fold the new key into an existing
    one. These three are the schema, and only "refused" stops a deploy.
    """
    return {key: report[key] for key in ("tables", "columns", "refused")}


_NOTHING = {"tables": [], "columns": [], "refused": []}


def _schema(engine) -> list[tuple[str, str]]:
    """Every table and index in the file, as SQLite itself records them.

    Read from sqlite_master rather than assembled from the inspector because the
    idempotence test needs to compare two whole files and see nothing move --
    including the exact DDL text of every index.
    """
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT name, COALESCE(sql, '') FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ).all()
    return [(name, sql) for name, sql in rows]


# ---------------------------------------------------------------------------
# The near miss: a column the deployed database has never had
# ---------------------------------------------------------------------------


def test_the_query_that_would_have_taken_the_site_down_works_after_migrating(engine_for):
    """The production failure, reproduced and then closed. Nothing else catches it.

    Before the migration the SELECT the application makes on every proceeding,
    claim and verification screen raises "no such column". This test asserts the
    failure first, on purpose: without it, the second half would pass against a
    migration that did nothing on a database that never had the gap.
    """
    engine = _yesterday(engine_for())

    with pytest.raises(Exception) as raised:
        with engine.begin() as connection:
            connection.execute(text("SELECT source_url FROM document_versions"))
    assert "no such column" in str(raised.value)

    migrate(engine)

    with engine.begin() as connection:
        rows = connection.execute(text("SELECT id, source_url FROM document_versions")).all()
    assert rows == [("VER-OLD", None)]


def test_it_adds_the_column_the_models_grew(engine_for):
    engine = _yesterday(engine_for())

    report = migrate(engine)

    assert "document_versions.source_url" in report["columns"]
    assert "source_url" in _columns(engine, "document_versions")


def test_the_new_column_is_null_on_the_rows_that_predate_it(engine_for):
    """Deliberate, not accidental. NULL is the only honest value here.

    NULL says the schema of the day did not record this. A backfilled default
    would have every historical filing assert a provenance nobody checked -- and
    in a product whose whole claim is that a citation either verifies or refuses,
    inventing a source URL is the one thing that must never happen quietly.
    """
    engine = _yesterday(engine_for())

    migrate(engine)

    with engine.begin() as connection:
        value = connection.execute(
            text("SELECT source_url FROM document_versions WHERE id = 'VER-OLD'")
        ).scalar_one()
    assert value is None, "a migration that guesses a value fabricates provenance"


def test_it_creates_a_table_the_models_added(engine_for):
    """A new table is a feature arriving, and create_all alone is what does it."""
    engine = _yesterday(engine_for())

    report = migrate(engine)

    assert report["tables"] == ["passages"]
    assert inspect(engine).has_table("passages")


def test_an_empty_file_gets_the_whole_schema(engine_for):
    """The first deploy. Everything is missing and nothing is refused.

    Worth its own test because it is the only path where create_all does all the
    work and the column loop finds nothing to do -- so a bug in the column loop
    hides completely here, and every other test in this file is what finds it.
    """
    engine = engine_for("brand-new.db")

    report = migrate(engine)

    assert len(report["tables"]) == len(Base.metadata.sorted_tables)
    assert report["columns"] == []
    assert report["refused"] == []


# ---------------------------------------------------------------------------
# Additive only
# ---------------------------------------------------------------------------


def test_a_table_the_models_never_heard_of_survives_with_its_rows(engine_for):
    """The migration derives what to ADD. It must never derive what to remove.

    Symmetry is the trap here: the same comparison that says "the models have a
    table the database lacks" would just as easily say "the database has a table
    the models lack", and the second half of that sentence deletes a customer's
    data. A hand-written table beside ours is a normal thing for an operator to
    have made.
    """
    engine = _yesterday(engine_for())

    migrate(engine)

    with engine.begin() as connection:
        rows = connection.execute(text("SELECT id, note FROM legacy_notes")).all()
    assert rows == [(1, "written by hand in 2025")]


def test_a_column_the_models_never_heard_of_survives_with_its_values(engine_for):
    """The same rule one level down, and the more likely one to be broken.

    "Make the database match the models" read strictly means dropping this
    column. It is read as "add what is missing" and nothing else, because the
    other reading destroys data on a deploy, at the moment nobody is watching the
    schema.
    """
    engine = _yesterday(engine_for())

    migrate(engine)

    assert "legacy_flag" in _columns(engine, "proceedings")
    with engine.begin() as connection:
        value = connection.execute(
            text("SELECT legacy_flag FROM proceedings WHERE id = 'PRC-OLD'")
        ).scalar_one()
    assert value == "set by code nobody has now"


def test_it_deletes_no_row(engine_for):
    """Stated once, bluntly, across every table that had one."""
    engine = _yesterday(engine_for())

    with engine.begin() as connection:
        before = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("document_versions", "proceedings", "legacy_notes")
        }

    migrate(engine)

    with engine.begin() as connection:
        after = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("document_versions", "proceedings", "legacy_notes")
        }
    assert after == before
    assert all(count == 1 for count in after.values()), "the fixture stopped proving anything"


def test_it_writes_no_ddl_that_drops_or_renames_or_retypes(engine_for):
    """The rule as SQLite records it, rather than as the report claims it.

    A report is written by the same code that did the work, so a migration that
    dropped a column could report anything it liked. sqlite_master is the file
    itself. Nothing that existed before may be missing afterwards.
    """
    engine = _yesterday(engine_for())
    before = dict(_schema(engine))

    migrate(engine)

    after = dict(_schema(engine))
    assert set(before) <= set(after), f"lost: {sorted(set(before) - set(after))}"
    for name, sql in before.items():
        if name == "document_versions":
            # The one table that legitimately changed shape, and only by growing.
            assert "source_url" in after[name]
            continue
        assert after[name] == sql, f"rewrote {name}"


# ---------------------------------------------------------------------------
# It refuses rather than guessing
# ---------------------------------------------------------------------------


def _needs_a_not_null_column(engine):
    """A table with rows, missing a column the models declare NOT NULL.

    improvement_items.title is the column: no default, no server default, and a
    row already there. SQLite cannot add it and no honest value exists for the
    row that is already written, so there is nothing to do but stop.
    """
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE improvement_items DROP COLUMN title"))
        connection.execute(text(_ITEM_ROW))
    return engine


def test_a_not_null_column_with_no_default_is_refused(engine_for):
    """Inventing a value writes a fact into a historical row that nobody asserted.

    The alternatives are all worse: an empty string claims the item had no title,
    a placeholder claims a machine wrote one, and rebuilding the table to add the
    constraint rewrites rows the audit chain has hashed. Refusing is the only
    move that tells the truth, and it hands the decision to a person.
    """
    engine = _needs_a_not_null_column(engine_for())

    report = migrate(engine)

    assert len(report["refused"]) == 1
    refusal = report["refused"][0]
    assert refusal.startswith("improvement_items.title:")
    assert "NOT NULL with no default" in refusal
    # It says what to do next, not merely that it stopped. A refusal a reader
    # cannot act on stops a deploy twice.
    assert "backfill deliberately" in refusal


def test_a_refusal_changes_nothing_at_all(engine_for):
    """A half-applied migration is worse than none: nobody knows where it stopped.

    The column is still absent and the row is still exactly as it was, so the
    person who reads the refusal is looking at the same database the deploy
    started with.
    """
    engine = _needs_a_not_null_column(engine_for())

    migrate(engine)

    assert "title" not in _columns(engine, "improvement_items")
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, detail FROM improvement_items")
        ).all()
    assert row == [("IMP-OLD", "written before the column existed")]


def test_a_refusal_does_not_stop_the_rest_of_the_work(engine_for):
    """One refusal must not hide the other four things a reader needs to fix.

    Stopping at the first refusal turns one deploy into four, each revealing the
    next problem. The report carries everything found in one pass and the exit
    code stops the deploy once.
    """
    engine = _needs_a_not_null_column(engine_for())
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE passages"))

    report = migrate(engine)

    assert report["refused"], "the fixture stopped producing a refusal"
    assert report["tables"] == ["passages"], "gave up before doing the work it could do"


# ---------------------------------------------------------------------------
# The exit code, which is the only part a deploy reads
# ---------------------------------------------------------------------------


def _run_script(db_path: Path):
    """scripts/migrate.py as deploy/entrypoint.sh runs it: a real process.

    Calling main() in this process would test the function again and prove
    nothing about the script -- and the script is where the exit code lives, and
    the exit code is the entire mechanism. `set -eu` in the entrypoint turns a
    non-zero exit into a stopped container; a zero exit starts uvicorn against
    whatever schema is there.

    STRATA_DATABASE_URL is passed explicitly. The script calls load_env() first,
    which reads the developer's real .env -- and must not override a name the
    environment already sets, which is why this test can trust the value it sent.
    """
    return subprocess.run(
        [sys.executable, "scripts/migrate.py"],
        cwd=REPO_ROOT,
        env={**os.environ, "STRATA_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_script_exits_non_zero_when_it_refuses(tmp_path):
    """The deploy stops. Without this the container starts on a schema it cannot
    read and the refusal is a line in a log nobody is watching."""
    db = tmp_path / "refuses.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    _needs_a_not_null_column(engine)
    engine.dispose()

    result = _run_script(db)

    assert result.returncode != 0, result.stdout


def test_the_script_names_the_refusal_on_stderr(tmp_path):
    """A stopped deploy has to say which column, or the operator is guessing.

    stderr rather than stdout because that is the stream a container log
    aggregator flags, and because the successful runs put their reports on
    stdout -- one stream per meaning.
    """
    db = tmp_path / "refuses.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    _needs_a_not_null_column(engine)
    engine.dispose()

    result = _run_script(db)

    assert "MIGRATION REFUSED" in result.stderr
    assert "improvement_items.title" in result.stderr
    assert "refusing to start" in result.stderr


def test_the_script_exits_zero_and_reports_what_it_added(tmp_path):
    """A deploy that fixed something must go on to serve, and say what it did."""
    db = tmp_path / "yesterday.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    _yesterday(engine)
    engine.dispose()

    result = _run_script(db)

    assert result.returncode == 0, result.stderr
    assert "migration added tables: passages" in result.stdout
    assert "document_versions.source_url" in result.stdout


def test_the_script_says_so_when_there_is_nothing_to_do(tmp_path):
    """Silence on a healthy deploy reads as a script that did not run.

    A reader of the container log has to be able to tell "the schema is current"
    apart from "the migration never fired", and those two look identical if a
    no-op prints nothing.
    """
    db = tmp_path / "current.db"
    engine = create_engine(f"sqlite:///{db}", future=True)
    _current(engine)
    engine.dispose()

    result = _run_script(db)

    assert result.returncode == 0, result.stderr
    assert "schema already current" in result.stdout


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_the_second_run_changes_nothing(engine_for):
    """It runs on every start. An unsafe second run is the same as no migration.

    Compared against sqlite_master rather than against the report: the report is
    written by the code under test, so a second run that quietly rebuilt an index
    could still report an empty list.
    """
    engine = _yesterday(engine_for())
    first = migrate(engine)
    assert first["tables"] or first["columns"], "the fixture stopped needing a migration"
    after_first = _schema(engine)

    second = migrate(engine)

    assert _changes(second) == _NOTHING
    assert _schema(engine) == after_first


def test_a_third_and_fourth_run_are_also_quiet(engine_for):
    """Twice can pass by luck. A container restarts more often than that."""
    engine = _yesterday(engine_for())
    migrate(engine)

    for _ in range(3):
        assert _changes(migrate(engine)) == _NOTHING


def test_migrating_a_current_database_reports_nothing(engine_for):
    """The everyday deploy, where the schema has not moved."""
    engine = _current(engine_for())

    assert _changes(migrate(engine)) == _NOTHING


def test_an_index_the_database_lost_comes_back(engine_for):
    """create_all skips a table it can already see, and its indexes with it.

    So a new index on an existing table is the second half of the same bug as a
    new column: the fresh install has it, the migrated one does not, and the only
    symptom is that one deployment is slow while the other is fast.
    """
    engine = _current(engine_for())
    names = [index["name"] for index in inspect(engine).get_indexes("document_versions")]
    assert names, "document_versions stopped carrying indexes"
    with engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {names[0]}"))

    migrate(engine)

    restored = [index["name"] for index in inspect(engine).get_indexes("document_versions")]
    assert sorted(restored) == sorted(names)


def test_an_index_it_cannot_create_is_reported_rather_than_thrown(engine_for):
    """A traceback out of the migration is a container that dies without a reason.

    deploy/entrypoint.sh runs this under `set -eu`. An uncaught exception here
    stops the deploy with a stack trace whose top frame is SQLAlchemy, and the
    operator has to read our source to find out that an index was the problem.
    Caught and reported, the same event names itself in one line.

    The collision is built rather than mocked: something else in the database has
    taken the name the index wants. That is what an operator's stray object, or a
    half-finished rename, actually looks like.
    """
    engine = _current(engine_for())
    name = inspect(engine).get_indexes("document_versions")[0]["name"]
    with engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {name}"))
        connection.execute(text(f"CREATE TABLE {name} (x INTEGER)"))

    report = migrate(engine)

    assert len(report["refused"]) == 1
    assert report["refused"][0].startswith(f"index {name}:")
    assert "already a table" in report["refused"][0], "the reason has to survive"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_the_report_keeps_tables_columns_and_refusals_apart(engine_for):
    """Three different events to whoever reads the deploy log.

    A new table is a feature arriving, a new column is a feature growing, and a
    refusal is something a person has to look at tonight. Flattened into one list
    the third disappears into the first two -- and "refused" is the key
    scripts/migrate.py turns into a non-zero exit, so folding anything else into
    it turns that thing into a stopped deploy.
    """
    engine = _yesterday(engine_for())

    report = migrate(engine)

    for key in ("tables", "columns", "refused"):
        assert isinstance(report[key], list), f"{key} is not a list"
    assert report["tables"] == ["passages"]
    assert report["columns"] == ["document_versions.source_url"]
    assert report["refused"] == []


def test_the_report_names_a_column_by_its_table(engine_for):
    """"source_url" alone is ambiguous the day a second table grows one."""
    engine = _yesterday(engine_for())

    report = migrate(engine)

    for entry in report["columns"]:
        assert "." in entry, f"unqualified column name in the report: {entry!r}"


def test_the_report_claims_no_column_it_did_not_add(engine_for):
    """A report is evidence only if the file agrees with it.

    Every name reported must be readable in the database afterwards. A migration
    that reports work it did not do is worse than one that stays silent, because
    the deploy log then says the site is fine.
    """
    engine = _yesterday(engine_for())

    report = migrate(engine)

    for entry in report["columns"]:
        table, _, column = entry.partition(".")
        assert column in _columns(engine, table), f"reported {entry} and did not add it"
    for table in report["tables"]:
        assert inspect(engine).has_table(table)


# ---------------------------------------------------------------------------
# The audit log, which is the one table that cannot survive a careless column
# ---------------------------------------------------------------------------


def test_a_deploy_migration_leaves_a_pre_attribution_log_verifying(tmp_path):
    """Fixed 2026-08-04. The strict xfail this carried is deleted, not flipped.

    migrate_audit_schema now runs BEFORE the generic column loop. Run after it,
    the loop added audit_events.digest_version as NULL, migrate_audit_schema
    then found the column present and skipped its own backfill, and verify_chain
    refused the entire log. The audit trail is the one artefact this product
    exists to keep, and its own deploy step was destroying it -- on the only
    path deploy/entrypoint.sh takes, while every unit test of the audit
    migration passed, because they called it on its own.

    The one row of data this product exists to defend, broken by the deploy.

    A live database written before attribution has audit rows hashed under scheme
    1. They carry no digest_version column, so the migration must mark them as
    scheme 1 -- that is what app/state/audit.py's own migration does, with
    DEFAULT 1 in the DDL, and its docstring is explicit that a row already there
    was written under scheme 1.

    Run through migrate() instead, which is the only path production takes, and
    the column arrives NULL. verify_chain then cannot tell which scheme hashed
    the row and refuses the entire chain rather than guessing -- correctly. The
    result is a citation-grade product whose audit page reports its own evidence
    as untrustworthy, on the first deploy after the column landed, with the rows
    themselves perfectly intact.
    """
    engine = _legacy_database(tmp_path)
    try:
        migrate(engine)

        with engine.begin() as connection:
            versions = [
                row[0]
                for row in connection.execute(
                    text("SELECT digest_version FROM audit_events ORDER BY seq")
                ).all()
            ]
        assert versions == [DIGEST_V1, DIGEST_V1], (
            "rows written under scheme 1 must say so; NULL makes them unverifiable"
        )

        session = sessionmaker(bind=engine, future=True)()
        try:
            assert verify_chain(session, "MEP") is True
        finally:
            session.close()
    finally:
        engine.dispose()


def test_the_migration_reaches_the_audit_table_at_all(tmp_path):
    """Whatever else is wrong above, the attribution columns must arrive.

    Without them every audit read on an existing database fails with "no such
    column", because the ORM names every mapped column in every SELECT. That is
    the same failure as document_versions.source_url and it is the reason
    migrate() must cover this table rather than leave it to a caller.
    """
    engine = _legacy_database(tmp_path)
    try:
        report = migrate(engine)

        present = _columns(engine, "audit_events")
        for name in ("actor_user_id", "actor_kind", "session_id", "ip", "reverts_event_id"):
            assert name in present, f"audit_events lost {name}"
            assert f"audit_events.{name}" in report["columns"]
    finally:
        engine.dispose()


def test_the_migration_does_not_rehash_an_audit_row(tmp_path):
    """A rewritten hash is indistinguishable from a tampered one, forever.

    The chain's whole value is that entry_hash was computed once, at the moment
    the event happened. A migration that recomputed it would produce a log that
    verifies perfectly and proves nothing, and there would be no way afterwards
    to tell which rows had been touched.
    """
    engine = _legacy_database(tmp_path)
    try:
        with engine.begin() as connection:
            before = connection.execute(
                text("SELECT seq, entry_hash, prev_hash, actor, occurred_at "
                     "FROM audit_events ORDER BY seq")
            ).all()

        migrate(engine)

        with engine.begin() as connection:
            after = connection.execute(
                text("SELECT seq, entry_hash, prev_hash, actor, occurred_at "
                     "FROM audit_events ORDER BY seq")
            ).all()
        assert after == before
    finally:
        engine.dispose()
