"""The approval route, in words, for everybody who is not an admin.

THE PAIN THIS ANSWERS, stated the way the analyst stated it. They hand an item
off and it disappears. They cannot say who has it, how long that person has had
it, what happens if nobody acts, or what comes next. So they chase it by email,
which is the workflow this product claims to replace. A route nobody can see is
a black box, and a black box in a compliance product gets worked around.

IT IS NOT THE EDITOR WITH THE BUTTONS GREYED OUT. It is a different screen for a
different question. The editor answers "what shall the route be" and is a canvas
of nodes and arrows because that is how you draw a graph. This answers "where is
my thing and what happens next", and the shape of that answer is a numbered list
of steps in the order an item meets them, each one written as a sentence. There
is no canvas here, no drag, and no JavaScript at all: the whole page is rendered
on the server, so the one screen every signed-in person depends on cannot be
broken by a script failing to load.

THE VOCABULARY DOES NOT REACH THE PAGE. `on_timeout: escalate` is a value in a
column. What a person needs is "after 24 hours with no answer, this goes to
anyone holding the Regulatory Affairs role", which they can act on. Every
translation is in this module and none of it is in the template, so there is one
place to check that no column value leaks into prose.

THE BYPASS SENTENCE IS NOT SOFTENED. "After 48 hours this step is SKIPPED and
the item moves on WITHOUT approval." Somebody reading this screen is deciding
whether to trust the process, and a route that describes a bypass in the same
tone as an approval has misled them into trusting something they would not have.
The same rule applies to a step that has already been bypassed on a live item:
it renders at equal visual weight to an approved one and says plainly that it
was skipped rather than signed. A route that renders a bypassed step to look
like an approved one is the same defect as a claim asserted on a citation that
did not verify.

AND IT SAYS WHO CAN CHANGE IT. One line, shown to everyone, naming the
permission and where the route is drawn. For somebody who holds
workflow.manage that line is the link into the editor; for everybody else it is
text naming who to ask. Never a link that would refuse them, and never hidden --
somebody who thinks a rule is hard-coded stops asking for the rule they need.

WHAT THIS SCREEN CANNOT DO YET, said plainly rather than left to be discovered.
Nothing in this build creates a WorkflowRun. The rows exist, this screen reads
them, and until the engine that walks a route is written the "where has my item
reached" half of the page will say that no run has been started. It says exactly
that, rather than rendering an empty list that reads as "nothing has happened".
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.state.db import session_scope
from app.state.models import (
    OUTCOME_APPROVED,
    OUTCOME_BYPASSED,
    OUTCOME_CANCELLED,
    OUTCOME_ESCALATED,
    OUTCOME_REJECTED,
    TIMEOUT_BYPASS,
    TIMEOUT_ESCALATE,
    TIMEOUT_REMIND,
    WorkflowRun,
    WorkflowStepRun,
)
from app.state.queries import _require_scope
from app.web import TEMPLATES_DIR
from app.web.deps import current_user

# The editor's own module. Imported rather than rebuilt: a second reader of the
# same tables would be a second answer to "what is the live route", and on the
# day the two disagreed there would be no way to say which screen was lying.
# Same argument review_centre.py makes for embedding review.py.
from app.web.views.admin import (
    MANAGE,
    WORKFLOWS_URL,
    active_workflow_for_company,
    may_manage,
    resolve_assignee,
    step_order,
    steps_and_edges,
)
from app.web.views.proceedings import (
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    current_company_id,
    unresolved_escalations,
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

ROUTE_URL = "/workflow"

# No live route is a fact, not an empty list. A blank screen here would read as
# "there is no approval step", which is a different and much more dangerous
# claim than "nobody has drawn one yet".
NO_ROUTE = (
    "There is no active approval route for this company. Nothing is being routed "
    "for approval, and an item handed off today would sit wherever it was left."
)

# The fallback, announcing itself (best-practices §26). A validated route has one
# start and no loop, so the ordered walk below always works on a live one. If it
# ever does not, the page says the order is the step numbering rather than the
# route, instead of quietly presenting one as the other.
ORDER_FALLBACK = (
    "This route does not read as a single ordered path, so the steps below are "
    "listed by their number and not by the order an item meets them."
)


# ---------------------------------------------------------------------------
# Scoped reads. These belong in app/state/queries.py with the four in
# app/web/views/admin.py -- see that module's docstring for why they are not
# there yet. company_id is keyword-only with no default.
# ---------------------------------------------------------------------------


def run_for_escalation(
    session: Session, *, company_id: str, escalation_id: str
) -> WorkflowRun | None:
    """The latest run walking one escalation, or None. Another tenant's is None."""
    _require_scope(company_id)
    if not escalation_id:
        return None
    return (
        session.query(WorkflowRun)
        .filter(WorkflowRun.company_id == company_id)
        .filter(WorkflowRun.escalation_id == escalation_id)
        .order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
        .first()
    )


def run_for_company(
    session: Session, *, company_id: str, run_id: str
) -> WorkflowRun | None:
    _require_scope(company_id)
    if not run_id:
        return None
    return (
        session.query(WorkflowRun)
        .filter(WorkflowRun.company_id == company_id)
        .filter(WorkflowRun.id == run_id)
        .one_or_none()
    )


def step_runs_for_run(
    session: Session, *, company_id: str, run_id: str
) -> list[WorkflowStepRun]:
    """Every attempt at every step of one run, oldest first.

    A step may run more than once -- it closes as escalated and reopens with a
    new assignee -- so this is a history and not a lookup table. The screen
    reads the LAST row for each step, because that is the state the step is in,
    and counts the earlier ones as attempts.
    """
    _require_scope(company_id)
    if not run_id:
        return []
    return (
        session.query(WorkflowStepRun)
        .filter(WorkflowStepRun.company_id == company_id)
        .filter(WorkflowStepRun.run_id == run_id)
        .order_by(WorkflowStepRun.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Vocabulary to prose. Every translation is here; none is in the template.
# ---------------------------------------------------------------------------


def _hours(count: int | None) -> str:
    if count is None:
        return "no deadline"
    return "1 hour" if count == 1 else f"{count} hours"


def _pretty_role(name: str) -> str:
    """obligation_owner -> Obligation owner. The role's own name, made readable.

    Not a lookup table of nicer words. A table would be a second name for every
    role, free to disagree with the one an admin sees in the editor and in the
    grant screen, and a person asking "who holds that" would be asking about a
    name the product does not use anywhere else.
    """
    words = (name or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


@dataclass(frozen=True, slots=True)
class Who:
    """Who a step asks, in words, and whether that lands on anybody.

    TWO FORMS OF THE SAME PHRASE. `text` starts a sentence and `inline` sits in
    the middle of one. Each branch writes both rather than a rule lower-casing
    the first letter on the way past, because one of these values is a person's
    display name: "goes to alicia ferreira" is the same mistake as "goes to
    Anyone holding the Admin role", pointed the other way. Each branch knows
    which of its own words is a proper noun; a general rule does not.

    `certain` is False when the rule names a role or a user this company does
    not have. The screen shows those in alarm: a live route with a step assigned
    to nobody is a step an item will stop at, and a reader deciding whether to
    trust the process has to be able to see it.
    """

    text: str
    inline: str
    certain: bool


def describe_assignee(session: Session, company_id: str, rule: str | None) -> Who:
    who = resolve_assignee(session, company_id, rule)
    if who.kind == "obligation_owner":
        return Who(
            "The obligation owner named on the item",
            "the obligation owner named on the item",
            True,
        )
    if who.kind == "unassigned":
        return Who(
            "Nobody is named yet, so an item would stop here",
            "nobody, because no one is named yet",
            False,
        )
    if who.kind == "role":
        role = _pretty_role(who.name)
        if who.resolved:
            return Who(
                f"Anyone holding the {role} role",
                f"anyone holding the {role} role",
                True,
            )
        return Who(
            f"A role this company does not have ({who.name}). Nobody would be asked.",
            f"a role this company does not have ({who.name}), so nobody would be asked",
            False,
        )
    if who.kind == "user":
        if who.resolved:
            # A person's name, unchanged in both forms: a proper noun reads the
            # same at the start of a sentence and in the middle of one.
            return Who(who.name, who.name, True)
        return Who(
            f"An account this company does not have ({who.name}). Nobody would be asked.",
            f"an account this company does not have ({who.name}), so nobody would "
            "be asked",
            False,
        )
    return Who(
        f"An assignee the product cannot read ({who.rule})",
        f"an assignee the product cannot read ({who.rule})",
        False,
    )


def describe_timeout(
    session: Session,
    company_id: str,
    *,
    approval_hours: int | None,
    on_timeout: str | None,
    escalate_to: str | None,
    remind_every_hours: int | None,
) -> tuple[str, bool]:
    """What happens when the clock runs out, as a sentence, and is it a skip.

    The boolean is the one thing the template branches on, and it is separate
    from the words so that a change of wording cannot accidentally change which
    sentences are drawn in alarm.
    """
    window = _hours(approval_hours)

    if on_timeout is None:
        return (
            "This step does not say what happens when its time runs out, so "
            "nothing would happen and the item would wait indefinitely.",
            False,
        )

    if on_timeout == TIMEOUT_BYPASS:
        # Not softened, on purpose. See the module docstring.
        return (
            f"After {window} this step is SKIPPED and the item moves on "
            "WITHOUT approval.",
            True,
        )

    if on_timeout == TIMEOUT_REMIND:
        if remind_every_hours is None:
            return (
                f"After {window} with no answer this step is set to remind, but "
                "no interval is set, so no reminder is sent and the item waits.",
                False,
            )
        return (
            f"After {window} with no answer, a reminder goes out every "
            f"{_hours(remind_every_hours)}. Nobody else is asked and the item "
            "does not move on.",
            False,
        )

    if on_timeout == TIMEOUT_ESCALATE:
        target = describe_assignee(session, company_id, escalate_to)
        if not escalate_to:
            return (
                f"After {window} with no answer this step escalates, but names "
                "nobody to escalate to, so it would reach no one.",
                False,
            )
        return (f"After {window} with no answer, this goes to {target.inline}.", False)

    return (f"After {window}, {on_timeout}.", False)


# The outcome words. Exactly one of them means a person said yes, and the rest
# say what actually happened instead. "Completed" is not in this map anywhere,
# because a run reaching its end says nothing about whether anybody approved.
_OUTCOME_WORDS = {
    OUTCOME_APPROVED: ("Approved", False),
    OUTCOME_REJECTED: ("Rejected", False),
    OUTCOME_BYPASSED: ("Skipped without approval", True),
    OUTCOME_ESCALATED: ("Passed on unanswered", True),
    OUTCOME_CANCELLED: ("Cancelled before it was answered", True),
}


# ---------------------------------------------------------------------------
# What the template sees
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteStep:
    """One step, already in words. The template prints; it does not decide."""

    step_id: str
    position: int
    label: str
    who: str
    who_certain: bool
    window: str
    timeout_sentence: str
    timeout_is_skip: bool
    next_labels: tuple[str, ...]
    # Set only when the viewer arrived from an item.
    state: str
    state_is_skip: bool
    is_current: bool
    # Both already carry their own words. A bare timestamp beside a bypassed
    # step reads as the moment somebody signed it, and a bare timestamp beside
    # an open one reads as the moment somebody answered. Neither is true, and
    # the fix is that this view says which it is rather than the template
    # printing a coordinate and leaving the reader to guess.
    holder: str | None
    when: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ItemState:
    """Where one item has reached, or why the screen cannot say."""

    escalation_id: str | None
    run_id: str | None
    known: bool
    note: str


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _walk(steps, edges) -> tuple[list, bool]:
    """The steps in the order an item meets them, and whether that order is real.

    Depth-first from the one step nothing points at, taking branches in step
    order so the list is stable between reloads. Returns ordered=False when the
    route has no single start or does not cover every step, and the caller then
    says so on the page rather than presenting the fallback as the route.
    """
    by_id = {step.id: step for step in steps}
    incoming = {step.id: 0 for step in steps}
    outgoing: dict[str, list[str]] = {step.id: [] for step in steps}
    for edge in edges:
        if edge.from_step_id in by_id and edge.to_step_id in by_id:
            incoming[edge.to_step_id] += 1
            outgoing[edge.from_step_id].append(edge.to_step_id)

    roots = [step_id for step_id, count in incoming.items() if count == 0]
    if len(roots) != 1:
        return sorted(steps, key=lambda step: step_order(step.id)), False

    order: list = []
    seen: set[str] = set()
    frontier = [roots[0]]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        order.append(by_id[node])
        frontier.extend(sorted(outgoing.get(node, ()), key=step_order, reverse=True))

    if len(order) != len(steps):
        return sorted(steps, key=lambda step: step_order(step.id)), False
    return order, True


def _last_by_step(rows) -> dict[str, WorkflowStepRun]:
    """The state each step is in now. A step may have run more than once."""
    latest: dict[str, WorkflowStepRun] = {}
    for row in rows:
        latest[row.step_id] = row
    return latest


def _attempts(rows) -> dict[str, int]:
    counted: dict[str, int] = {}
    for row in rows:
        counted[row.step_id] = counted.get(row.step_id, 0) + 1
    return counted


def _holder(session: Session, company_id: str, user_id: str | None) -> str | None:
    """The name of whoever holds a step, or None. Never an id dressed as a name."""
    if not user_id:
        return None
    who = resolve_assignee(session, company_id, f"user:{user_id}")
    return who.name if who.resolved else f"an account this company does not have ({user_id})"


@router.get(ROUTE_URL, response_class=HTMLResponse)
def approval_route(
    request: Request,
    escalation: str | None = Query(default=None),
    run: str | None = Query(default=None),
) -> HTMLResponse:
    """The live approval route, and where one item has reached in it.

    Any signed-in user. No workflow.manage: the whole point of the screen is
    that the people who do not draw the route are the ones who have to be able
    to read it.

    The item comes from the query string -- ?escalation= is what an analyst has
    in hand coming off the review queue, and ?run= is for a link built from the
    run itself. Neither is required, and an id from another company resolves to
    nothing here exactly as it does everywhere else.
    """
    company_id = current_company_id()
    principal = current_user(request)
    user_id = principal.user_id if principal is not None else ""

    state = STATE_OK
    rows: list[RouteStep] = []
    ordered = True
    name, waiting = "", 0
    item = ItemState(escalation, run, False, "")
    can_edit = False

    try:
        with session_scope() as session:
            try:
                waiting = len(unresolved_escalations(session, company_id))
            except SQLAlchemyError:
                waiting = 0
            can_edit = may_manage(session, company_id, user_id)
            workflow = active_workflow_for_company(session, company_id=company_id)
            if workflow is None:
                state = "no-route"
            else:
                name = workflow.name
                steps, edges = steps_and_edges(session, workflow)
                walked, ordered = _walk(steps, edges)
                item, latest, tries = _item_state(
                    session, company_id, workflow.id, escalation, run
                )
                labels = {step.id: (step.label or step.id) for step in steps}
                nexts: dict[str, list[str]] = {step.id: [] for step in steps}
                for edge in edges:
                    if edge.from_step_id in nexts and edge.to_step_id in labels:
                        nexts[edge.from_step_id].append(labels[edge.to_step_id])

                current = None
                if item.known:
                    current = _current_step_id(session, company_id, item.run_id)

                for position, step in enumerate(walked, start=1):
                    rows.append(
                        _row(
                            session,
                            company_id,
                            step,
                            position,
                            tuple(nexts.get(step.id, ())),
                            latest.get(step.id),
                            tries.get(step.id, 0),
                            item.known,
                            step.id == current,
                        )
                    )
    except SQLAlchemyError:
        state = STATE_NO_TABLES

    context = chrome(
        company_id,
        page_title="Approval route",
        nav_active="workflow",
        review_count=waiting,
    )
    context.update(
        {
            "state": state,
            "route_name": name,
            "steps": rows,
            "ordered": ordered,
            "order_fallback": ORDER_FALLBACK,
            "no_route": NO_ROUTE,
            "item": item,
            "can_edit": can_edit,
            "permission": MANAGE,
            "workflows_url": WORKFLOWS_URL,
        }
    )
    return templates.TemplateResponse(request, "workflow_route.html", context)


def _item_state(
    session: Session,
    company_id: str,
    workflow_id: str,
    escalation: str | None,
    run_id: str | None,
) -> tuple[ItemState, dict, dict]:
    """Resolve the item in the query string to a run, or say why it did not.

    Every "no" is a sentence rather than an absence. An item with no run has not
    started; an item whose run pinned an older route is being walked through a
    graph this page is not showing, and saying so is the only honest answer --
    the route on screen is the live one and the item's is not.
    """
    if not escalation and not run_id:
        return ItemState(None, None, False, ""), {}, {}

    found = (
        run_for_company(session, company_id=company_id, run_id=run_id)
        if run_id
        else run_for_escalation(
            session, company_id=company_id, escalation_id=escalation or ""
        )
    )
    if found is None:
        subject = run_id or escalation
        return (
            ItemState(
                escalation,
                run_id,
                False,
                f"An approval run has not been started for {subject} in this "
                "company, so it has not entered the route below. Nothing here "
                "is holding it.",
            ),
            {},
            {},
        )

    if found.workflow_id != workflow_id:
        return (
            ItemState(
                escalation,
                found.id,
                False,
                f"{escalation or found.id} is being walked through an earlier "
                f"version of the route ({found.workflow_id}), not the live one "
                "shown below. The route it started under cannot change, which is "
                "why the two can differ.",
            ),
            {},
            {},
        )

    rows = step_runs_for_run(session, company_id=company_id, run_id=found.id)
    return (
        ItemState(escalation or found.escalation_id, found.id, True, ""),
        _last_by_step(rows),
        _attempts(rows),
    )


def _current_step_id(session: Session, company_id: str, run_id: str | None) -> str | None:
    if not run_id:
        return None
    found = run_for_company(session, company_id=company_id, run_id=run_id)
    return found.current_step_id if found is not None else None


def _row(
    session: Session,
    company_id: str,
    step,
    position: int,
    next_labels: tuple[str, ...],
    step_run,
    attempts: int,
    tracking: bool,
    is_current: bool,
) -> RouteStep:
    who = describe_assignee(session, company_id, step.assignee_rule)
    sentence, is_skip = describe_timeout(
        session,
        company_id,
        approval_hours=step.approval_hours,
        on_timeout=step.on_timeout,
        escalate_to=step.escalate_to,
        remind_every_hours=step.remind_every_hours,
    )

    state, state_is_skip, holder, when = "", False, None, None
    if tracking:
        if step_run is None:
            state = "Not reached yet"
        elif step_run.outcome is None:
            # NULL is not consent. It means the step is open -- waiting.
            state = "Waiting here now" if is_current else "Open, not yet answered"
            who_holds = _holder(session, company_id, step_run.assigned_to_user_id)
            holder = (
                f"With {who_holds}" if who_holds else "Nobody is assigned to it"
            )
            stamp = _stamp(step_run.due_at)
            when = f"due {stamp}" if stamp else None
        else:
            state, state_is_skip = _OUTCOME_WORDS.get(
                step_run.outcome, (step_run.outcome, True)
            )
            actor = _holder(session, company_id, step_run.acted_by_user_id)
            # No actor on a bypassed, escalated or cancelled row, and naming one
            # would be a false attribution: the clock is not a person. Said in
            # words rather than left as a blank the reader fills in themselves.
            holder = actor if actor else "Nobody acted. The clock did."
            stamp = _stamp(step_run.acted_at)
            when = f"recorded {stamp}" if stamp else None

    return RouteStep(
        step_id=step.id,
        position=position,
        label=step.label or step.id,
        who=who.text,
        who_certain=who.certain,
        window=_hours(step.approval_hours),
        timeout_sentence=sentence,
        timeout_is_skip=is_skip,
        next_labels=next_labels,
        state=state,
        state_is_skip=state_is_skip,
        is_current=is_current,
        holder=holder,
        when=when,
        attempts=attempts,
    )


__all__ = [
    "NO_ROUTE",
    "ORDER_FALLBACK",
    "ROUTE_URL",
    "ItemState",
    "RouteStep",
    "Who",
    "describe_assignee",
    "describe_timeout",
    "router",
    "run_for_company",
    "run_for_escalation",
    "step_runs_for_run",
    "templates",
]
