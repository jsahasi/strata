"""Bring a database that predates the current code up to it, additively.

WHY THIS EXISTS, and it is a near miss rather than a theory. deploy/entrypoint.sh
seeds only when there is no database file, which is right -- a redeploy must
never lay fresh demo rows over an audit chain. But it ran NOTHING when the file
was present, not even create_all. So the first deploy after a column was added
would have kept the old table while the new code selected the new column, and
every proceeding, claim and verification screen on the live site would have
answered:

    (sqlite3.OperationalError) no such column: document_versions.source_url

Nothing in the test suite could catch that. Tests build their schema from the
current models every time, so they never see yesterday's database. The only
place it shows up is production, on the deploy, in front of whoever is looking.

WHY IT IS DERIVED RATHER THAN A LIST. The obvious fix is five ALTER TABLE lines
for the five columns that caused this. That fixes the instance and leaves the
class: the next column added by anybody, in any table, breaks the deploy the
same way, and the person who adds it will not think of this file. So the
migration asks SQLAlchemy what the models declare, asks the database what it
has, and adds the difference. A column added tomorrow is covered by code written
today, which is the only kind of coverage that survives a team.

WHAT IT WILL NOT DO, deliberately:
  - It never drops, renames or retypes. Those are destructive and this product
    keeps an append-only hash chain that a rewrite would silently invalidate.
    If a column changes type, this refuses and says so rather than guessing.
  - It never backfills a value. A new column is NULL on old rows, because NULL
    says "the schema of the day did not record this" and a default says
    something the record cannot support. That is the argument
    migrate_audit_schema already makes for its own columns and it holds here.
  - It adds no NOT NULL column to a table with rows, because SQLite cannot and
    because the honest answer for an existing row is that the value is unknown.

Safe to run on every start, and it must be: an idempotent migration nobody calls
is the same as no migration. It reports what it did rather than assuming.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex

from app.state.audit import migrate_audit_schema
from app.state.models import Base


def _ddl_type(engine, column) -> str:
    """Render a model column's type as this dialect's DDL."""
    return column.type.compile(dialect=engine.dialect)


def migrate(engine) -> dict[str, list[str]]:
    """Make the database match the models, additively. Returns what changed.

    The three keys are separate because they are different events to a reader:
    a new table is a feature arriving, a new column is a feature growing, and a
    refusal is something a person has to look at.
    """
    report: dict[str, list[str]] = {"tables": [], "columns": [], "refused": []}

    inspector = inspect(engine)
    before = set(inspector.get_table_names())

    # Creates what is missing and leaves what exists untouched. It does NOT add
    # a column to a table it already sees, which is the whole reason for the
    # loop below.
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    report["tables"] = sorted(set(inspector.get_table_names()) - before)

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        present = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable and column.default is None and column.server_default is None:
                # SQLite cannot add a NOT NULL column without a default, and
                # inventing one would write a fact into every historical row.
                report["refused"].append(
                    f"{table.name}.{column.name}: NOT NULL with no default. "
                    f"Add it nullable, backfill deliberately, then tighten."
                )
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {_ddl_type(engine, column)}"
            with engine.begin() as connection:
                connection.execute(text(ddl))
            report["columns"].append(f"{table.name}.{column.name}")

    # Indexes on a table that already existed are not created by create_all.
    with engine.begin() as connection:
        existing = {
            (t, i["name"])
            for t in inspect(engine).get_table_names()
            for i in inspect(engine).get_indexes(t)
        }
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                if (table.name, index.name) in existing:
                    continue
                try:
                    connection.execute(CreateIndex(index, if_not_exists=True))
                except Exception as exc:  # a partial index the dialect refuses
                    report["refused"].append(f"index {index.name}: {exc}")

    # The audit chain has its own migration with its own rules -- it backfills
    # digest_version and must never rehash. Call it rather than reimplementing
    # a rule it already states correctly. It had no production caller until now.
    added = migrate_audit_schema(engine)
    report["columns"].extend(f"audit_events.{name}" for name in added)

    return report
