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

**Nothing runs yet.** `app/`, `tests/`, and `data/` are empty directories. This README states the
contract the build must satisfy, per the 48-hour plan in
[`docs/.ai/tasks.html`](docs/.ai/tasks.html) (task T2: `make run` and `make test` must exist and
work on a clean checkout before any feature work, even against an empty app). It will be updated,
commit by commit, to say only what is true at each point — never what is planned.

Check `docs/.ai/tasks.html` for the current state of each task. Do not trust adjectives in this
file over the board; the board is the source of truth on what exists.

---

## Run it

```
make run
```

**Contract:** installs dependencies into a local virtual environment, initializes a SQLite
database, loads the synthetic corpus from `data/` if the database is empty, and starts the
server at `http://localhost:8000`. No other service, no container, no signup, no API key for the
deterministic parts of the system (see Prerequisites).

## Test it

```
make test
```

**Contract:** runs the full pytest suite offline. No network call is required for a passing run.
Citation verification, version diffing, and draft-versus-final handling are deterministic and
asserted against known answers in the synthetic corpus — see task T3 in
[`docs/.ai/tasks.html`](docs/.ai/tasks.html) for what those known answers are.

Both targets are unimplemented as of this commit. Neither has been run on a clean checkout.
Marking that honestly here rather than claiming otherwise is the point of this section.

---

## Prerequisites

- **Python 3.12.** No other language runtime, no Node build step — the UI is server-rendered
  HTML (ADR-007).
- **No external services.** No Postgres, no Docker, no message queue. SQLite ships with Python.
  This is a deliberate trade against scale, written up in ADR-007: SQLite will not carry real
  multi-tenant volume, and the architecture doc says what changes at that point.
- **Deterministic components need no API key.** Ingestion, citation verification, and version
  diffing run entirely offline and are what the test suite exercises.
- **Model-backed components need an LLM.** Materiality interpretation and obligation extraction
  call a language model. Which provider, and what the offline fallback is when it is unavailable,
  is an open decision — see "To be decided" at the foot of
  [`docs/.ai/decisions.html`](docs/.ai/decisions.html). This README will name the exact
  environment variable once that decision is made and will not guess at one now.

---

## Loading the synthetic corpus

`make run` loads `data/` automatically on first start if the database is empty. To reload it
against a clean database:

```
make load-corpus
```

The corpus is one proceeding in three versions — draft, revised draft, final — with deliberate
edits: one change is material, one is cosmetic, one appears only in the final version, and one
moves a deadline. Alongside it sits a company context of roughly eight obligations, three
projects, and four documents. Every edit is written down as ground truth so tests can assert
against a known answer rather than an impression. See task T3 in
[`docs/.ai/tasks.html`](docs/.ai/tasks.html) for the exact list once it is built, and
`data/README.md` (to be added with the corpus) for the authored edits themselves.

---

## Where to look first

Start with citation verification. It is the hardest technical decision in this build and the
one the product is designed to make visible rather than bury (ADR-003, task T14). The claim
being tested: a claim whose citation does not check out against the stored source text must
never be shown as established fact — it degrades to a review-queue item labelled unverified,
never to a confident guess.

Once T14 and T17 land, the reviewer path is:

1. Open a change with a verified claim. Click the citation. The exact source span it points to
   highlights in the original document text.
2. Open a change with a citation that was deliberately corrupted (task T17 ships a test proving
   the verifier rejects a fabricated quote). Confirm it is held in the review queue, with the
   reason stated, rather than rendered as fact.
3. Compare that to the deterministic version diff behind it (ADR-004) and the draft-versus-final
   field on the change record (ADR-005) — those two are what the citation is a claim about.

After that: the decision log ([`docs/.ai/decisions.html`](docs/.ai/decisions.html)) is written
to answer "why this and not the alternative" for every choice above, including the ones this
README does not have room for.

---

## Documents

| Doc | Purpose |
|---|---|
| [`docs/.ai/decisions.html`](docs/.ai/decisions.html) | Decision log (ADR-001 through ADR-008). Every choice with its alternatives and the trade-off accepted. Written when the decision is made. |
| [`docs/.ai/tasks.html`](docs/.ai/tasks.html) | The 48-hour build board. Current state of every task. |
| `docs/prd.html` | Product requirements. Not yet written. |
| `docs/mrd.html` | Market requirements. Not yet written. |
| `docs/tdd.html` | Technical design. Not yet written. |
| `docs/architecture.html` | System architecture, and what changes past SQLite/single-node. Not yet written. |
| `docs/security.html` | Security posture. Not yet written. |
| [`docs/user-research.html`](docs/user-research.html) | Who was interviewed, what they said, what changed in the build because of it. Interview requests are the first task in the build plan (T1) — check this file for how many have landed. |
| `docs/future-enhancements.html` | What is deliberately out of scope for 48 hours. Not yet written. |
| `docs/submission.html` | What was built, what was reused, what the AI wrote versus what got rewritten or rejected. Kept current as the build proceeds, not reconstructed at the end. Not yet written. |
| [`docs/best-practices.html`](docs/best-practices.html) | Portable engineering playbook carried over from the peer project. Sections 26 and 27 are directly load-bearing here. |

A doc marked "not yet written" above and a green link below it will be true at different times —
this table is kept in sync with the repository, not with the plan.

---

## Layout

```
app/        application code
tests/      pytest; offline by default
data/       synthetic proceeding versions + company context
docs/       prd.html mrd.html tdd.html architecture.html security.html
            future-enhancements.html user-research.html submission.html
docs/.ai/   decisions.html (ADRs) tasks.html (board)
```

`app/`, `tests/`, and `data/` are currently empty. That is accurate as of this commit, not an
oversight in this file.
