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

.PHONY: help venv install run test eval seed clean fresh-check status

help:
	@echo "make run    - start the workspace at http://localhost:$(PORT)"
	@echo "make test   - run the test suite"
	@echo "make eval   - run the extraction and citation evals, print the scores"
	@echo "make seed   - load the synthetic proceeding and company context"
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

seed: install
	$(PY) -m app.seed

# The one file in docs/.ai/ nobody writes by hand, so it cannot go stale.
status: install
	$(PY) scripts/status.py

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
