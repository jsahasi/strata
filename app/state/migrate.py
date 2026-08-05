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

ONE EXCEPTION, AND IT IS NOT A BACKFILL. The passage index is derived data, and
derived data has the opposite rule: it migrates all at once or not at all
(best-practices.html section 27), because half of it answers every query
plausibly and wrongly. So this file calls a REBUILD -- discard, recompute from
the stored text, whole, in one transaction -- and never a fill-in-the-missing.
Nothing here writes a value into an existing row, which is the promise above and
it still holds. See the block at the foot of migrate().

Safe to run on every start, and it must be: an idempotent migration nobody calls
is the same as no migration. It reports what it did rather than assuming.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex

from app.state.audit import migrate_audit_schema
from app.state.models import Base
from app.state.search import ensure_passage_index


def _ddl_type(engine, column) -> str:
    """Render a model column's type as this dialect's DDL."""
    return column.type.compile(dialect=engine.dialect)


def migrate(engine) -> dict[str, list[str]]:
    """Make the database match the models, additively. Returns what changed.

    The four keys are separate because they are different events to a reader:
    a new table is a feature arriving, a new column is a feature growing, a
    refusal is something a person has to look at, and "index" is derived data
    that was recomputed -- which changes no record and stops no deploy. Only
    "refused" is fatal to a caller; see the block that fills "index" for why
    the two must not be folded together.
    """
    report: dict[str, list[str]] = {
        "tables": [],
        "columns": [],
        "refused": [],
        "index": [],
    }

    inspector = inspect(engine)
    before = set(inspector.get_table_names())

    # Creates what is missing and leaves what exists untouched. It does NOT add
    # a column to a table it already sees, which is the whole reason for the
    # loop below.
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    report["tables"] = sorted(set(inspector.get_table_names()) - before)

    # THE AUDIT CHAIN FIRST, BEFORE THE GENERIC COLUMN LOOP BELOW, and the order
    # is the fix rather than a preference. migrate_audit_schema backfills
    # digest_version to 1 and builds two indexes. The loop below adds columns
    # from the model with no server_default rendered, so run second it would
    # find digest_version missing, add it NULL, and migrate_audit_schema would
    # then see the column present, return () and never backfill. verify_chain
    # refuses the whole log at that point -- "seq 1 claims digest scheme None,
    # which this build cannot compute" -- which is the audit trail, the one
    # artefact this product exists to keep, destroyed by its own deploy step.
    # tests/test_audit_v2.py passed throughout, because it calls
    # migrate_audit_schema on its own and that path was always correct. Only the
    # order the entrypoint actually takes was wrong.
    report["columns"].extend(f"audit_events.{name}" for name in migrate_audit_schema(engine))

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

    # The passage index. NOT A BACKFILL, and the distinction is why it is
    # allowed to be here at all: nothing above ever writes a value into an
    # existing row, and neither does this. It discards a derived artefact and
    # computes it again from the stored text, whole, in one transaction --
    # which is what best-practices.html section 27 asks of derived data, and the
    # opposite of what a partial backfill would do to it.
    #
    # It is here rather than left to a person because of section 29: built is
    # not connected. deploy/entrypoint.sh runs scripts/migrate.py on every start,
    # so this is the one call the deploy path already makes. An index nothing
    # ever builds announces itself missing on every query for the life of the
    # product -- honest, and useless.
    #
    # It rebuilds only when the index is absent or does not cover the corpus, so
    # a start that changes nothing costs two counts.
    #
    # ITS OUTCOME GETS ITS OWN KEY AND NEVER "refused", which reads like a
    # detail and is the whole safety of putting it here. scripts/migrate.py
    # EXITS NON-ZERO on anything in "refused", and deploy/entrypoint.sh stops
    # the deploy on that -- correctly, because a schema the code cannot read is
    # an outage either way. An index is not that. Without it retrieval answers
    # from a complete scan and says so on every query, so a database with no
    # FTS5 should serve a slower product rather than no product, and folding
    # this into "refused" would turn an optimisation into an outage.
    #
    # THE ORDER MATTERS ON A FIRST DEPLOY and this file cannot fix it alone.
    # entrypoint.sh migrates before it seeds, so on the very first start the
    # corpus is still empty here and the index is built over nothing. The seed
    # then lands, and retrieval would report the index stale on every query
    # until the next start rebuilt it -- answers complete throughout, and
    # unranked for a whole deploy. `make seed` runs scripts/build_index.py after
    # the corpus lands, which closes it on the local path; entrypoint.sh runs
    # the seed inline rather than through make, so the first hosted start is
    # still one restart behind. That is a deploy file, not this one.
    index = ensure_passage_index(engine)
    report["index"] = list(index["refused"])
    if index["built"]:
        report["index"].append(f"passage index rebuilt over {index['built']} passages")

    return report
