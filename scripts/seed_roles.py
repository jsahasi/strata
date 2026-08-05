#!/usr/bin/env python3
"""Seed the demonstration role templates, drawn from real titles.

WHY THESE AND NOT "MANAGER" AND "DIRECTOR". Seniority is not a permission. A
Director of Load Forecasting and a Director of System Operations approve
completely different things, and a route that says "Director" says nothing a
product can act on. Every role below is a FUNCTION, and every one is taken from
a title that appears in this repository's own evidence:

  data/company_context.json names the obligation owners and what each owns --
  Manager, Interconnection; Director, Load Forecasting; Director, System
  Operations. Those are three different approvers, not three managers.

  data/real/ carries 102 filings by people whose titles the old three-role model
  could not express: Analyst II Regulatory Affairs, Manager Regulatory Affairs,
  Indiana Regulatory Counsel, VP Pricing and Planning, Deputy Director, Legal
  Assistant.

THESE ARE TEMPLATES, NOT A VOCABULARY. That distinction is the whole point of
this file and it must survive into the interface. A company starts from these
because starting from nothing is worse; it then grants any permission to any
person and names whatever set it ends up with. A role called "analyst" that one
company has quietly edited into something else is worse than no role at all,
which is why a system template is copied rather than edited.

WHAT IS DELIBERATELY ABSENT. No template holds both action.propose and
action.approve. Not because a company may not do that -- a four-person team will,
legitimately -- but because a DEFAULT should not hand it to them silently. If
they want it, they grant it, and the conflict report names them.

Run:  .venv/bin/python scripts/seed_roles.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

#: (name, description, permission codes). The description says what the role
#: DOES, in the words a person in that seat would use, never what it is called.
ROLE_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Regulatory analyst",
        "Reads the docket, works out what moved between versions, and proposes "
        "what the company should do about it. Does not approve their own work.",
        ("proceeding.read", "change.read", "claim.read", "action.propose",
         "escalation.resolve", "steer.issue", "project.create", "knowledge.write"),
    ),
    (
        "Regulatory affairs manager",
        "Everything the analyst does, plus the filing calendar and the authority "
        "to approve a change that touches no binding obligation.",
        ("proceeding.read", "change.read", "claim.read", "action.propose",
         "escalation.resolve", "steer.issue", "project.create", "knowledge.write",
         "threshold.set"),
    ),
    (
        "Regulatory counsel",
        "Decides whether a change binds the company and what it commits them to. "
        "Approves and rejects; does not propose, so the reading and the legal "
        "opinion are never the same person's.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject", "audit.read"),
    ),
    (
        "Rates and pricing",
        "Owns tariff language, cost allocation and the numbers in a schedule. "
        "Approves what lands on a rate.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject"),
    ),
    (
        "Interconnection",
        "Owns the duties that attach to connecting a large load: security "
        "posted, studies kept, network upgrade costs charged to the right "
        "project.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject"),
    ),
    (
        "Load forecasting",
        "Owns the demand forecasts and the filings that report them.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject"),
    ),
    (
        "System operations",
        "Owns what the company must be able to do on the system itself -- "
        "curtail a large load on short notice, and prove it can.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject"),
    ),
    (
        "Certifying officer",
        "Signs what is filed. The last approval before a document leaves the "
        "company, and the person whose name is on it.",
        ("proceeding.read", "change.read", "claim.read", "action.approve",
         "action.reject", "audit.read"),
    ),
    (
        "Workspace administrator",
        "Accounts, permissions, the approval route and the sources. Approves "
        "nothing by default: the person who draws the route should not be the "
        "person it routes to. A company that wants otherwise grants it, and the "
        "conflict report names them.",
        ("proceeding.read", "change.read", "claim.read", "user.manage",
         "user.invite", "workflow.manage", "threshold.set", "audit.read"),
    ),
    (
        "Auditor",
        "Reads everything, including the hash chain, and changes nothing. For "
        "internal audit, external counsel, or a regulator asking how a decision "
        "was reached.",
        ("proceeding.read", "change.read", "claim.read", "audit.read"),
    ),
)


def main() -> int:
    from app.state.models import PERMISSION_CODES

    known = set(PERMISSION_CODES)
    bad = []
    for name, _, codes in ROLE_TEMPLATES:
        for code in codes:
            if code not in known:
                bad.append(f"{name}: {code}")
    if bad:
        print("REFUSED: templates name permissions this product does not define:")
        for line in bad:
            print(f"  {line}")
        return 1

    print(f"{len(ROLE_TEMPLATES)} role templates, all codes valid:\n")
    for name, description, codes in ROLE_TEMPLATES:
        both = "action.propose" in codes and "action.approve" in codes
        flag = "  <-- HOLDS BOTH SIDES" if both else ""
        print(f"  {name:28} {len(codes):2} permissions{flag}")

    print(
        "\nThese are starting points. An administrator grants any permission to "
        "any person\nand names whatever set they end up with; a template is "
        "copied, never edited."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
