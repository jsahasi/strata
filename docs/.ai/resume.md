# Resume here — written 2026-08-04, ~00:15 Pacific, at the quota wall

Read `docs/.ai/state.json` first. It is generated, so it is the only file here that
cannot be stale. Everything below is prose and may already be wrong.

**Safe point:** commit `160bb5c`, 17 commits, pushed to `github.com/jsahasi/strata`
(private). 284 tests pass. Working tree was coherent when this was written.

---

## Do these first, in this order

1. `make test` — confirm still green before touching anything.
2. `rm -f strata.db` — the repo-root database holds stray rows from manual runs and
   reports 4 versions where a fresh clone reports 3. Reseed after deleting.
3. `grep -rn "index=True" app/state/models.py` and check no column with `index=True`
   also has an explicit same-named `Index(...)`. That collision took the entire suite
   down for several minutes during the workspace build and is easy to reintroduce
   across the ~14 new models.

## Four workflows died mid-run. All resumable from cache.

Completed agents replay instantly; only the failed ones re-run.

| Run ID | What is missing |
|---|---|
| `wf_706342ba-87e` | **Nothing landed.** Correctness fixes (findings 5–8), the eval harness, submission.html. Highest value of the four. |
| `wf_c5700011-f0b` | Identity: policy, sessions/login, all 7 attack lenses, the coverage critic, docs. |
| `wf_90691fbd-f45` | Workspace: project views, review centre views, wiring, adversarial audit, docs reconciliation. |
| `wf_21aab519-034` | Reciprocity line for batch 1 (10 drafts) only. The other 28 have it. |

Resume with `Workflow({scriptPath: ..., resumeFromRunId: ...})`. Script paths are in
the scratchpad: `close-findings.js`, `build-identity.js`, `build-workspace.js`,
`add-reciprocity.js`.

**Do not run all four at once.** They overlap on `app/state/models.py`, `app/seed.py`
and `app/web/`. Run `close-findings.js` first — it is the only one that touches
neither, owning `app/text/`, `app/verification/`, `app/evals/` and `docs/submission.html`.

## Open seams the agents reported about their own work

- **Identity writes carry no attribution.** `create_user`, `grant_role`, `revoke_role`
  and `set_user_status` pass only the `actor` display string to `record_event`. They
  need `actor_user_id` / `actor_kind` / `session_id` once a login path exists to supply
  them. Until then, segregation of duties is unprovable in the log.
- **`app/web/views/review.py` records approvals as `actor_kind="system"`.** Same cause.
- **`ensure_system_roles` is never called** from `app/seed.py`, so no roles exist on the
  app path. `make run` is unaffected; the tables are created.
- **`LoginSession` is a table nothing writes.** `failed_attempts` and `locked_until` are
  storage with no writer, so nothing throttles guessing yet.
- **No ADR-015 exists.** Two agents deliberately avoided writing one to avoid racing on
  the number. At least five decisions need recording: scrypt/RBAC, the versioned digest,
  segregation from the audit chain, the demo self-approval downgrade, and the
  coverage-on-every-synthesis rule.
- **`docs/security.html` has a pre-existing unbalanced `<div>` near the end.**
- **Known limit, disclosed not hidden:** an admin holds `user.manage`, so an admin can
  grant themselves `obligation_owner` and approve their own work. The chain records the
  grant, making it visible afterwards; nothing prevents it. Preventing it needs a second
  approver on privilege changes.

## Then, in priority order

1. **3–5 SAMPLE projects with activity** across every page, tagged SAMPLE. Note the seed
   agent already built four projects derived from `data/company_context.json` rather than
   inventing names — extend that, do not replace it.
2. **Create-project gated on permission** (the view exists; the gate does not).
3. **Superuser permissions panel** — modify permissions for all users in the workspace.
4. **Responsive app templates.** The corporate site is responsive; the app is not.
5. **Basic auth at the edge** before the app replaces the holding page at
   `strata.sudama.ai`.

## Deployment state

- `strata.sudama.ai` → `143.198.140.28` (citelocal-1, **not** the PII/PHI host).
- Live: corporate site, `/login` holding page, custom 404. TLS valid to 2 November.
- Container `strata-site`, nginx, `/data/strata/` on the box. SSH key
  `~/.ssh/citelocal_deploy`.
- ADR-010 needs amending: the remedy is "a host that is not the PII/PHI box", not "its
  own droplet". No DigitalOcean token exists anywhere, so a genuinely separate droplet
  still needs one.

## Outreach

7 sent. 38 drafted, corrected, carrying `strata.sudama.ai` in the signature. 28 carry the
share-what-I-learn offer; 10 still need it (batch 1).

**Send wave 1 only** — one per organisation, 20 recipients. Wave 2 has near-identical
middle paragraphs between colleagues at the same employer; the audit findings are in the
task output for `wtnngjexm`.

Nourse and Schuler are both AEP. Send one.
