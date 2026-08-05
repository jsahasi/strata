"""Reset the demonstration tenant: remove every row it owns, then lay it down again.

WHY THIS EXISTS, and it is a hole the landing page opened on purpose.
deploy/site/index.html prints three accounts and one shared password, and argues
for it in its own words: "an account whose password is on a public page is not a
secret being kept badly -- it is a door held open." That argument is sound and it
is only half of one. A door held open is safe when the room behind it is
disposable, and until this script existed the room was permanent. Anybody who
signed in as sarah.lindqvist@mep.example held user.manage, user.invite,
workflow.manage and threshold.set: they could add accounts, redraw the approval
route, move the confidence threshold, mint share links and grant themselves
approval through an invitation. None of that reaches anything confidential --
the corpus is invented plus public filings, and there are no customers. All of it
is PERMANENT, because the audit chain is append-only and nothing in this product
removes a row. So a visitor could have made the demonstration unreadable half an
hour before a reviewer opened it, and the only way back was deleting the database
by hand on the host. That is the gap this closes.

WHAT IT REFUSES, AND THE FIRST REFUSAL IS THE ONE THAT MATTERS.

  * IT CAN ONLY DELETE THE TENANT THE SEED ITSELF WOULD CREATE. The company id
    is read from data/company_context.json -- the same file app/seed.py reads --
    rather than taken from an argument. There is no --company flag and there
    will not be one. Point this at a production database and the id is not there,
    so it deletes nothing and says so. A reset that takes its target from the
    caller is one typo from being the worst command in the repository.
  * It refuses unless STRATA_DEMO_ACCOUNTS says this is a demonstration. That
    switch already exists and already carries exactly this meaning --
    deploy/entrypoint.sh: "one setting, one meaning: this workspace is a
    demonstration" -- so a deployment holding a customer's documents, which sets
    it to 0 to keep the published passwords off the login page, cannot be reset
    by this script either. One switch, not a second one to keep in step.
  * It refuses without --yes. There is no default, no prompt and no environment
    variable that can stand in for it. A destructive command that can run because
    something was already set in a shell profile is a command that runs by
    accident.

WHY THIS MAY DELETE AN AUDIT CHAIN WHEN NOTHING ELSE IN THE PRODUCT MAY.
app/state/retention.py schedules audit_events as "never", and app/state/audit.py
refuses an UPDATE or a DELETE from application code. Both are right and neither
is weakened here, because the thing they protect is a DIFFERENT thing from what
this does. The chain is gapless PER COMPANY: verify_chain walks one company's
sequence, and a missing row inside that sequence is read as tampering. Removing
one company's rows ENTIRELY leaves no gap in anybody's chain, because there is no
longer a sequence to have a gap in. Deleting some of a tenant's history is
tampering; decommissioning a whole tenant is not, in the same way that closing an
account is not forging its statements. The demonstration tenant is declared
disposable in advance, on the public page, before anybody signs in -- not after
somebody made a mess that would be convenient to forget.

The deletes go through SQLAlchemy Core rather than the ORM, so the append-only
hook on the Session does not fire. That is deliberate and it is the hook's own
stated design: audit.py says it "does not stop a direct SQL statement, and it is
not meant to. That is the hash chain's job, and the two are kept separate because
they fail differently: this one refuses the mistake, the chain catches the
attack." This is neither a mistake nor an attack, so it goes round the guard that
exists to catch mistakes, in a file that says at length why.

PROVING THE BLAST RADIUS RATHER THAN ASSERTING IT. After the reseed, every
company still in the database EXCEPT the one that was reset has its chain
verified, and the script exits non-zero if any of them fails. So "this touched
nothing else" is a check that ran, not a sentence somebody wrote. The reset
tenant is excluded from that check for the obvious reason: its chain is new.

WHAT IT DOES NOT DO. It is not scheduled and nothing calls it -- no entrypoint
line, no job, no route, and deliberately no button in the product, because a
reset reachable from a browser is a reset a visitor can press. It does not touch
the permissions vocabulary, the system roles, or any tenant that is not the
demonstration one. It does not reset the passage index by itself: the reseed
rebuilds what it lays down, and scripts/build_index.py is the whole-corpus
answer if the index needs it. And it does not pretend the demonstration tenant
carries evidentiary weight -- nothing in a workspace whose password is public
ever did.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import delete, func, select  # noqa: E402

from app.seed import DATA_DIR, ensure_accounts, load  # noqa: E402
from app.state.audit import AuditTamperError, verify_chain  # noqa: E402
from app.state.db import session_scope  # noqa: E402
from app.state.models import (  # noqa: E402
    ApprovalWorkflow,
    Base,
    DocumentVersion,
    Role,
    ShareLink,
)

#: The switch deploy/entrypoint.sh already uses to mean "this workspace is a
#: demonstration". Read the same way it reads it, so the two cannot disagree.
DEMO_SWITCH = "STRATA_DEMO_ACCOUNTS"

#: Tables scoped by a join rather than by a company_id column of their own, each
#: with the parent that carries the tenancy. They are deleted FIRST, because
#: every one of them points at a row the company_id pass is about to remove.
#: share_opens is the one with a comment on it in models.py saying a query that
#: does not join share_links is an unscoped read; this is that join, written as
#: a delete.
JOIN_SCOPED = (
    ("share_opens", "share_id", ShareLink),
    ("workflow_edges", "workflow_id", ApprovalWorkflow),
    ("workflow_steps", "workflow_id", ApprovalWorkflow),
    ("passages", "version_id", DocumentVersion),
    ("role_permissions", "role_id", Role),
)

#: Never touched. `permissions` is the closed vocabulary of permission codes --
#: it describes the product, not any tenant, and app/state/retention.py says so
#: in the same words: "there is no person in it to have a retention right over."
NEVER = frozenset({"permissions"})


def demo_company_id(data_dir: Path = DATA_DIR) -> str:
    """The one tenant this script may delete, read from the seed's own source.

    Not an argument, and not a constant restated here either. app/seed.py reads
    this exact field to decide what company_id to write, so the only tenant this
    can remove is the one the seed would create. Restating it as a literal would
    be a second spelling to drift.
    """
    context = json.loads((data_dir / "company_context.json").read_text("utf-8"))
    return context["company"]["short_name"]


def demonstration_workspace(env=None) -> bool:
    """Whether this deployment calls itself a demonstration.

    The same parse deploy/entrypoint.sh performs, so a value that turns the demo
    content off there cannot leave this script thinking it is still on.
    """
    raw = (env or os.environ).get(DEMO_SWITCH, "1")
    return str(raw).strip().lower() != "0"


def _company_ids(session) -> set[str]:
    """Every tenant with an audit row. The chain is what makes a tenant real."""
    from app.state.models import AuditEvent

    return set(session.execute(select(AuditEvent.company_id).distinct()).scalars())


def purge_company(session, company_id: str) -> dict[str, int]:
    """Remove every row this tenant owns. Returns a count per table it touched.

    Children first, parents after: the join-scoped tables, then the tables with
    a company_id in reverse dependency order. sorted_tables is parents-first, so
    reversing it deletes a row before whatever it points at.
    """
    removed: dict[str, int] = {}

    for table_name, column, parent in JOIN_SCOPED:
        table = Base.metadata.tables[table_name]
        owned = select(parent.id).where(parent.company_id == company_id)
        result = session.execute(
            delete(table).where(table.c[column].in_(owned))
        )
        if result.rowcount:
            removed[table_name] = result.rowcount

    for table in reversed(Base.metadata.sorted_tables):
        if table.name in NEVER or "company_id" not in table.c:
            continue
        result = session.execute(
            delete(table).where(table.c.company_id == company_id)
        )
        if result.rowcount:
            removed[table.name] = result.rowcount

    return removed


def reseed(session) -> None:
    """Lay the corpus and the accounts down again, exactly as a fresh start does."""
    load(session)
    ensure_accounts(session)


def run_extra_seeds() -> list[str]:
    """The three scripts deploy/entrypoint.sh runs after the corpus.

    Without them the demonstration comes back with no obligations, no source
    registrations, no approval route and none of the real filings -- which is
    precisely the defect the entrypoint's own comment describes, and a reset
    that reproduced it would be a reset nobody could use twice. A failure is
    reported and is not fatal, for the reason the entrypoint gives: a
    demonstration missing its approval route is still worth serving.
    """
    problems: list[str] = []
    for name in ("seed_demo_gaps", "seed_route", "ingest_real"):
        script = REPO / "scripts" / f"{name}.py"
        result = subprocess.run(
            [sys.executable, str(script)], cwd=REPO, capture_output=True, text=True
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()
            problems.append(f"{name}: {tail[-1] if tail else 'failed with no output'}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reset the demonstration tenant. Deletes every row it owns and lays "
            "it down again. Only ever touches the tenant the seed itself creates."
        )
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required; there is no prompt and no environment variable for this",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what is there and what would go, and change nothing",
    )
    args = parser.parse_args(argv)

    company_id = demo_company_id()

    if not demonstration_workspace():
        print(
            f"refusing: {DEMO_SWITCH} is 0, so this workspace does not call itself "
            "a demonstration. Nothing was touched.",
            file=sys.stderr,
        )
        return 2

    if not args.yes and not args.dry_run:
        print(
            "refusing: pass --yes. This removes every row the demonstration "
            f"tenant {company_id!r} owns, including its audit chain.",
            file=sys.stderr,
        )
        return 2

    with session_scope() as session:
        before = _company_ids(session)
        others = sorted(before - {company_id})

        if company_id not in before:
            print(
                f"no tenant {company_id!r} in this database; nothing to reset. "
                f"Tenants present: {', '.join(others) or 'none'}."
            )
            return 0

        if args.dry_run:
            counts: dict[str, int] = {}
            for table in Base.metadata.sorted_tables:
                if table.name in NEVER or "company_id" not in table.c:
                    continue
                found = session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.company_id == company_id)
                )
                if found:
                    counts[table.name] = found
            print(
                f"dry run: would remove tenant {company_id!r} -- "
                f"{sum(counts.values())} rows across {len(counts)} tables -- "
                "and lay it down again."
            )
            for name in sorted(counts):
                print(f"  {name}: {counts[name]}")
            print(
                f"other tenants, which this cannot touch: {', '.join(others) or 'none'}. "
                "Nothing was changed."
            )
            return 0

        removed = purge_company(session, company_id)
        session.flush()
        reseed(session)

    problems = run_extra_seeds()

    with session_scope() as session:
        broken: list[str] = []
        for other in others:
            try:
                verify_chain(session, other)
            except AuditTamperError as exc:
                broken.append(f"{other}: {exc}")

    total = sum(removed.values())
    print(f"reset {company_id}: removed {total} rows across {len(removed)} tables.")
    for name in sorted(removed):
        print(f"  {name}: {removed[name]}")
    print(f"reseeded {company_id}.")

    if problems:
        print("the extra seed scripts reported problems; screens they fill may be empty:")
        for line in problems:
            print(f"  {line}")

    if others:
        if broken:
            print("OTHER TENANTS' CHAINS DID NOT VERIFY AFTER THIS RESET:", file=sys.stderr)
            for line in broken:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(
            f"verified the audit chain of every other tenant ({', '.join(others)}); "
            "none was disturbed."
        )
    else:
        print("no other tenant in this database, so nothing else could be disturbed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
