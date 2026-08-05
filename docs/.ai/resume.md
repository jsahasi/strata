# Resume — written 2026-08-04, ~14:00 Pacific, at 97% context

Read `docs/.ai/state.json` first: it is generated, so it is the only file here
that cannot be stale. Everything below is prose and may already be wrong.

## Live right now

- **strata.sudama.ai serves the real product**, not a holding page. Marketing site
  at `/`, application behind it on the same host, nginx proxying a NAMED LIST of
  paths (not a catch-all — see the reasoning in `deploy/nginx.conf`).
- Sign in: `denise.okoro@mep.example` (analyst), `priya.nandakumar@mep.example`
  (obligation owner), `sarah.lindqvist@mep.example` (admin). Password
  `strata-demo-2026`, printed on the site on purpose.
- Host: citelocal-1, 143.198.140.28, key `~/.ssh/citelocal_deploy`.
  `/data/strata/` holds `compose.yml`, `nginx.conf`, `site/`, `src/`, `db/`, `app.env`.
- Deploy: `rsync` to `/data/strata/src/`, then `docker compose build strata-app &&
  docker compose up -d`. Site only: `rsync deploy/site/ …:/data/strata/site/`.
- **The entrypoint migrates on EVERY start** (`scripts/migrate.py`). It seeds only
  when no database file exists, so a redeploy keeps the audit chain.

## Suite

1381 passing, 2 xfailed, 1 xpassed. Coverage **92%** (10,359 statements, 804 missed).
Lowest: `review_centre.py` 74%, `migrate.py` 0% (new, untested), `notify/transport.py` 83%.

## Running when this was written

| Workflow | What |
|---|---|
| `wf_2d62c09a-b13` | Granular permissions: any permission to any person, custom roles, conflict report |
| `wf_1f066e45-b92` | Blog repair — all five articles were refuted on numbers, quotes were all clean |

## Asked for and NOT yet done

1. **Realistic roles.** Three fixed roles are too few. Real titles in `data/real/`:
   Analyst II, Manager, Director, Deputy Director, VP Pricing & Planning,
   Regulatory Counsel, Legal Assistant. The demo route in `scripts/seed_route.py`
   puts "Legal review" AND "Officer signs" both on `role:admin` because no such
   roles exist — so the person who draws the route approves through it. Fix after
   the permissions workflow lands.
2. **99% coverage on all modules.** User overrode ADR-38, which rejects a
   line-coverage target. User then asked "can't we do both?" — yes: keep branch
   and mutation testing AND reach 99%, with every new test required to fail when
   the behaviour breaks. ADR-38 needs amending, not deleting.
3. **Logo mismatch.** The application masthead wordmark differs from the marketing
   site's. User wants the SITE one used in the app.
4. **Re-record the demo video.** Current one predates the glass restyle, the
   markdown fix, source links, the tour and Integrations.
5. **Blog is not publishable** until `wf_1f066e45-b92` lands and re-checks clean.
6. Tour and Integrations built; verify they are wired and visible.

## Things that were true and surprising

- **`db.py` reads `STRATA_DATABASE_URL`**, not `STRATA_DB_PATH`. Setting the latter
  silently writes `./strata.db` in the working directory.
- **`init_db()` drops every table** unless `drop_first=False`. 480 test call sites
  depend on the drop; production must pass False.
- The four bounced outreach addresses were all **filing-verified** — a service list
  records who was reachable when filed, not now.
- Zero replies to ~40 emails. Julia Lundin (AES) and Federico Heine (AES) both
  emailed, both unanswered. Those are the only paths to a real ICP interview.
- 102 real filings in `data/real/`, all hashes verified, 62 in version families.
  Kentucky Kollen pair: 1,024,409 vs 1,024,536 chars, 144 changes in 0.78s.
- Three of those families contain the **filer's own redline** — real ground truth
  for an accuracy claim, which retires the "eval set is self-labelled" concession.

## The failure that keeps recurring

Built and not connected. Today: two routers unmounted while the nav linked to
them; Clarke built and never included in `base.html`; its engine shipped under a
different name than the view looked for; `.env` never loaded so every model path
announced itself as off while holding a valid key; the approval route seeded by an
agent that died, so the screen correctly said there was none. **No test caught any
of them** — tests verify capability, not whether anything is wired or seeded.
`tests/test_app_wiring.py` and the demo-readiness idea are the guards for this.
