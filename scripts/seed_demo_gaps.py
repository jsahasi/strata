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
    AUTHOR_ANALYST,
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

#: The two mappings a person makes in the demonstration, and each is chosen for
#: what it shows rather than for being convenient.
#:
#: CHG-v1-v2-004 / OBL-005 is the flagship case and data/company_context.json
#: calls it that: section 5.2 moves from 100% customer-pays to a shared
#: allocation, and OBL-005 states the old practice. The internal document that
#: was correct against v1 is wrong against v2 with nobody having edited it. The
#: words agree strongly here -- network, upgrade, costs, general, rate -- so this
#: is the case where the proposer offers and a person agrees.
#:
#: CHG-v1-v2-006 / OBL-002 is the opposite and the more important one. The
#: docket's compliance date moves from March 2027 to June 2027; OBL-002 says
#: "on an annual cycle" and mentions no date at all, so the two share nothing a
#: lexical rule can see, and the corpus says so in its own note_for_semantic_join
#: field. The proposer does NOT offer it. A person maps it anyway and the audit
#: row records that the words did not find it -- which is the whole argument for
#: proposing rather than asserting, standing in the seeded data where a reviewer
#: meets it rather than in a paragraph claiming it.
CONFIRMED_BY_A_PERSON = (
    ("CHG-v1-v2-004", "OBL-005"),
    ("CHG-v1-v2-006", "OBL-002"),
)
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


def seed_change_mappings() -> None:
    """Map changes to the duties they bear on, so routing has somewhere to go.

    THE GAP THIS CLOSES IS THE ONE THIS SCRIPT WAS NAMED FOR AND DID NOT CLOSE.
    seed_obligations above filled the obligations table and stopped there, so
    change_obligations stayed at zero rows after a full `make seed` and
    resolve_change_owner answered ROUTE_NO_OBLIGATION for all 171 changes in the
    workspace. The owner handoff, the approval route and the escalation queue
    were all downstream of a table nothing wrote. Capability against content, in
    the same shape the docstring at the head of this file describes.

    TWO KINDS OF ROW, AND BOTH ARE VISIBLE ON PURPOSE.

    A person's. Two mappings are written as AUTHOR_ANALYST, by the analyst
    account, because a demonstration where every mapping came from the pipeline
    cannot show the difference the mapped_by_kind column exists for. One of them
    -- CONFIRMED_BY_HAND below -- is a mapping the words could not have found,
    which is the single most useful thing in this seed: it is the case that
    proves a person can reach past the proposer rather than only agree with it.

    The pipeline's. Every remaining change gets its top candidate as
    AUTHOR_SYSTEM. On this corpus that is 24 more rows out of 171 changes, and
    the other 145 stay unmapped -- which is the honest number. A seed that
    mapped every change would be a seed asserting a link it does not have.

    THE PIPELINE NEVER WRITES BESIDE A PERSON. A change somebody has already
    mapped is skipped whole, rather than gaining a second machine-proposed row.
    Two obligations with two different owners is ROUTE_OWNERS_DISAGREE, so a
    candidate added next to a confirmed mapping would take a change that routed
    cleanly and stop it routing at all -- the machine overruling a person by
    arithmetic. It also makes this idempotent for free: the second run finds a
    mapping and writes nothing.
    """
    from app.state.claims import change_for_company
    from app.state.mapping import (
        confirm_obligation_for_change,
        propose_obligations_for_change,
    )
    from app.state.models import Change, ChangeObligation, ROLE_ANALYST
    from app.state.routing import map_change_to_obligation, mappings_for_change

    with session_scope() as session:
        # Counted rather than assumed. The first version of this printed how
        # many mappings it had asked for, which on a second run was two
        # confirmations and nothing written -- a seed reporting work it did not
        # do. What a reader needs is how many rows this run added and how many
        # are there now, and the only way to know the first is to count.
        held = (
            session.query(ChangeObligation)
            .filter(ChangeObligation.company_id == COMPANY)
            .count()
        )
        analyst_email = next(
            (a.email for a in demo_account_list() if a.role == ROLE_ANALYST), None
        )
        analyst = (
            session.query(User)
            .filter(User.company_id == COMPANY, User.email == analyst_email)
            .one_or_none()
            if analyst_email
            else None
        )

        if analyst is None:
            print("mappings: no analyst account, so nothing is confirmed by a person")
        else:
            for change_id, obligation_id in CONFIRMED_BY_A_PERSON:
                if change_for_company(session, COMPANY, change_id) is None:
                    # A corpus without the synthetic docket is a corpus this
                    # pair does not describe. Saying so beats raising: the rest
                    # of the seed is still worth running.
                    print(f"mappings: {change_id} is not in this corpus; skipped")
                    continue
                confirm_obligation_for_change(
                    session,
                    COMPANY,
                    change_id=change_id,
                    obligation_id=obligation_id,
                    actor=f"person:{analyst.email}",
                    actor_user_id=analyst.id,
                )

        changes = (
            session.query(Change)
            .filter(Change.company_id == COMPANY)
            .order_by(Change.id)
            .all()
        )
        for change in changes:
            if mappings_for_change(session, COMPANY, change.id):
                continue
            proposal = propose_obligations_for_change(
                session, COMPANY, change_id=change.id
            )
            if not proposal.candidates:
                continue
            top = proposal.candidates[0]
            map_change_to_obligation(
                session,
                COMPANY,
                change_id=change.id,
                obligation_id=top.obligation_id,
                mapped_by=ACTOR,
                note=f"proposed on {', '.join(top.matched_terms)}",
            )

        session.flush()
        rows = (
            session.query(ChangeObligation)
            .filter(ChangeObligation.company_id == COMPANY)
            .all()
        )
        by_person = sum(1 for row in rows if row.mapped_by_kind == AUTHOR_ANALYST)
        print(
            f"mappings: {len(rows) - held} written this run; "
            f"{len(rows)} on record -- {by_person} confirmed by a person, "
            f"{len(rows) - by_person} proposed by the pipeline. "
            f"{len(changes) - len(rows)} changes carry no obligation."
        )


def main() -> int:
    seed_obligations()
    seed_sources()
    seed_change_mappings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
