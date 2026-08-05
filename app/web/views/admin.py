"""The approval route editor: a canvas, five endpoints, and the rules of the graph.

WHAT THIS SCREEN IS FOR. An escalation has to reach a person. Today the product
knows how to refuse a claim and nothing about who is asked next, in what order,
or what happens when nobody answers. This is where an admin draws that, once,
and every escalation afterwards walks it. The read-only half of the same fact --
what the route is, in words, for everybody who is not an admin -- is
app/web/views/workflow.py, and it is not this screen with the buttons removed.

THE WIRE CONTRACT IS FIXED AND IS NOT NEGOTIATED HERE. The five paths, the field
names and the two vocabularies were handed down; the canvas editor and whatever
runs the route were built by people who could not talk to each other, and a
field renamed on one side is a graph the other side cannot read. app/state
/models.py already stores the contract field for field, so the serialiser below
is a copy and not a translation table. Where I disagreed with the contract I
built it anyway and said so in the report.

THREE DECISIONS THIS MODULE MAKES, none of which the contract settles.

**A save validates types and vocabulary. Activation validates completeness.**
The contract says a draft may be saved while invalid, and it means half-finished
-- an admin who has drawn three nodes and filled in one. It does not mean wrong.
`on_timeout: "ignore"` is not a half-finished answer, it is a word the product
does not have; no CHECK constraint exists to stop it reaching the column, and
once there it routes nowhere while looking like a decision somebody made. Same
for an edge naming a step that does not exist: SQLite takes it and PostgreSQL
refuses it, so accepting it would mean the product behaves differently on the
demo database from the one it ships on. Those are refused at save. What is
allowed to be missing stays missing: approval_hours, on_timeout,
remind_every_hours and escalate_to are written as NULL rather than defaulted,
because a default of 24 hours is an answer nobody gave sitting in a column a
reviewer reads as a decision.

**Activation is where absence becomes denial.** Every refusal names the step it
belongs to, so the editor can put the message on the node. An error list at the
bottom of a canvas is a list nobody reads. Four of the checks are about a single
step and four are about the shape of the graph: one starting point, every step
reachable, no loop, every edge naming a step that exists. The graph-level ones
carry step_id None, which is the only extension to the contract's error shape
and is a null in a field the contract already types as a string id.

**An escalation target that resolves to nobody is refused.** `role:approvers`
is well-formed and, if this company has no such role, routes into silence. The
product's own rule -- absence is denial -- applies to routing exactly as it
applies to citations, so activation resolves every assignee_rule and every
escalate_to against the company's roles and users, and refuses the ones that
land nowhere. A rule cannot be checked this way: obligation_owner resolves per
escalation, and whether that person exists is a question about the item rather
than about the route.

WHAT THIS MODULE DOES NOT DO. It does not run a workflow. Nothing here creates a
WorkflowRun or moves one; the tables exist and another module fills them. It
does not enforce that the person drawing the route is not the person the route
asks -- workflow.manage is deliberately not an approval permission and admin
deliberately holds no approval code, which is the whole of that control (see the
grid in app/state/identity.py). And it has no undo: a route is edited while it
is a draft, and once activated the way to change it is a new draft that
supersedes it.

WHERE THE READS BELONG. Every scoped read in this file is private and prefixed,
and every one of them belongs in app/state/queries.py beside versions_for_company
and passages_for_company. They are here because that file is owned by another
agent in this build and a parallel edit would lose their work. The tenant guard
itself is IMPORTED rather than copied -- `_require_scope` -- so there is still
one definition of what an unscoped read is, which is the property that file
exists for. Moving these four functions is a handoff, not a rewrite: the
signatures are already the ones that file uses.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# THE MODULE, NOT THE NAMES, AND THIS IS NOT A STYLE CHOICE.
#
# `from app.auth.policy import PermissionDenied` binds the class object that
# existed when this module was imported. app/auth/policy.py reads its approval
# mode at import, so the only honest way to test that path is to reload it --
# tests/test_policy.py does exactly that, several times. A reload builds a NEW
# class object and puts it in the same module dict, so `require` (whose globals
# ARE that dict) then raises the new class while an `except` here still names
# the old one. The refusal stops being caught, and a 403 becomes a 500.
#
# It is not a test artefact. Any process that reloads a module -- a plugin
# loader, a development reloader, a fixture -- breaks every `except` bound to a
# class it exported. Referring to policy.PermissionDenied looks the name up when
# the exception is caught, so this module always catches whatever the current
# policy module raises. Same reasoning for require() and has(): calling them
# through the module means a reloaded gate is the gate that runs, rather than
# the copy this module happened to capture first.
from app.auth import policy
from app.state.audit import ACTOR_SYSTEM, ACTOR_USER, record_event
from app.state.db import session_scope
from app.state.identity import roles_for_company, users_for_company
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    ASSIGNEE_ROLE_PREFIX,
    ASSIGNEE_RULE_LITERALS,
    ASSIGNEE_RULE_PREFIXES,
    ASSIGNEE_UNASSIGNED,
    ASSIGNEE_USER_PREFIX,
    STEP_TIMEOUT_ACTIONS,
    TIMEOUT_BYPASS,
    TIMEOUT_ESCALATE,
    TIMEOUT_REMIND,
    WORKFLOW_ACTIVE,
    WORKFLOW_ARCHIVED,
    WORKFLOW_DRAFT,
    ApprovalWorkflow,
    WorkflowEdge,
    WorkflowStep,
)

# The one definition of an unscoped read, imported rather than restated. Two
# copies of a tenant guard drift, and the copy nobody audited is the one that
# leaks. app/auth/policy.py imports it from the same place for the same reason.
from app.state.queries import _require_scope
from app.web.deps import current_user
from app.web.templating import build_templates

# The page shell and the tenant, decided in one place for every screen.
from app.web.views.proceedings import (
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    current_company_id,
    unresolved_escalations,
)

router = APIRouter()
templates = build_templates()

# The five contract paths hang off this one.
WORKFLOWS_URL = "/admin/workflows"
# The read-only screen, named here so the refusal page and the editor can point
# somebody without the permission at the thing they can read.
ROUTE_URL = "/workflow"

# The permission all five require. Spelt once; app/state/models.py defines it
# and app/auth/policy.py raises at import if a code in a gate stops existing.
MANAGE = "workflow.manage"

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE FOUR BELONG IN app/state/audit.py beside ACTION_PROJECT_CREATED and the
# rest, and they must be MOVED there rather than restated -- the module docstring
# there gives the reason, which is that a second spelling writes rows no query
# for the first spelling will ever return. They are here only because that file
# is owned by another agent in this build. This is the same note the schema work
# left on DIGEST_V3, and it is a handoff rather than a decision.
# ---------------------------------------------------------------------------

ACTION_WORKFLOW_CREATED = "workflow.created"
ACTION_WORKFLOW_SAVED = "workflow.saved"
ACTION_WORKFLOW_ACTIVATED = "workflow.activated"
ACTION_WORKFLOW_ARCHIVED = "workflow.archived"

REFUSED = (
    f"{MANAGE} is required to see or change the approval route. Ask an "
    "administrator. The route itself is readable by anyone signed in, at "
    f"{ROUTE_URL}."
)


# ---------------------------------------------------------------------------
# Scoped reads. Every one of these belongs in app/state/queries.py -- see the
# module docstring. company_id is keyword-only and has no default, which is the
# shape that file uses so the move is a cut and paste.
# ---------------------------------------------------------------------------


def workflows_for_company(session: Session, *, company_id: str) -> list[ApprovalWorkflow]:
    """Every route this company has drawn, newest first, drafts included."""
    _require_scope(company_id)
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .order_by(ApprovalWorkflow.created_at.desc(), ApprovalWorkflow.id.desc())
        .all()
    )


def workflow_for_company(
    session: Session, *, company_id: str, workflow_id: str
) -> ApprovalWorkflow | None:
    """One route, or None. Another company's id resolves to None, not to a row.

    None rather than a raise, matching change_for_company and user_for_company:
    the caller has an id from a URL and a 404 is the honest answer. What must
    never happen is the row coming back.
    """
    _require_scope(company_id)
    if not workflow_id:
        return None
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .filter(ApprovalWorkflow.id == workflow_id)
        .one_or_none()
    )


def active_workflow_for_company(
    session: Session, *, company_id: str
) -> ApprovalWorkflow | None:
    """The one live route, or None. The partial unique index makes "one" true."""
    _require_scope(company_id)
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .filter(ApprovalWorkflow.status == WORKFLOW_ACTIVE)
        .order_by(ApprovalWorkflow.id)
        .first()
    )


def steps_and_edges(
    session: Session, workflow: ApprovalWorkflow
) -> tuple[list[WorkflowStep], list[WorkflowEdge]]:
    """The graph of one route. Takes the row, so tenancy is already settled.

    WorkflowStep and WorkflowEdge carry no company_id -- they are meaningless
    outside their workflow and tenancy comes through it, the same argument
    Passage takes from DocumentVersion. Taking the workflow row rather than an
    id is what makes that safe here: the caller cannot reach this function
    without having already resolved the route under a company scope.
    """
    steps = (
        session.query(WorkflowStep)
        .filter(WorkflowStep.workflow_id == workflow.id)
        .all()
    )
    edges = (
        session.query(WorkflowEdge)
        .filter(WorkflowEdge.workflow_id == workflow.id)
        .all()
    )
    return sorted(steps, key=lambda row: step_order(row.id)), sorted(
        edges, key=lambda row: (step_order(row.from_step_id), step_order(row.to_step_id))
    )


# ---------------------------------------------------------------------------
# Serialisation. The columns are the contract, so this is a copy.
# ---------------------------------------------------------------------------


def step_order(step_id: str) -> tuple:
    """STP-2 before STP-10. Lexical order would put ten before two.

    The canvas numbers its own nodes and a route with more than nine steps is
    ordinary, so sorting the id as text would reorder the list the moment a
    tenth node was added -- with nothing on screen to say why.
    """
    head, _, tail = (step_id or "").rpartition("-")
    if tail.isdigit():
        return (0, int(tail), head)
    return (1, 0, step_id or "")


def graph_of(session: Session, workflow: ApprovalWorkflow) -> dict:
    """The wire contract's JSON for one route. No translation table."""
    steps, edges = steps_and_edges(session, workflow)
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "steps": [
            {
                "id": step.id,
                "label": step.label or "",
                "assignee_rule": step.assignee_rule or ASSIGNEE_UNASSIGNED,
                "approval_hours": step.approval_hours,
                "on_timeout": step.on_timeout,
                "escalate_to": step.escalate_to,
                "remind_every_hours": step.remind_every_hours,
                "x": step.x or 0,
                "y": step.y or 0,
            }
            for step in steps
        ],
        # from and to, not from_step_id and to_step_id. `from` is a Python
        # keyword, which is why the columns are spelt the long way; mapping them
        # back is this function's job and nobody else's.
        "edges": [{"from": edge.from_step_id, "to": edge.to_step_id} for edge in edges],
    }


# ---------------------------------------------------------------------------
# Errors, in the contract's shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphError:
    """One refusal, and the node it belongs to. None means the graph itself.

    The contract types step_id as a step id. A fact about the whole graph -- two
    starting points, a loop -- has no node to sit on, and inventing one would
    put the message on a node that is not at fault. Null is the honest answer
    and the editor renders those in the panel rather than on the canvas.
    """

    step_id: str | None
    message: str

    def as_dict(self) -> dict:
        return {"step_id": self.step_id, "message": self.message}


def _refusal(errors: list[GraphError], status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "errors": [error.as_dict() for error in errors]},
        status_code=status,
    )


_OK = {"ok": True}


# ---------------------------------------------------------------------------
# Resolving an assignee. Used by activation to refuse, and by the read-only
# screen to say who in plain words.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assignee:
    """What an assignee_rule or an escalate_to points at, and whether it lands.

    `resolved` is False when the rule is well-formed and names a role or a user
    this company does not have. That is a route into silence, and both the
    validator and the screen have to be able to see it: activation refuses it,
    and a route already live shows it in alarm rather than pretending somebody
    is holding the step.

    `per_item` is True only for obligation_owner, the one rule that cannot be
    resolved against the route because it resolves against the escalation.
    """

    rule: str
    kind: str
    name: str
    resolved: bool
    per_item: bool


def rule_is_well_formed(rule: str | None) -> bool:
    """Shape only: one of the two literals, or a prefix with something after it."""
    if not rule or not isinstance(rule, str):
        return False
    if rule in ASSIGNEE_RULE_LITERALS:
        return True
    return any(
        rule.startswith(prefix) and len(rule) > len(prefix)
        for prefix in ASSIGNEE_RULE_PREFIXES
    )


def target_is_well_formed(target: str | None) -> bool:
    """escalate_to takes the two prefixes and neither literal.

    obligation_owner as an escalation target would send a step that timed out
    back to the person who did not answer it, and unassigned would send it
    nowhere. The contract types this field as role: or user: and it is right.
    """
    if not target or not isinstance(target, str):
        return False
    return any(
        target.startswith(prefix) and len(target) > len(prefix)
        for prefix in ASSIGNEE_RULE_PREFIXES
    )


def resolve_assignee(session: Session, company_id: str, rule: str | None) -> Assignee:
    """Turn a rule into who it names, and whether this company has them.

    Reads roles and users through app/state/identity.py, which scopes both. A
    role from another tenant is not in roles_for_company, so a rule naming one
    resolves to False here -- which is the same answer as a role that was never
    created, and deliberately so.
    """
    _require_scope(company_id)
    text = rule or ""
    if text == ASSIGNEE_OBLIGATION_OWNER:
        return Assignee(text, "obligation_owner", "", True, True)
    if text == ASSIGNEE_UNASSIGNED or not text:
        return Assignee(text, "unassigned", "", False, False)

    if text.startswith(ASSIGNEE_ROLE_PREFIX):
        wanted = text[len(ASSIGNEE_ROLE_PREFIX) :]
        for role in roles_for_company(session, company_id):
            if role.name == wanted:
                return Assignee(text, "role", role.name, True, False)
        return Assignee(text, "role", wanted, False, False)

    if text.startswith(ASSIGNEE_USER_PREFIX):
        wanted = text[len(ASSIGNEE_USER_PREFIX) :]
        for user in users_for_company(session, company_id):
            if user.id == wanted:
                return Assignee(text, "user", user.display_name or user.email, True, False)
        return Assignee(text, "user", wanted, False, False)

    return Assignee(text, "malformed", text, False, False)


# ---------------------------------------------------------------------------
# Reading a posted graph. Types and vocabulary; nothing about completeness.
# ---------------------------------------------------------------------------


def _positive_int(value) -> int | None:
    """An integer above zero, or None. A bool is not an integer here.

    True passes isinstance(value, int) in Python and would be stored as 1 hour.
    A JSON true in an hours field is a mistake somewhere upstream, not a
    deadline, so it is refused rather than quietly rounded into one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _coordinate(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def read_graph(payload: dict, workflow: ApprovalWorkflow) -> tuple[dict, list[GraphError]]:
    """Check a posted graph, or say what is wrong with it. Writes nothing.

    Returns the cleaned graph and the refusals. Both are returned rather than
    raising on the first problem: an admin who mistyped two fields should be
    told about both, and the editor renders every message against its own node.
    """
    errors: list[GraphError] = []
    if not isinstance(payload, dict):
        return {}, [GraphError(None, "The body must be a graph object.")]

    # ABSENCE IS DENIAL, APPLIED TO A WRITE. A body with no `steps` key is not
    # a route with no steps: it is a save that did not say. Treating the two
    # alike would let an empty POST -- a client bug, a half-built request, a
    # retry that lost its body -- delete every step of a route and report
    # success. The editor always sends both keys, so nothing legitimate is
    # refused here.
    for required in ("steps", "edges"):
        if required not in payload:
            errors.append(
                GraphError(
                    None,
                    f"This save does not say what the {required} are. A body with "
                    f"no {required} is not the same as a route with no "
                    f"{required}, so nothing was written.",
                )
            )
    if errors:
        return {}, errors

    posted_id = payload.get("workflow_id")
    if posted_id is not None and posted_id != workflow.id:
        # Two editor tabs, or a graph pasted from somewhere else. Writing it
        # would overwrite one route with another's steps and say ok.
        return {}, [
            GraphError(
                None,
                f"This save is addressed to {workflow.id} and carries the graph "
                f"for {posted_id}. Nothing was written. Reload the editor.",
            )
        ]

    posted_status = payload.get("status")
    if posted_status is not None and posted_status != workflow.status:
        # The editor posts back what it was given, so this only fires when a
        # caller tried to change the status through the save path. Status moves
        # through activation, which validates; a save that could set it would be
        # a way round every check on this page.
        errors.append(
            GraphError(
                None,
                f"This route is {workflow.status} and the save says "
                f"{posted_status!r}. A route's state changes by activation, not "
                "by saving the graph. Reload the editor.",
            )
        )

    name = payload.get("name", workflow.name)
    if not isinstance(name, str) or not name.strip():
        errors.append(GraphError(None, "A route needs a name somebody can search for."))
        name = workflow.name

    raw_steps = payload.get("steps", [])
    if not isinstance(raw_steps, list):
        return {}, errors + [GraphError(None, "Steps must be a list.")]

    steps: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            errors.append(GraphError(None, f"Step {index + 1} is not an object."))
            continue
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            errors.append(GraphError(None, f"Step {index + 1} has no id."))
            continue
        step_id = step_id.strip()
        if step_id in seen:
            errors.append(
                GraphError(
                    step_id,
                    f"Two steps are called {step_id}. A step id is the only thing "
                    "an edge can point at, so it has to be unique in this route.",
                )
            )
            continue
        seen.add(step_id)

        label = raw.get("label", "")
        if not isinstance(label, str):
            errors.append(GraphError(step_id, "A label is text."))
            label = ""

        rule = raw.get("assignee_rule", ASSIGNEE_UNASSIGNED)
        if rule is None:
            # One spelling of absence. The contract has a word for nobody-yet
            # and NULL would be a second one, splitting every query over the
            # column without anyone noticing which half they got.
            rule = ASSIGNEE_UNASSIGNED
        if not rule_is_well_formed(rule):
            errors.append(
                GraphError(
                    step_id,
                    f"{rule!r} is not an assignee rule. Use obligation_owner, "
                    "unassigned, role:<name> or user:<id>.",
                )
            )
            continue

        hours = raw.get("approval_hours")
        if hours is not None and _positive_int(hours) is None:
            errors.append(
                GraphError(
                    step_id,
                    "Approval hours must be a whole number of hours above zero, "
                    "or left blank while you decide.",
                )
            )
            continue

        timeout = raw.get("on_timeout")
        if timeout is not None and timeout not in STEP_TIMEOUT_ACTIONS:
            errors.append(
                GraphError(
                    step_id,
                    f"{timeout!r} is not one of {', '.join(STEP_TIMEOUT_ACTIONS)}.",
                )
            )
            continue

        escalate_to = raw.get("escalate_to")
        if escalate_to is not None and not target_is_well_formed(escalate_to):
            errors.append(
                GraphError(
                    step_id,
                    f"{escalate_to!r} is not an escalation target. Use "
                    "role:<name> or user:<id>.",
                )
            )
            continue

        remind = raw.get("remind_every_hours")
        if remind is not None and _positive_int(remind) is None:
            errors.append(
                GraphError(
                    step_id,
                    "A reminder interval is a whole number of hours above zero, "
                    "or left blank.",
                )
            )
            continue

        x, y = _coordinate(raw.get("x", 0)), _coordinate(raw.get("y", 0))
        if x is None or y is None:
            errors.append(GraphError(step_id, "A canvas position is two integers."))
            continue

        steps.append(
            {
                "id": step_id,
                "label": label,
                "assignee_rule": rule,
                "approval_hours": hours,
                "on_timeout": timeout,
                "escalate_to": escalate_to,
                "remind_every_hours": remind,
                "x": x,
                "y": y,
            }
        )

    raw_edges = payload.get("edges", [])
    if not isinstance(raw_edges, list):
        return {}, errors + [GraphError(None, "Edges must be a list.")]

    edges: list[dict] = []
    drawn: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict):
            errors.append(GraphError(None, "An edge is an object with from and to."))
            continue
        tail, head = raw.get("from"), raw.get("to")
        if not isinstance(tail, str) or not isinstance(head, str):
            errors.append(GraphError(None, "An edge names two steps."))
            continue
        # Refused rather than stored. The composite foreign key would reject
        # this on PostgreSQL and SQLite would take it, so accepting it here
        # would mean the product behaves differently on the demo database from
        # the one it ships on -- and a draft carrying an arrow to nothing is
        # not the half-finished state the contract allows for.
        missing = [name for name in (tail, head) if name not in seen]
        if missing:
            errors.append(
                GraphError(
                    tail if tail in seen else None,
                    f"An arrow points at {', '.join(missing)}, which is not a "
                    "step in this route.",
                )
            )
            continue
        if tail == head:
            errors.append(GraphError(tail, "A step cannot wait for itself."))
            continue
        if (tail, head) in drawn:
            # A double click, not a decision. The composite primary key would
            # refuse the second row anyway; catching it here keeps the message
            # readable instead of an IntegrityError.
            continue
        drawn.add((tail, head))
        edges.append({"from": tail, "to": head})

    return {"name": name.strip(), "steps": steps, "edges": edges}, errors


def write_graph(session: Session, workflow: ApprovalWorkflow, graph: dict) -> None:
    """Replace the route's steps and edges with the posted ones.

    Whole, not merged. The canvas holds the truth about the graph -- a node the
    admin deleted is gone from the post and has to be gone from the table, and
    working out which rows to update, insert and delete would be a second model
    of the same graph living in this function. Edges go first so nothing is left
    pointing at a step that is about to be deleted, which matters on a database
    where foreign keys are enforced.
    """
    session.query(WorkflowEdge).filter(
        WorkflowEdge.workflow_id == workflow.id
    ).delete(synchronize_session=False)
    session.query(WorkflowStep).filter(
        WorkflowStep.workflow_id == workflow.id
    ).delete(synchronize_session=False)
    session.flush()

    workflow.name = graph["name"]
    for step in graph["steps"]:
        session.add(WorkflowStep(workflow_id=workflow.id, **step))
    session.flush()
    for edge in graph["edges"]:
        session.add(
            WorkflowEdge(
                workflow_id=workflow.id,
                from_step_id=edge["from"],
                to_step_id=edge["to"],
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Activation: where absence becomes denial
# ---------------------------------------------------------------------------


def validate_for_activation(
    session: Session, company_id: str, workflow: ApprovalWorkflow
) -> list[GraphError]:
    """Every reason this route cannot go live, each named against its step.

    Per step: it has a label, it names somebody, that somebody exists, it says
    how long it has, it says what happens when that runs out, and the answer it
    gives is complete -- a reminder needs an interval, an escalation needs a
    target that resolves.

    Per graph: it has at least one step, exactly one starting point, no loop,
    and every step is reachable from the start. A second starting point is a
    route with no answer to "where does an item enter"; a loop is a route an
    item never leaves; an unreachable step is a node an admin drew and wired to
    nothing, which on a live route means work that silently never happens.

    Returns them all rather than the first. An admin fixing one message at a
    time and pressing Activate between each is the workflow this list exists to
    avoid.
    """
    steps, edges = steps_and_edges(session, workflow)
    errors: list[GraphError] = []

    if not steps:
        return [
            GraphError(
                None,
                "This route has no steps, so there is nothing to route an item "
                "through. Add a step before activating.",
            )
        ]

    for step in steps:
        if not (step.label or "").strip():
            errors.append(
                GraphError(
                    step.id,
                    "This step has no label. Everyone who is not an admin reads "
                    "the route by its labels, and an unnamed step tells them "
                    "nothing about what is being asked.",
                )
            )

        rule = step.assignee_rule or ASSIGNEE_UNASSIGNED
        if rule == ASSIGNEE_UNASSIGNED:
            errors.append(
                GraphError(
                    step.id,
                    "This step names nobody to ask, so an item reaching it would "
                    "stop there. Choose who is asked.",
                )
            )
        else:
            who = resolve_assignee(session, company_id, rule)
            if not who.per_item and not who.resolved:
                errors.append(
                    GraphError(
                        step.id,
                        f"{rule} names nobody in this company. A step assigned to "
                        "somebody who does not exist routes into silence.",
                    )
                )

        if _positive_int(step.approval_hours) is None:
            errors.append(
                GraphError(
                    step.id,
                    "This step does not say how many hours it has. Without a "
                    "deadline nothing can time out, and nothing can be chased.",
                )
            )

        if step.on_timeout is None:
            errors.append(
                GraphError(
                    step.id,
                    "This step does not say what happens when its time runs out. "
                    "Choose remind, escalate or bypass -- and read the bypass "
                    "wording before choosing it.",
                )
            )
        elif step.on_timeout == TIMEOUT_REMIND and _positive_int(
            step.remind_every_hours
        ) is None:
            errors.append(
                GraphError(
                    step.id,
                    "This step reminds when it times out but does not say how "
                    "often. A reminder with no interval is never sent.",
                )
            )
        elif step.on_timeout == TIMEOUT_ESCALATE:
            if not target_is_well_formed(step.escalate_to):
                errors.append(
                    GraphError(
                        step.id,
                        "This step escalates when it times out but names nobody "
                        "to escalate to.",
                    )
                )
            else:
                target = resolve_assignee(session, company_id, step.escalate_to)
                if not target.resolved:
                    errors.append(
                        GraphError(
                            step.id,
                            f"{step.escalate_to} names nobody in this company, so "
                            "an escalation from this step would reach no one.",
                        )
                    )

    known = {step.id for step in steps}
    # Re-checked rather than trusted. SQLite does not enforce foreign keys
    # unless asked, so a row written before this rule existed, or by a script,
    # can carry an arrow to a step that is gone.
    live_edges = [
        edge for edge in edges if edge.from_step_id in known and edge.to_step_id in known
    ]
    for edge in edges:
        if edge not in live_edges:
            errors.append(
                GraphError(
                    edge.from_step_id if edge.from_step_id in known else None,
                    f"An arrow runs from {edge.from_step_id} to {edge.to_step_id}, "
                    "and one of those steps is not in this route.",
                )
            )

    incoming: dict[str, int] = {step.id: 0 for step in steps}
    outgoing: dict[str, list[str]] = {step.id: [] for step in steps}
    for edge in live_edges:
        incoming[edge.to_step_id] += 1
        outgoing[edge.from_step_id].append(edge.to_step_id)

    roots = [step_id for step_id, count in incoming.items() if count == 0]
    if not roots:
        errors.append(
            GraphError(
                None,
                "Every step in this route has something pointing at it, so there "
                "is no start. An item would have nowhere to enter.",
            )
        )
    elif len(roots) > 1:
        errors.append(
            GraphError(
                None,
                "This route has "
                + str(len(roots))
                + " steps with nothing pointing at them ("
                + ", ".join(sorted(roots, key=step_order))
                + "), so it has more than one start and nothing says which one "
                "an item uses. Connect them, or split this into two routes.",
            )
        )

    # Depth-first, colouring as it goes: grey while on the stack, black when
    # finished. An edge back to a grey node is a loop. Written out rather than
    # inferred from a reachability count, because the message has to name the
    # step the loop closes on and a count cannot.
    colour: dict[str, int] = {}
    loops: list[tuple[str, str]] = []

    def walk(node: str) -> None:
        colour[node] = 1
        for nxt in outgoing.get(node, ()):
            if colour.get(nxt, 0) == 1:
                loops.append((node, nxt))
            elif colour.get(nxt, 0) == 0:
                walk(nxt)
        colour[node] = 2

    for start in sorted(known, key=step_order):
        if colour.get(start, 0) == 0:
            walk(start)

    for tail, head in loops:
        errors.append(
            GraphError(
                tail,
                f"This step loops back to {head}. An item going round a loop "
                "never leaves the route.",
            )
        )

    if len(roots) == 1 and not loops:
        reached: set[str] = set()
        frontier = [roots[0]]
        while frontier:
            node = frontier.pop()
            if node in reached:
                continue
            reached.add(node)
            frontier.extend(outgoing.get(node, ()))
        for step_id in sorted(known - reached, key=step_order):
            errors.append(
                GraphError(
                    step_id,
                    f"Nothing reaches this step from {roots[0]}, so an item would "
                    "never arrive at it.",
                )
            )

    return errors


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _user_id(request: Request) -> str:
    principal = current_user(request)
    return principal.user_id if principal is not None else ""


def _may_manage(session: Session, company_id: str, user_id: str) -> str | None:
    """None when allowed, else the reason. The refusal is audited on the way out.

    require() writes an audit row and then raises. The raise is caught HERE,
    inside the caller's session scope, so the row commits with the response --
    catching it outside would roll the refusal back along with everything else,
    and a denial nobody recorded is a denial nobody can review.
    """
    try:
        policy.require(session, company_id, user_id, MANAGE)
    except policy.PermissionDenied as denied:
        return denied.reason
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(request: Request) -> tuple[str, str | None]:
    """The name for the audit row and the id beside it.

    Both, because they answer different questions: the id is the identity and
    the address is what a person reads a year later. Nobody signed in gives an
    empty id rather than an invented one -- the middleware makes that
    unreachable through the running application, and record_event would refuse
    an empty actor anyway.
    """
    principal = current_user(request)
    if principal is None:
        return "person:unauthenticated", None
    return principal.email, principal.user_id


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowRow:
    """One route in the list. A link, a state, and how big it is."""

    workflow_id: str
    name: str
    status: str
    badge: str
    url: str
    steps: int
    created_at: str
    activated_at: str | None
    supersedes_id: str | None


_STATUS_BADGE = {
    WORKFLOW_DRAFT: "badge--draft",
    WORKFLOW_ACTIVE: "badge--active",
    WORKFLOW_ARCHIVED: "badge--closed",
}


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _rows(session: Session, company_id: str) -> list[WorkflowRow]:
    rows = []
    for workflow in workflows_for_company(session, company_id=company_id):
        steps, _edges = steps_and_edges(session, workflow)
        rows.append(
            WorkflowRow(
                workflow_id=workflow.id,
                name=workflow.name,
                status=workflow.status,
                badge=_STATUS_BADGE.get(workflow.status, "badge--draft"),
                url=f"{WORKFLOWS_URL}/{workflow.id}",
                steps=len(steps),
                created_at=_stamp(workflow.created_at) or "",
                activated_at=_stamp(workflow.activated_at),
                supersedes_id=workflow.supersedes_id,
            )
        )
    return rows


def _waiting(session: Session, company_id: str) -> int:
    """The nav's held-for-review count. Zero on a database with no tables.

    Read on the refusal path too. A masthead that drops its count on one screen
    tells the reader the queue emptied, and it did not -- they were refused a
    different screen.
    """
    try:
        return len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        return 0


def _shell(company_id: str, waiting: int, title: str) -> dict:
    """base.html's context, with the nav count."""
    context = chrome(
        company_id,
        page_title=title,
        # Not "workflow": the read-only route is a different screen and marking
        # its nav item as the current page while an admin is in the editor
        # would be a small lie in an aria-current attribute.
        nav_active="",
        review_count=waiting,
    )
    context["route_url"] = ROUTE_URL
    context["workflows_url"] = WORKFLOWS_URL
    return context


def _forbidden_page(
    request: Request, company_id: str, reason: str, waiting: int = 0
) -> HTMLResponse:
    context = _shell(company_id, waiting, "Approval route")
    context.update({"reason": reason, "refused": REFUSED, "permission": MANAGE})
    return templates.TemplateResponse(
        request, "workflow_list.html", context, status_code=403
    )


def _forbidden_json(reason: str) -> JSONResponse:
    return _refusal([GraphError(None, f"{reason}. {REFUSED}")], status=403)


@router.get(WORKFLOWS_URL, response_class=HTMLResponse)
def workflow_list(request: Request) -> HTMLResponse:
    """Every route this company has drawn, with its state and a way in."""
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, rows, state, waiting = None, [], STATE_OK, 0
    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, user_id)
            if reason is None:
                rows = _rows(session, company_id)
            context = _shell(company_id, waiting, "Approval routes")
    except SQLAlchemyError:
        # A database that cannot be read is a permission check that did not
        # run, and a check that did not run is not a check that passed. On a
        # clone with no tables this screen would otherwise open for anybody,
        # empty -- which shows no data but is still the gate failing open, and
        # a gate that fails open once fails open again on a database that is
        # merely unreachable rather than merely unseeded.
        state = STATE_NO_TABLES
        reason = (
            "this company's roles could not be read, so nothing confirmed that "
            f"you hold {MANAGE}"
        )
        context = _shell(company_id, 0, "Approval routes")

    if reason is not None:
        return _forbidden_page(request, company_id, reason, waiting)

    context.update({"rows": rows, "state": state})
    return templates.TemplateResponse(request, "workflow_list.html", context)


@router.post(WORKFLOWS_URL)
def create_workflow(request: Request, name: str = Form(default="")):
    """Open a new draft and go straight to its canvas.

    NOT ONE OF THE CONTRACT'S FIVE. The contract describes how a route is read,
    saved and activated and is silent on where the first one comes from, and a
    product where an admin cannot make one is a product with an editor nobody
    can open. It is a plain HTML form post so the list screen works with no
    script at all, and it adds no field to the graph shape.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    title = " ".join((name or "").split()) or "Untitled approval route"

    with session_scope() as session:
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            workflow_id = _next_workflow_id(session, company_id)
            session.add(
                ApprovalWorkflow(
                    id=workflow_id,
                    company_id=company_id,
                    name=title,
                    status=WORKFLOW_DRAFT,
                    created_by_user_id=user_id or None,
                )
            )
            session.flush()
            who, who_id = _actor(request)
            record_event(
                session,
                company_id=company_id,
                actor=who,
                action=ACTION_WORKFLOW_CREATED,
                subject_type="workflow",
                subject_id=workflow_id,
                reason=f"opened draft approval route {title!r}",
                actor_user_id=who_id,
                actor_kind=ACTOR_USER if who_id else ACTOR_SYSTEM,
            )

    if reason is not None:
        return _forbidden_page(request, company_id, reason, waiting)
    return RedirectResponse(url=f"{WORKFLOWS_URL}/{workflow_id}", status_code=303)


def _next_workflow_id(session: Session, company_id: str) -> str:
    """WF-0001, WF-0002, counted within this company and checked for collision.

    The number comes from this company's own rows, so one tenant's count does
    not tell another tenant anything. The primary key is global, so a candidate
    is then checked with a COUNT rather than a get: a count answers "is this
    string taken" without a row from another company ever being loaded, which
    an insert would answer anyway when it failed.
    """
    mine = workflows_for_company(session, company_id=company_id)
    used = [
        int(row.id.rpartition("-")[2])
        for row in mine
        if row.id.rpartition("-")[2].isdigit()
    ]
    number = (max(used) + 1) if used else 1
    while True:
        candidate = f"WF-{number:04d}"
        taken = session.query(func.count()).select_from(ApprovalWorkflow).filter(
            ApprovalWorkflow.id == candidate
        ).scalar()
        if not taken:
            return candidate
        number += 1


@router.get(WORKFLOWS_URL + "/{workflow_id}", response_class=HTMLResponse)
def workflow_editor(request: Request, workflow_id: str) -> HTMLResponse:
    """The canvas. The graph is embedded in the page, so drawing needs no fetch.

    Same argument as citation.js: what the screen is about must not depend on a
    second request succeeding. Saving needs the network, and it says so when it
    fails; opening the editor does not.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, graph, found, waiting = None, None, False, 0
    roles: list[str] = []
    people: list[dict] = []
    with session_scope() as session:
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            workflow = workflow_for_company(
                session, company_id=company_id, workflow_id=workflow_id
            )
            found = workflow is not None
            if workflow is not None:
                graph = graph_of(session, workflow)
                # The real roles and the real accounts, so the menus offer what
                # exists rather than a free-text box that saves a name nobody
                # holds and fails at activation. Both reads are scoped by
                # app/state/identity.py.
                roles = [role.name for role in roles_for_company(session, company_id)]
                people = [
                    {"id": user.id, "name": user.display_name or user.email}
                    for user in users_for_company(session, company_id)
                ]
            context = _shell(company_id, waiting, "Approval route")

    if reason is not None:
        return _forbidden_page(request, company_id, reason, waiting)
    if not found:
        raise HTTPException(status_code=404, detail="no such approval route")

    context.update(
        {
            "graph": graph,
            "workflow_id": workflow_id,
            "editable": graph["status"] == WORKFLOW_DRAFT,
            "graph_url": f"{WORKFLOWS_URL}/{workflow_id}/graph",
            "activate_url": f"{WORKFLOWS_URL}/{workflow_id}/activate",
            "timeout_actions": STEP_TIMEOUT_ACTIONS,
            "timeout_remind": TIMEOUT_REMIND,
            "timeout_escalate": TIMEOUT_ESCALATE,
            "timeout_bypass": TIMEOUT_BYPASS,
            "assignee_literals": ASSIGNEE_RULE_LITERALS,
            "role_prefix": ASSIGNEE_ROLE_PREFIX,
            "user_prefix": ASSIGNEE_USER_PREFIX,
            "draft_status": WORKFLOW_DRAFT,
            "roles": roles,
            "users": people,
        }
    )
    return templates.TemplateResponse(request, "workflow_edit.html", context)


@router.get(WORKFLOWS_URL + "/{workflow_id}/graph")
def workflow_graph(request: Request, workflow_id: str):
    """The graph as JSON, exactly as the contract types it."""
    company_id = current_company_id()
    user_id = _user_id(request)
    with session_scope() as session:
        reason = _may_manage(session, company_id, user_id)
        graph = None
        if reason is None:
            workflow = workflow_for_company(
                session, company_id=company_id, workflow_id=workflow_id
            )
            if workflow is not None:
                graph = graph_of(session, workflow)

    # Permission before existence, so a refusal cannot be used to find out which
    # ids exist. Somebody without the code learns the same thing for a real id
    # and an invented one -- the ordering app/auth/policy.py::can_approve takes.
    if reason is not None:
        return _forbidden_json(reason)
    if graph is None:
        return _refusal(
            [GraphError(None, "No such approval route.")], status=404
        )
    return JSONResponse(graph)


@router.post(WORKFLOWS_URL + "/{workflow_id}/graph")
def save_graph(request: Request, workflow_id: str, payload: dict | None = Body(default=None)):
    """Save the whole graph. A half-finished draft is allowed; a wrong one is not.

    409 rather than 200 when the route is frozen: the body says ok false either
    way and the editor reads the body, but the status is what a proxy, a log or
    a person reading a network tab sees, and "the thing you asked for is not in
    a state that allows it" is what 409 means.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    outcome: JSONResponse | None = None

    with session_scope() as session:
        reason = _may_manage(session, company_id, user_id)
        if reason is not None:
            outcome = _forbidden_json(reason)
        else:
            workflow = workflow_for_company(
                session, company_id=company_id, workflow_id=workflow_id
            )
            if workflow is None:
                outcome = _refusal([GraphError(None, "No such approval route.")], 404)
            elif workflow.status != WORKFLOW_DRAFT:
                outcome = _refusal(
                    [
                        GraphError(
                            None,
                            f"This route is {workflow.status} and cannot be edited. "
                            "Activation freezes a route so that a run in flight "
                            "cannot change under it. Copy it into a new draft "
                            "instead.",
                        )
                    ],
                    409,
                )
            else:
                # `payload or {}` would turn a request with no body into an
                # empty graph, which read_graph refuses on its own -- but the
                # message would be about missing keys rather than a missing
                # body, and the honest answer names what actually happened.
                graph, errors = read_graph(
                    payload if payload is not None else None, workflow
                )
                if errors:
                    outcome = _refusal(errors, 200)
                else:
                    write_graph(session, workflow, graph)
                    who, who_id = _actor(request)
                    record_event(
                        session,
                        company_id=company_id,
                        actor=who,
                        action=ACTION_WORKFLOW_SAVED,
                        subject_type="workflow",
                        subject_id=workflow.id,
                        reason=(
                            f"saved draft with {len(graph['steps'])} step(s) and "
                            f"{len(graph['edges'])} arrow(s)"
                        ),
                        actor_user_id=who_id,
                        actor_kind=ACTOR_USER if who_id else ACTOR_SYSTEM,
                    )
                    outcome = JSONResponse(_OK)

    return outcome


@router.post(WORKFLOWS_URL + "/{workflow_id}/activate")
def activate_workflow(request: Request, workflow_id: str):
    """Validate, then make this the live route. Refuses with every reason at once.

    Activating a second route archives the first and records that this one
    supersedes it. That is the only way to change a live route -- the old rows
    are never edited, so a run that pinned them still reads the graph it ran
    under. Both the archiving and the activation go into the chain, because
    "which route was live on the day this was approved" is the question an
    auditor asks and it has to be answerable from the log rather than from a
    column that has since moved.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    outcome: JSONResponse | None = None

    with session_scope() as session:
        reason = _may_manage(session, company_id, user_id)
        if reason is not None:
            outcome = _forbidden_json(reason)
        else:
            workflow = workflow_for_company(
                session, company_id=company_id, workflow_id=workflow_id
            )
            if workflow is None:
                outcome = _refusal([GraphError(None, "No such approval route.")], 404)
            elif workflow.status != WORKFLOW_DRAFT:
                outcome = _refusal(
                    [
                        GraphError(
                            None,
                            f"This route is already {workflow.status}. Only a draft "
                            "can be activated.",
                        )
                    ],
                    409,
                )
            else:
                errors = validate_for_activation(session, company_id, workflow)
                if errors:
                    outcome = _refusal(errors, 200)
                else:
                    who, who_id = _actor(request)
                    kind = ACTOR_USER if who_id else ACTOR_SYSTEM
                    previous = active_workflow_for_company(
                        session, company_id=company_id
                    )
                    if previous is not None:
                        previous.status = WORKFLOW_ARCHIVED
                        session.flush()
                        record_event(
                            session,
                            company_id=company_id,
                            actor=who,
                            action=ACTION_WORKFLOW_ARCHIVED,
                            subject_type="workflow",
                            subject_id=previous.id,
                            reason=(
                                f"archived on activation of {workflow.id}; runs "
                                "already started keep reading it"
                            ),
                            actor_user_id=who_id,
                            actor_kind=kind,
                        )
                        if workflow.supersedes_id is None:
                            workflow.supersedes_id = previous.id

                    workflow.status = WORKFLOW_ACTIVE
                    workflow.activated_at = _now()
                    session.flush()
                    steps, edges = steps_and_edges(session, workflow)
                    record_event(
                        session,
                        company_id=company_id,
                        actor=who,
                        action=ACTION_WORKFLOW_ACTIVATED,
                        subject_type="workflow",
                        subject_id=workflow.id,
                        reason=(
                            f"{workflow.name!r} is the live approval route: "
                            f"{len(steps)} step(s), {len(edges)} arrow(s)"
                            + (
                                f", superseding {workflow.supersedes_id}"
                                if workflow.supersedes_id
                                else ""
                            )
                        ),
                        actor_user_id=who_id,
                        actor_kind=kind,
                    )
                    outcome = JSONResponse(_OK)

    return outcome


def may_manage(session: Session, company_id: str, user_id: str) -> bool:
    """For rendering only. The read-only screen asks this to decide on a link.

    has() and not require(): this is a question about what to draw, and
    auditing every hidden link would fill the chain with rows nobody decided.
    The gate is _may_manage above, and it runs at the point of the act.
    """
    if not user_id:
        return False
    return policy.has(session, company_id, user_id, MANAGE)


__all__ = [
    "ACTION_WORKFLOW_ACTIVATED",
    "ACTION_WORKFLOW_ARCHIVED",
    "ACTION_WORKFLOW_CREATED",
    "ACTION_WORKFLOW_SAVED",
    "MANAGE",
    "REFUSED",
    "ROUTE_URL",
    "WORKFLOWS_URL",
    "Assignee",
    "GraphError",
    "WorkflowRow",
    "active_workflow_for_company",
    "graph_of",
    "may_manage",
    "read_graph",
    "resolve_assignee",
    "router",
    "rule_is_well_formed",
    "step_order",
    "steps_and_edges",
    "target_is_well_formed",
    "templates",
    "validate_for_activation",
    "workflow_for_company",
    "workflows_for_company",
    "write_graph",
]
