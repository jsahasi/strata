#!/usr/bin/env python3
"""Draw and activate the demonstration approval route.

WHY THIS IS A SCRIPT AND NOT PART OF app/seed.py. The agent that was to seed it
died mid-build, so the live workspace had no active route and the read-only
screen said so, correctly and at length. That screen was telling the truth; the
truth was just useless to anybody looking at a demo.

WHAT THE ROUTE HAS TO SHOW, and why each part earns its place:

  THE BRANCH THAT ACTUALLY EXISTS. The engine refuses two arrows out of one
  step, and says why: it walks an item one step at a time, and two branches
  would need a rule for joining back together that nobody has written. That is
  an honest limit and this route does not pretend otherwise.

  The fork that IS real is the timeout branch. Every step has two exits: the
  person acts and it moves on, or the clock runs out and it goes somewhere else
  -- to a reminder, to a more senior person, or past the step entirely. That is
  the branch worth drawing, because it is the one nobody thinks about until it
  fires at 2am on a filing deadline.

  ALL THREE TIMEOUT BEHAVIOURS, on the steps where each is actually right:
    remind   -- the first step, because chasing is the right answer early and
                reminding is not acting: the step stays put and stays assigned.
    escalate -- the two review steps and the sign-off, because when a named
                person goes quiet on something that binds, it goes to somebody
                more senior rather than through.
    bypass   -- ONE step, and deliberately the only one it could be: telling
                Records that a change happened. Nobody approves a notification.
                A bypass on a step whose name implies sign-off would teach the
                wrong lesson about the product to everyone who saw the demo.

  DIFFERENT CLOCKS. A first read is a day. A legal review is two. An officer is
  three, because that person is busier and the route should say so rather than
  pretend everybody moves at one speed.

Run:  .venv/bin/python scripts/seed_route.py
Idempotent: it does nothing if the company already has an active route.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

from app.state.db import session_scope  # noqa: E402
from app.state.models import ROLE_ADMIN, ROLE_OBLIGATION_OWNER  # noqa: E402
from app.state.workflow import (  # noqa: E402
    activate_workflow,
    active_workflow,
    create_workflow,
    save_graph,
)

COMPANY = "MEP"
WORKFLOW_ID = "WF-0001"
ACTOR = "system:seed"

GRAPH = {
    "workflow_id": WORKFLOW_ID,
    "name": "Large load tariff change",
    "steps": [
        {
            "id": "STP-1",
            "label": "Obligation owner reads it",
            "assignee_rule": "obligation_owner",
            "approval_hours": 24,
            "on_timeout": "remind",
            "remind_every_hours": 4,
            "escalate_to": None,
            "x": 40, "y": 150,
        },
        {
            "id": "STP-2",
            "label": "Legal review",
            "assignee_rule": f"role:{ROLE_ADMIN}",
            "approval_hours": 48,
            "on_timeout": "escalate",
            "escalate_to": f"role:{ROLE_ADMIN}",
            "remind_every_hours": None,
            "x": 250, "y": 150,
        },
        {
            "id": "STP-3",
            "label": "Rates review",
            "assignee_rule": f"role:{ROLE_OBLIGATION_OWNER}",
            "approval_hours": 48,
            "on_timeout": "escalate",
            "escalate_to": f"role:{ROLE_ADMIN}",
            "remind_every_hours": None,
            "x": 460, "y": 150,
        },
        {
            "id": "STP-4",
            "label": "Officer signs the filing",
            "assignee_rule": f"role:{ROLE_ADMIN}",
            "approval_hours": 72,
            "on_timeout": "escalate",
            "escalate_to": f"role:{ROLE_ADMIN}",
            "remind_every_hours": None,
            "x": 670, "y": 150,
        },
        {
            "id": "STP-5",
            "label": "Tell Records it happened",
            "assignee_rule": f"role:{ROLE_OBLIGATION_OWNER}",
            "approval_hours": 120,
            "on_timeout": "bypass",
            "escalate_to": None,
            "remind_every_hours": None,
            "x": 880, "y": 150,
        },
    ],
    "edges": [
        {"from": "STP-1", "to": "STP-2"},
        {"from": "STP-2", "to": "STP-3"},
        {"from": "STP-3", "to": "STP-4"},
        {"from": "STP-4", "to": "STP-5"},
    ],
}


def main() -> int:
    with session_scope() as session:
        live = active_workflow(session, COMPANY)
        if live is not None:
            print(f"route already active: {live.id} {live.name!r}. Nothing to do.")
            return 0

        create_workflow(
            session,
            COMPANY,
            workflow_id=WORKFLOW_ID,
            name=GRAPH["name"],
            actor=ACTOR,
        )
        print(f"drafted {WORKFLOW_ID}")

        saved = save_graph(session, COMPANY, WORKFLOW_ID, GRAPH, actor=ACTOR)
        if getattr(saved, "errors", None):
            for problem in saved.errors:
                print(f"  save refused: {problem}")
            return 1
        print(f"  {len(GRAPH['steps'])} steps, {len(GRAPH['edges'])} edges saved")

        result = activate_workflow(session, COMPANY, WORKFLOW_ID, actor=ACTOR)
        if getattr(result, "errors", None):
            print("  ACTIVATION REFUSED, which is the validation working:")
            for problem in result.errors:
                print(f"    {problem}")
            return 1

    with session_scope() as session:
        live = active_workflow(session, COMPANY)
        print(f"active: {live.id} {live.name!r}" if live else "NOT ACTIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
