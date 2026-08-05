#!/usr/bin/env python3
"""Fill the tables a seeded demo leaves empty, so every screen has something on it.

WHY THIS EXISTS, and it is the same failure four times over. A freshly seeded
workspace had 26 tables with rows and 16 without. Some of those sixteen are
empty for good reasons -- a chat transcript, a login session and a piece of
feedback all arrive when somebody uses the product. Three were not:

  obligations / change_obligations   Routing had nobody to route TO. The whole
                                     owner-handoff feature, its tests all green,
                                     could not demonstrate at all.
  source_registrations               The Integrations screen was blank.
  workflow_runs / workflow_step_runs A route with nothing travelling it.

WHY NO TEST CAUGHT IT. The suite verifies the ENGINE: a route can be drawn,
saved and activated; validation refuses a bad graph; routing resolves an owner
or refuses. Not one test asserted that the SEEDED DEMO contains a route, or an
obligation, or a source. Capability against content -- and the tests only ever
asked about capability. tests/test_demo_ready.py now asks the other question.

Run:  .venv/bin/python scripts/seed_demo_gaps.py
Idempotent throughout: each part checks before it writes.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

from app.state.db import session_scope  # noqa: E402
from app.state.models import (  # noqa: E402
    ROLE_ADMIN,
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    SourceRegistration,
    User,
    initial_status_for_kind,
)
from app.seed import demo_account_list  # noqa: E402
from app.state.routing import ensure_obligation, obligations_for_company  # noqa: E402

COMPANY = "MEP"
ACTOR = "system:seed"
CONTEXT = json.loads((ROOT / "data" / "company_context.json").read_text())
REAL = ROOT / "data" / "real"


def seed_obligations() -> None:
    """The company's own duties, with the owner resolved to a real account.

    owner_name in the JSON is a display string. The routing layer needs a User,
    so it is matched by name against the seeded accounts -- and where no account
    matches, the obligation is created WITHOUT an owner rather than with a
    guessed one. An unowned obligation is visible and unroutable, which is the
    honest state and the one routing.py already refuses on.
    """
    with session_scope() as session:
        if obligations_for_company(session, COMPANY):
            print("obligations already present")
            return
        people = {
            u.display_name: u.id
            for u in session.query(User).filter(User.company_id == COMPANY)
        }
        made = unowned = 0
        for row in CONTEXT["obligations"]:
            owner_id = people.get(row.get("owner_name") or "")
            ensure_obligation(
                session,
                COMPANY,
                obligation_id=row["id"],
                title=row["internal_wording"],
                owner_user_id=owner_id,
                actor=ACTOR,
            )
            made += 1
            if owner_id is None:
                unowned += 1
        print(f"obligations: {made} created, {unowned} with no matching account")


def seed_sources() -> None:
    """The eight commissions the real corpus was actually retrieved from.

    Every row is true: these are the systems the 102 filings in data/real/ came
    from, and each carries the count it accounts for. Status comes from
    initial_status_for_kind, which today returns not_implemented for every kind,
    because nothing in this product fetches anything. A registry that rendered
    these as live feeds would be the lie the screen exists to avoid.
    """
    seen: dict[str, int] = {}
    for prov in REAL.glob("*.provenance.json"):
        data = json.loads(prov.read_text())
        name = data.get("jurisdiction") or data.get("filer") or "unknown"
        seen[name] = seen.get(name, 0) + 1

    with session_scope() as session:
        if session.query(SourceRegistration).filter(
            SourceRegistration.company_id == COMPANY
        ).first():
            print("source registrations already present")
            return
        # created_by_user_id is NOT NULL, and rightly: a source is a decision
        # somebody made and the registry has to name them. The admin account is
        # the honest answer for a seeded row -- it is who would have added it.
        admin_email = next(
            (a.email for a in demo_account_list() if a.role == ROLE_ADMIN), None
        )
        admin = (
            session.query(User)
            .filter(User.company_id == COMPANY, User.email == admin_email)
            .one_or_none()
            if admin_email
            else None
        )
        if admin is None:
            print("sources: no admin account to attribute them to; skipped")
            return

        for index, (name, count) in enumerate(sorted(seen.items()), start=1):
            session.add(
                SourceRegistration(
                    id=f"SRC-{index:04d}",
                    company_id=COMPANY,
                    name=name,
                    kind=SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
                    status=initial_status_for_kind(SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET),
                    config={"documents_retrieved": count},
                    credential_ref=None,
                    created_by_user_id=admin.id,
                    enabled=True,
                )
            )
        print(f"sources: {len(seen)} commissions registered, {sum(seen.values())} documents")


def main() -> int:
    seed_obligations()
    seed_sources()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
