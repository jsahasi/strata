# Resume — state at 2026-08-05 09:20 UTC

**HEAD `f3b14f8`, pushed. Working tree clean. 2,039 passed, 2 xfailed.**
Repo public: https://github.com/jsahasi/strata · Live: https://strata.sudama.ai

Deadline is TODAY. Submission + 60-minute DeepInterview as one sitting.

## Score against "How we evaluate": 91/100

| Criterion | Wt | Score | Note |
|---|--:|--:|---|
| Working prototype ("the main event") | 30 | 28 | runs, live, `make fresh-check` green, `make reset-demo` exists |
| Four required capabilities | 25 | 23 | change→obligation join CLOSED today |
| Depth area | 10 | 10 | citation grounding; gate now refuses the model's own verdict |
| PRD | 10 | 9.5 | trust metrics + expansion both substantial (I under-scored these twice) |
| TDD | 10 | 9.5 | A10 adds build-method section + fan-out diagram |
| Not a thin wrapper | 10 | 10 | deterministic half runs with no key |
| Submission mechanics | 5 | 5 | public, unsquashed, template filled |

## NOT DEPLOYED — the one thing outstanding
HEAD is pushed but **not deployed**. Live site runs `68cbfa2`. To ship:
```
make publish-docs
rsync -a --delete -e "ssh -i ~/.ssh/citelocal_deploy" ./ root@143.198.140.28:/data/strata/src/ \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude strata.db --exclude .env
ssh -i ~/.ssh/citelocal_deploy root@143.198.140.28 'cd /data/strata/src && docker compose build strata-app && docker compose up -d'
rsync -a --delete -e "ssh -i ~/.ssh/citelocal_deploy" deploy/site/ root@143.198.140.28:/data/strata/site/
```
Key: `~/.ssh/citelocal_deploy`. Nothing else authenticates.

## Read before the interview
`docs/.ai/briefing.html` — rebuilt as a ten-minute read. Five files map, nine-step
walkthrough of `/changes/{id}`, verified numbers, uncomfortable questions, three stories.

**Five files decide everything:** `verifier.py::verify_citation` (151) ·
`claims.py::verified_claims` (502) · `queries.py::_require_scope` (60) /
`row_for_company` (113) · `audit.py::record_event` (515) / `verify_chain` (668) ·
`diff/engine.py::diff` (78). The two smallest matter most.

## Numbers that survive checking
2,039 tests · 93% coverage / 13,073 statements · 85 ADRs · `make eval` 5 of 5 ·
retrieval 0.5–1.1ms vs 357–364ms on 8,707 passages · Kentucky pair 1,024,409 vs
1,024,536 chars, 127 apart, 144 changes · 102 filings, 8 commissions, 19 dockets ·
26 change→obligation mappings (24 proposed, 2 confirmed), all resolving an owner.

**DO NOT SAY:** "101 of 176 vs the old suites' 2" — the 2 was test *files*. True
version: an 18-deletion audit where hand-written files caught 4; a separate
176-deletion campaign where the derived rule caught 78 per-function, 101 per-query.
No mutation harness was committed, so nothing reproduces these.

**Glass contrast is 16.41:1 → 16.73:1**, not 15.84 → 16.15. I measured the first
pair against an ink colour not in the stylesheet.

## Open, ranked
1. **Deploy** (above).
2. `resolve_change_owner` ignores `mapped_by_kind` — a proposed mapping routes like
   a confirmed one everywhere except the change screen. Needs a 16th refusal code.
   ADR-85 names it.
3. 32 call sites still read the real clock (`tests/test_clock_pinned.py`, strict xfail).
4. `restore()` in backup.py says "Nothing was written" and can leave a partial file
   at the live DB path. ADR-77.
5. Missing vocabulary: `obligation.map` permission, `obligation.confirmed` audit code.
6. Zero user interviews. 45 messages, 0 replies, 4 bounces. Unrecoverable in time.

## Standing lessons
- Agents ignored "do not commit/deploy" **four times**. Put it in every brief AND
  verify with `git log` after.
- `git add docs/` swept an unreviewed agent edit into my commit. Stage explicit paths.
- Every audit that worked read **code**, never another document.
- Four of six modules had docstrings claiming controls their code did not keep.
