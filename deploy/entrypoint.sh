#!/bin/sh
# Start the application, seeding the database only if there is not one.
#
# THE RULE THIS SCRIPT EXISTS TO ENFORCE: a redeploy must never reseed. The
# audit chain is append-only and hash-linked, and dropping it to lay down demo
# rows would destroy the only evidence the product actually offers -- silently,
# and at exactly the moment somebody is looking. So the seed runs when the file
# is absent and never otherwise, and it says which of the two happened.
set -eu

# app/state/db.py reads STRATA_DATABASE_URL, a SQLAlchemy URL -- not a path.
# The file is derived from it so the "seed only when absent" check looks at
# the same database the application will open.
DB="${STRATA_DB_FILE:-/data/strata.db}"

# ALWAYS, before anything reads a table, and before the seed. A migration that
# runs only against a fresh database is the same as no migration: the deploy
# that adds a column is exactly the deploy where the old table is still there.
# This script came within one redeploy of taking the live site down with
# "no such column: document_versions.source_url" on every screen, because the
# branch below runs nothing at all when the file exists.
#
# Additive only. It never drops, renames, retypes or backfills, and it exits
# non-zero if it has to refuse -- so a deploy stops rather than starting the
# application against a schema it cannot read.
python scripts/migrate.py

if [ -f "$DB" ]; then
  echo "strata: using the database already at $DB. Not seeding."
else
  echo "strata: no database at $DB. Creating and seeding the demonstration corpus."
  python - <<'PY'
from app.state.db import init_db, session_scope
from app.seed import load, ensure_accounts

init_db(drop_first=False)
with session_scope() as session:
    load(session)
    ensure_accounts(session)
print("strata: seeded.")
PY
fi

# One worker, on purpose. SQLite takes a write lock across the file, so a second
# worker buys concurrency the database cannot honour and turns a slow write into
# a locked one. This is the ADR-28 trade-off arriving in the deployment: the
# ceiling is real and it is documented rather than papered over with workers.
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --workers 1 \
  --proxy-headers --forwarded-allow-ips='*' \
  --no-server-header
