# Resume — state at 2026-08-05, after the final deploy

**HEAD pushed AND deployed. 2,301 passed, 2 xfailed. `make fresh-check` passes on a clone in 310s, exit zero.**
Repo public: https://github.com/jsahasi/strata · Live: https://strata.sudama.ai
`/healthz` returns `{"status":"ok","corpus_loaded":true,...,"changes":171}`.

Deadline is TODAY. Submission + 60-minute DeepInterview as one sitting.

## Score against "How we evaluate": 97/100

| Criterion | Wt | Score | Note |
|---|--:|--:|---|
| Working prototype ("the main event") | 30 | 29 | live at HEAD; `make fresh-check` green; `make reset-demo`; last clock-dependent flake gone |
| Four required capabilities | 25 | 24 | change→obligation join now routes honestly (ADR-87) |
| Depth area | 10 | 10 | citation grounding; the gate refuses the model's own verdict |
| PRD | 10 | 9.5 | trust metrics + expansion both substantial |
| TDD | 10 | 9.5 | build-method section + fan-out diagram |
| Not a thin wrapper | 10 | 10 | the deterministic half runs with no key |
| Submission mechanics | 5 | 5 | public, unsquashed, template filled |

The earlier file said 91 in its header while its own table summed to 95 — I revised
two rows and did not revise the total. The table was right both times.

## What the last commit changed
1. **`ROUTE_MAPPING_UNCONFIRMED`, the sixteenth refusal code (ADR-87).**
   `resolve_change_owner` never read `mapped_by_kind`, so a mapping the pipeline
   guessed from a word overlap assigned work to a named person exactly as a
   person-confirmed one did. The would-be owner now goes in `candidate_user_ids`,
   never `user_id` — and since the new code is not in `ROUTE_OK_CODES`, the
   existing invariant does the enforcing. `ROUTE_PENDING_ACCEPTANCE` converts too,
   which cost nine `test_invites` failures that were fixed, not exempted.
2. **The materiality verdict is stored and audited** under `ACTION_MATERIALITY_SET`
   (ADR-86). The model was re-judging on every render. The citation is still
   re-verified on every read, so this is not the cached verdict ADR-003 refuses.
3. **Thirty-two clocks pinned, strict xfail deleted.** Tests asserting a default
   (a fresh session is live) are named in `ALLOWED` with the reason rather than
   pinned. One of these fired at 09:00 yesterday; the next would have shown a
   reviewer a red suite.

## Read before the interview
`docs/.ai/briefing.html` — a ten-minute read. Five-file map, nine-step walkthrough
of `/changes/{id}`, verified numbers, uncomfortable questions, three stories.

**Five files decide everything:** `verifier.py::verify_citation` (151) ·
`claims.py::verified_claims` (502) · `queries.py::_require_scope` (60) /
`row_for_company` (113) · `audit.py::record_event` (515) / `verify_chain` (668) ·
`diff/engine.py::diff` (78). The two smallest matter most.

## Numbers that survive checking
2,301 tests · 90 ADRs · 82 modules · 84 commits · `make eval` 5 of 5 ·
retrieval 0.5–1.1ms vs 357–364ms on 8,707 passages · Kentucky pair 1,024,409 vs
1,024,536 chars, 127 apart, 144 changes · 102 filings, 8 commissions, 19 dockets ·
26 change→obligation mappings (24 proposed, 2 confirmed) · 16 refusal codes.

**DO NOT SAY:** "101 of 176 vs the old suites' 2" — the 2 was test *files*. True
version: an 18-deletion audit where hand-written files caught 4; a separate
176-deletion campaign where the derived rule caught 78 per-function, 101 per-query.
No mutation harness was committed, so nothing reproduces these.

**Glass contrast is 16.41:1 → 16.73:1**, not 15.84 → 16.15. I measured the first
pair against an ink colour not in the stylesheet.

## Open, ranked — all of these are known and none is on the demo path
1. **`restore()` in `backup.py` says "Nothing was written" and can leave a partial
   file.** A refusal that is not true is worse than no refusal. Highest-value
   remaining fix and the one I would name first if asked what is broken.
2. **Confirming a mapping is a database write with no screen.** ADR-87 converts a
   wrong answer into a dead end until that screen exists. Named as a cost there.
3. **Nothing forces `mapped_by_kind` to be set at write time**, so ADR-87's refusal
   is only as good as the column.
4. **No test asserts every one of the 16 refusal codes has words a person can read.**
5. **`AnthropicTransport` is unexercised** — every test drives a deterministic fake.

## Deploy (already done; here for the next time)
```
make publish-docs
rsync -a --delete -e "ssh -i ~/.ssh/citelocal_deploy" ./ root@143.198.140.28:/data/strata/src/ \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude strata.db --exclude .env
ssh -i ~/.ssh/citelocal_deploy root@143.198.140.28 'cd /data/strata/src && docker compose build strata-app && docker compose up -d'
rsync -a --delete -e "ssh -i ~/.ssh/citelocal_deploy" deploy/site/ root@143.198.140.28:/data/strata/site/
```
Key: `~/.ssh/citelocal_deploy`. Nothing else authenticates. `/healthz` 502s during
seed on a cold boot — wait for it, it is not a failure.
