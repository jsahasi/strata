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
  echo "strata: using the database already at $DB. Not seeding the corpus."
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

# --------------------------------------------------------------------------
# The rest of the demonstration, and it runs on EVERY start rather than only on
# a fresh database. That is a deliberate departure from the rule above and the
# reasoning is worth reading before anybody tightens it back.
#
# WHAT WENT WRONG WITHOUT THIS. The block above lays down the corpus, the claims
# and the accounts. It lays down no obligations, no source registrations, no
# approval route and no real filings -- those arrived later in four scripts, and
# nothing here called any of them. So the hosted site served an owner-handoff
# screen with nobody to hand to, a blank Integrations page, an empty approval
# route, and a workspace that carried none of the 102 real filings the
# documentation said were in it. Every one of those features worked. None could
# demonstrate. `make seed` was fixed for the same reason; this is the container
# half of the same defect, and it is worse, because a reviewer opening
# strata.sudama.ai cannot run a Makefile.
#
# WHY RUNNING EVERY TIME IS SAFE HERE, AND WHY IT DOES NOT BREAK THE RULE ABOVE.
# That rule exists to stop a redeploy dropping the audit chain. None of these
# four drops anything. Each is additive and each is guarded -- they print
# "already present" and change nothing on a second run, which was checked by
# running them three times against one database and counting rows, not by
# reading the code. Running them on every start is also the only thing that
# repairs a database seeded before they existed, which is exactly the state the
# live one is in.
#
# GATED ON STRATA_DEMO_ACCOUNTS, because this is demonstration content. A
# deployment holding a customer's documents sets that to 0 to keep the published
# passwords off the login page, and the same switch must keep invented
# obligations and a fictional approval route out of their workspace. One setting,
# one meaning: "this workspace is a demonstration".
if [ "$(printf '%s' "${STRATA_DEMO_ACCOUNTS:-1}" | tr 'A-Z' 'a-z')" = "0" ]; then
  echo "strata: STRATA_DEMO_ACCOUNTS is 0, so no demonstration content was laid down."
else
  for script in seed_demo_gaps seed_route ingest_real; do
    if python "scripts/$script.py"; then
      echo "strata: scripts/$script.py ok."
    else
      # Not fatal. A demonstration that is missing its approval route is worth
      # serving; a site that will not start because a seed script failed is not.
      # It says which one, so the gap on screen has a name in the log.
      echo "strata: scripts/$script.py FAILED. The screens it fills will be empty."
    fi
  done
fi

# --------------------------------------------------------------------------
# Periodic work.
#
# NOTHING RUNS ON A CLOCK UNLESS STRATA_JOBS_ENABLED IS "true". That is the
# default and it is the safe one: the retention purges can delete a customer's
# rows and the approval clock can BYPASS AN APPROVAL, so neither may start
# because somebody deployed an image.
#
# The banner runs either way, and that is the point of it. A reviewer reading
# this container's log has to be able to tell "no job is scheduled" from "a job
# is scheduled and has not fired yet", and a silent boot reads as the second. So
# app/jobs/runner.py prints one line per known job, on or off, plus the mode
# retention is in -- dry or deleting -- on EVERY start, whichever way it landed.
#
# The banner exits non-zero when the configuration names a job this build does
# not have. In that case nothing is started and the line below says so, rather
# than starting the jobs that happen to be spelled correctly: an operator who
# asked for four jobs and silently got three has a scheduler they cannot read the
# enabled list of.
if python scripts/run_jobs.py --banner; then
  if [ "$(printf '%s' "${STRATA_JOBS_ENABLED:-}" | tr 'A-Z' 'a-z')" = "true" ]; then
    # A separate process rather than a thread in the application, because this
    # image's entrypoint is the only thing here that can start it -- and it is
    # the better shape anyway: a sweep cannot slow a request, and the two cannot
    # sweep at once because both take the same SQLite lock beside the database.
    #
    # NOTHING SUPERVISES IT. If it dies, the site keeps serving and the only sign
    # is that the "strata.jobs:" lines stop. The pid is printed so a reader can
    # check it is still there.
    python scripts/run_jobs.py --loop &
    echo "strata: the job loop is running as pid $!. Nothing restarts it if it dies."
  else
    echo "strata: STRATA_JOBS_ENABLED is not \"true\", so no job loop was started in this container."
  fi
else
  echo "strata: THE JOB CONFIGURATION WAS REFUSED (above). Nothing is scheduled in this container."
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
