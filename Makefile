# Strata — the two commands AI Fund executes before scheduling a panel.
#
# Keep both working from a FRESH CLONE at all times. A reviewer who cannot start
# the prototype does not read the rest of the submission, so `make run` breaking
# is the only bug in this repository that is fatal rather than embarrassing.
#
# Deliberately no Docker, no database service, no build step: every dependency a
# reviewer has to install first is a chance for the submission to fail on someone
# else's machine (see ADR-007 in docs/.ai/decisions.html).

PY := .venv/bin/python
PIP := .venv/bin/pip
PORT ?= 8000

.PHONY: help venv install run test eval seed reset-demo reset-demo-dry clean fresh-check status

help:
	@echo "make run    - start the workspace at http://localhost:$(PORT)"
	@echo "make test   - run the test suite"
	@echo "make eval   - run the extraction and citation evals, print the scores"
	@echo "make seed   - load the synthetic proceeding and company context"
	@echo "make reset-demo - put the demonstration tenant back the way it shipped"
	@echo "make reset-demo-dry - show what reset-demo would remove, and change nothing"
	@echo "make fresh-check - clone this repo to a temp dir and prove run+test work there"
	@echo "make status - regenerate docs/.ai/state.json from the repo itself"

.venv:
	python3.12 -m venv .venv || python3 -m venv .venv

venv: .venv

# Dependencies are installed once and stamped. Without the stamp every `make
# test` re-resolves the requirements before printing anything, and a reviewer
# watches a blank terminal wondering whether the command hung. Silence during a
# long command reads as a broken command.
install: .venv/.deps-installed

.venv/.deps-installed: requirements.txt | .venv
	@echo "==> installing dependencies (a minute or so on a fresh clone)"
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt
	@touch $@
	@echo "==> dependencies ready"

# One command, end to end: install, load the corpus, serve.
run: install seed
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

test: install
	@echo "==> running the suite (1100+ tests, about four minutes)"
	$(PY) -m pytest tests/ -q

eval: install seed
	$(PY) -m app.evals.run

# THE WHOLE DEMONSTRATION, NOT HALF OF IT. app.seed lays down the corpus, the
# claims and the accounts. It does NOT lay down the obligations, the source
# registrations or the approval route -- those arrived later, in two scripts
# nothing called, so `make run` served a product whose owner-handoff screen had
# nobody to hand to and whose approval route was empty. Every one of those
# features works and none of them could demonstrate.
#
# A reviewer runs this command once and judges what they see. Three lines here
# are cheaper than the sentence explaining why the screens are hollow.
#
# Order matters: seed_route needs the accounts app.seed creates, and
# seed_demo_gaps needs the proceeding. Each is idempotent, so running `make
# seed` twice is safe.
#
# THE LAST TWO BOTH READ WHAT EVERY STEP ABOVE THEM WROTE, and both used to sit
# in the wrong place.
#
# build_index rebuilds the retrieval index whole from the passages, so anything
# ingested after it is not in it. Retrieval says so rather than answering short
# -- it reports the index stale and falls back to reading every passage -- but a
# reviewer running `make run` should see the built thing, not the announcement
# of the unbuilt one.
#
# seed_demo_gaps is the same shape and it was ahead of ingest_real, which made
# `make seed` give a different answer the second time it ran. Its mapping pass
# compares every stored change against the company's obligations; run before the
# real corpus is ingested, it saw 27 changes, and run again on the same database
# it saw 171 and wrote more rows. A seed whose output depends on how many times
# it has run is a seed nobody can reason about, and the fix is the same one
# build_index already had: read after every writer has written.
seed: install
	$(PY) -m app.seed
	$(PY) scripts/seed_route.py
	$(PY) scripts/ingest_real.py
	$(PY) scripts/seed_demo_gaps.py
	$(PY) scripts/build_index.py

# Put the demonstration tenant back the way it shipped.
#
# WHY IT EXISTS. deploy/site/index.html publishes three accounts and their
# password on purpose, and that argument -- a door held open, not a secret kept
# badly -- only holds while the room behind it is disposable. Anybody who signs
# in as the administrator can add users, redraw the approval route, move the
# threshold and mint share links, and every one of those is permanent, because
# the audit chain is append-only and nothing else in this product removes a row.
# This is the way back, and without it the way back was deleting the database on
# the host by hand.
#
# It refuses unless STRATA_DEMO_ACCOUNTS says this workspace is a demonstration,
# it takes no --company argument so the only tenant it can touch is the one the
# seed itself creates, and it verifies every OTHER tenant's chain afterwards and
# exits non-zero if any of them moved. Run `reset-demo-dry` first if you want to
# see what would go. build_index runs after, for the reason the seed target
# gives above.
reset-demo: install
	$(PY) scripts/reset_demo.py --yes
	$(PY) scripts/build_index.py

reset-demo-dry: install
	$(PY) scripts/reset_demo.py --dry-run

# The one file in docs/.ai/ nobody writes by hand, so it cannot go stale.
status: install
	$(PY) scripts/status.py

# Copy docs/ into the site so strata.sudama.ai serves them at /docs/. Refuses if
# any internal link in the published copy goes nowhere -- it caught one on the
# first run, and a document served with a broken link is worse than one not
# served, because the reader blames the product rather than the deploy.
publish-docs:
	$(PY) scripts/publish_docs.py

# The check that matters: does this work for someone who is not you?
# Clones HEAD into a temp directory and runs the suite there.
fresh-check:
	@tmp=$$(mktemp -d) && \
	git clone -q . $$tmp/strata && \
	cd $$tmp/strata && \
	$(MAKE) test && \
	echo "fresh clone: tests pass at $$tmp/strata"

clean:
	rm -rf .venv strata.db .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
