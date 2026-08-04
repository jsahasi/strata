# Strata

Strata turns a regulatory proceeding into tracked work. It ingests successive versions of a
docket plus a company's obligations, projects, and documents; finds what changed between
versions; and turns each material change into a cited, routed action. Every claim it makes
carries a source citation, and that citation is checked against the original text before the
claim is ever shown as fact — an unverified claim goes to a review queue instead of the page.
Built for the AI Fund Engineer-in-Residence Build Challenge, deadline 48 hours from 2026-08-03.

**The one user.** A regulatory affairs analyst at a multi-state investor-owned utility — the
person who reads the docket, works out what changed, and tells the business what it must do.
Not counsel, not the compliance officer, not an executive. See ADR-001 in
[`docs/.ai/decisions.html`](docs/.ai/decisions.html) for why, and what would overturn it.

---

## Status of this build

**It runs.** `app/` holds 40 Python modules, `tests/` holds a suite that passes offline, and
`data/` holds the synthetic corpus — one proceeding in three versions, a company context, and a
manifest of ground truth. `make run`, `make test` and `make eval` all work on a clean checkout,
and all three were executed as this sentence was written rather than assumed.

No test count is written here, because a number in prose goes stale the next time somebody adds
a test. `make test` prints the current one, and `docs/.ai/state.json` is generated from the
repository by `make status` for the same reason.

What is built and covered by tests: ingestion with exact source offsets, version diffing,
citation verification with the occurrence rule, confidence-gated escalation, a hash-chained
audit log, tenant isolation, logins with roles and permissions, and an offline eval harness.

What the names promise and the code does not, stated here rather than left to be discovered:

- **No model runs anywhere.** `anthropic` is pinned in `requirements.txt` and imported by
  nothing under `app/`. Extraction is deterministic assembly from the seed. The evidence
  *links* are real and enforced — a claim whose citation fails genuinely refuses to assert
  itself — but nothing is extracted by a model, so this is the linking without the extraction.
- **Rollback does not exist.** The history half is solid and tamper-evident; the undo half was
  never built.
- **Reviewer routing is a shared queue, not routing.** Everything held back lands in one pile
  anyone in the company can pick from.

The same three gaps, with the eight areas that do work, are tabulated in
[`docs/mrd.html`](docs/mrd.html) under "What the submission has to cover, in plain English".
`docs/.ai/tasks.html` carries the current state of each task. Do not trust adjectives in this
file over the board; the board is the source of truth on what exists.

---

## Run it

```
make run
```

**Contract:** installs dependencies into a local virtual environment, initializes a SQLite
database, loads the synthetic corpus from `data/` if the database is empty, seeds seven demo
accounts, and starts the server at `http://localhost:8000`. No other service, no container, no
signup, no API key.

Sign in with any of the seeded addresses — `denise.okoro@mep.example` is the analyst ADR-001
is written for — and the password `strata-demo-2026`. Every account shares that password, it
is printed by the seed and shown on the login page, and it is a demo downgrade rather than a
design: a real deployment sets `STRATA_DEMO_ACCOUNTS=0` and seeds its own.

## Test it

```
make test
```

**Contract:** runs the full pytest suite offline. No network call is required for a passing
run, and no API key. Citation verification, version diffing, and draft-versus-final handling
are deterministic and asserted against known answers in the synthetic corpus, not against the
code's own output.

## Score it

```
make eval
```

Prints a scorecard over the deterministic spine — the verifier, the diff, the occurrence
check, the draft/final routing table — computed against `data/manifest.json`, whose offsets
were produced by a separate script and verified against the bytes. It opens no socket and
needs no key. It exits 1 when any blocking metric fails, so it is usable as a gate rather than
as something a human reads and forgets. Read its closing caveat before quoting any number: the
corpus labels five changes, and the harness refuses to print a percentage over a denominator
that small.

## Two more targets

```
make seed          # load the synthetic proceeding and company context
make fresh-check   # clone HEAD to a temp dir and prove the suite passes there
```

`make run` calls `make seed` for you. Running the seed again changes nothing — it is
idempotent by design, and a test asserts it.

---

## Prerequisites

- **Python 3.12.** No other language runtime, no Node build step — the UI is server-rendered
  HTML (ADR-007, reaffirmed as ADR-012).
- **No external services.** No Postgres, no Docker, no message queue. SQLite ships with Python.
  This is a deliberate trade against scale, written up in ADR-007: SQLite will not carry real
  multi-tenant volume, and `docs/architecture.html` says what changes at that point.
- **No API key, for anything currently built.** Ingestion, citation verification, version
  diffing and the eval harness all run offline. `requirements.txt` pins the `anthropic` client
  against the interpretation layer described in `docs/architecture.html`; that layer is not
  built, nothing imports the package, and this file names no environment variable for it
  because there is none to name.

---

## Where to look first

Start with citation verification. It is the hardest technical decision in this build and the
one the product is designed to make visible rather than bury (ADR-003, ADR-013). The claim
being tested: a claim whose citation does not check out against the stored source text must
never be shown as established fact — it degrades to a review-queue item labelled unverified,
never to a confident guess.

The reviewer path, in the running product:

1. Open `http://localhost:8000/changes/CHG-v1-v2-003`. This change carries two claims. One
   verifies; click its citation chip and the exact source span highlights in the original
   document text.
2. The second claim on that same change is a deliberate misquote at real offsets. It does not
   render as a claim at all — the page has no sentence to show, because a withheld claim
   carries no statement field to leak. Compare the two side by side: that pairing is the
   demonstration, not the green one on its own.
3. Open `http://localhost:8000/escalations`. Both refusals are there with the verifier's own
   reason — one misquote, one quote that appears three times and did not say which occurrence
   it meant.
4. Compare that to the deterministic version diff behind it (ADR-004) and the draft-versus-final
   status on each version (ADR-005) — those two are what the citation is a claim about.

After that: the decision log ([`docs/.ai/decisions.html`](docs/.ai/decisions.html)) answers
"why this and not the alternative" for every choice above, and
[`docs/.ai/findings.html`](docs/.ai/findings.html) records every defect found during the build,
including the two a fuzzer found in code that already looked finished.

---

## The synthetic corpus

`data/` holds one proceeding in three versions — draft, revised draft, final — with deliberate
edits: one change is material, one is cosmetic, one appears only in the final version, and one
moves a deadline. A fifth edit restructures a section so the diff has to escalate rather than
guess. Alongside it sits a company context of obligations, projects, and documents. Every edit
is written down in `data/manifest.json` as ground truth, so tests assert against a known answer
rather than an impression. `data/README.md` describes the authored edits.

Nothing in `data/` is a real filing and no real company appears in it. The people named in
`docs/mrd.html`'s outreach log are real and filed in public proceedings; the corpus is not.

---

## Documents

| Doc | Purpose |
|---|---|
| [`docs/.ai/decisions.html`](docs/.ai/decisions.html) | Decision log, ADR-001 through ADR-023. Every choice with its alternatives and the trade-off accepted. Written when the decision is made. |
| [`docs/.ai/findings.html`](docs/.ai/findings.html) | Every defect found during the build, how it was found, what was done, and which test now guards it. Including the three that were false claims in documents. |
| [`docs/.ai/briefing.html`](docs/.ai/briefing.html) | The rubric, an honest assessment against each of its four dimensions, and the limits to concede before being asked. |
| [`docs/.ai/tasks.html`](docs/.ai/tasks.html) | The 48-hour build board. Current state of every task. |
| [`docs/prd.html`](docs/prd.html) | Product requirements. One analyst, one workflow, citation-grade change intelligence. |
| [`docs/mrd.html`](docs/mrd.html) | Market requirements, the outreach log, and what the submission has to cover with where each area stands. |
| [`docs/tdd.html`](docs/tdd.html) | Technical design. How the hard bet is built, alternative rejected, trade-off accepted at each step. |
| [`docs/architecture.html`](docs/architecture.html) | System architecture and data flow. Components, module boundaries under `app/`, and what changes past SQLite/single-node. |
| [`docs/security.html`](docs/security.html) | Security posture. Threat model, tenant isolation via the company_id chokepoint, the audit chain, and what the demo downgrades. |
| [`docs/user-research.html`](docs/user-research.html) | The interview plan, the four hypotheses, and what changed in the build because of what was heard. The interview table is still empty — no conversation has happened. Who was contacted, and when, is in `docs/mrd.html`. |
| [`docs/future-enhancements.html`](docs/future-enhancements.html) | What is deliberately out of scope for 48 hours. Roadmap preconditions and unlocks past the build. |
| [`docs/submission.html`](docs/submission.html) | What was built, what was reused, what the AI wrote versus what got rewritten or rejected. Kept current as the build proceeds, not reconstructed at the end. |
| [`docs/best-practices.html`](docs/best-practices.html) | Portable engineering playbook carried over from the peer project. Sections 26 and 27 are directly load-bearing here. |

All documents listed above exist in the repository.

---

## Layout

```
app/        application code, 40 modules
  text/         normalization and the projection back to raw offsets
  ingestion/    raw text in, hashed version plus offset-addressed passages out
  diff/         two passage sequences in, typed changes out; no model
  verification/ the citation verifier and the occurrence rule
  interpretation/ the draft-versus-final routing table
  state/        models, the tenant chokepoint, claims, review, audit, identity
  auth/         sessions, and the policy that reads authorship off the chain
  evals/        the offline scorecard
  web/          FastAPI routers, Jinja templates, one stylesheet, one script
tests/      pytest; offline by default, no API key
data/       three proceeding versions, company context, manifest of ground truth
docs/       prd.html mrd.html tdd.html architecture.html security.html
            best-practices.html future-enhancements.html user-research.html
            submission.html web-design.html
docs/.ai/   decisions.html (ADRs) findings.html briefing.html tasks.html
```
