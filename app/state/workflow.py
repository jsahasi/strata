"""Walking an escalation through the approval route, and what the clock may not do.

app/state/routing.py answers "whose is this". This file answers "what happens
next, and what happens when nobody does anything at all" -- the question nobody
in the product could answer before, because nothing anywhere expressed it. An
approval sat untouched for two days and the only honest answer was that it sat
there.

FOUR CLAIMS, AND THE CODE IS ARRANGED AROUND THEM.

A DRAFT MAY BE HALF-FINISHED; A LIVE ROUTE MAY NOT. save_graph checks types and
vocabulary and invents nothing -- no default of twenty-four hours, no default of
"remind" -- so a route an admin is halfway through drawing survives a save.
validation_errors is where absence becomes denial, and activate_workflow returns
the list rather than raising or filling the gaps in. A refused activation writes
NOTHING: not the status, not an audit row. A validation that half-applies is a
migration, and best-practices.html section 27 is about exactly that.

REMINDING IS NOT ACTING. A step whose on_timeout is remind stays pending, stays
on the same desk, and grows a counter. Nothing advances, because chasing
somebody is not the same as them answering. The tempting shortcut -- treat the
third reminder as consent -- is how an unapproved action gets filed as approved.

ESCALATION FAILING IS NOT PERMISSION TO SKIP. When escalate_to resolves to
nobody the step stays open, visibly, on the person who already had it, and the
failure is recorded once. IT NEVER FALLS THROUGH TO BYPASS. This is the branch a
tired implementation gets wrong, and getting it wrong turns a routing bug into
an unapproved action that reads as an approved one.

BYPASSED IS NOT APPROVED, AND THE PRODUCT HAS TO BE ABLE TO SAY SO. A run that
skipped a step can still reach the end, so approval_summary returns the LIST of
bypassed steps rather than a boolean. A caller cannot round a list off to yes
the way it can round `fully_approved` off, and the completion row in the chain
names the skipped steps in its own reason text as well. A bypass is the one
sanctioned fallback in this engine and it announces itself three times over: the
outcome value, the audit row, and the summary.

TIME IS A PARAMETER, NEVER A READING. Every function that decides anything takes
`now`. There is no scheduler in this product and pretending otherwise would be a
lie in the architecture: advance_overdue() is the whole clock, it is idempotent,
and something outside has to call it. In production that is a cron entry or a
worker loop calling it once a minute per company; in the demo it is a button and
the request that renders the desk. Whatever calls it, calling it twice with the
same `now` must do nothing the second time, and every branch below is written to
that rule.

WHAT THIS ENGINE DOES NOT DO, said here rather than discovered.

It walks ONE STEP AT A TIME. A step with two outgoing arrows is refused at
activation, because a fork needs a rule for joining back and this engine has
none. Parallel approval -- two people asked at once, either or both required --
is the obvious next feature and it is not built.

It does not send anything. A reminder is a row in the audit chain saying a
reminder was owed; no mail leaves the process. The row is honest about that and
the surfaces that render it must be too.

It cannot un-stick an unrouted step by itself. A step that opened with nobody on
it stays visible in unrouted_step_runs() until a person fixes the underlying
duty -- gives the obligation an owner, grants the role, reinstates the account.
The sweep deliberately does not re-resolve and quietly assign it later: the
person watching that queue is the fix, and a queue that empties itself without
anyone deciding is the "looks handled" failure this codebase refuses everywhere.

THE READS BELONG IN app/state/queries.py. Every scoped read here takes
company_id and pushes it through _require_scope, which is IMPORTED from that
module and never copied -- two spellings of one tenant guard drift, and the
point of a chokepoint is that there is only one. The functions themselves sit
here for now, beside the writes they serve, exactly as routing.py's do; moving
them is a cut and paste.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.state.audit import (
    ACTION_ACTION_APPROVED,
    ACTION_ACTION_REJECTED,
    ACTOR_SYSTEM,
    ACTOR_USER,
    record_event,
)
from app.state.identity import roles_for_company, users_for_company
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    ASSIGNEE_ROLE_PREFIX,
    ASSIGNEE_RULE_LITERALS,
    ASSIGNEE_RULE_PREFIXES,
    ASSIGNEE_UNASSIGNED,
    ASSIGNEE_USER_PREFIX,
    OUTCOME_APPROVED,
    OUTCOME_BYPASSED,
    OUTCOME_CANCELLED,
    OUTCOME_ESCALATED,
    OUTCOME_REJECTED,
    STEP_TIMEOUT_ACTIONS,
    TIMEOUT_BYPASS,
    TIMEOUT_ESCALATE,
    TIMEOUT_REMIND,
    WORKFLOW_ACTIVE,
    WORKFLOW_ARCHIVED,
    WORKFLOW_DRAFT,
    WORKFLOW_RUN_COMPLETED,
    WORKFLOW_RUN_REJECTED,
    WORKFLOW_RUN_RUNNING,
    ApprovalWorkflow,
    AuditEvent,
    Escalation,
    User,
    WorkflowEdge,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)

# Imported, never copied. Same import routing.py makes, for the same reason.
# row_for_company is the second of the two: a primary-key fetch that will not
# hand a row to anybody but its owner.
from app.state.queries import CrossTenantRow, _require_scope, row_for_company

# The two guards routing.py already spells, imported rather than restated. They
# are private names and are taken knowingly: a second copy of "an unattributed
# write is not auditable" is a second message every caller has to match, and a
# second copy of the naive-timestamp check is a second place for one of them to
# stop being enforced. If either is renamed this import fails loudly, which is
# the behaviour a copy would not give.
from app.state.routing import _require_actor, _require_aware, resolve_assignee

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE BELONG IN app/state/audit.py, BESIDE THE REST OF THE VOCABULARY, AND
# MUST BE MOVED THERE RATHER THAN RESTATED. They sit here for the reason
# app/state/routing.py gives about its own block: that file is owned by another
# agent in this build. This is a handoff, not a decision.
#
# THE FOUR workflow.* CODES ALREADY HAVE A SECOND HOME, and that is worse than
# a handoff. app/web/views/admin.py declares ACTION_WORKFLOW_CREATED, SAVED,
# ACTIVATED and ARCHIVED with the same four strings. The values here are
# byte-identical to that file's on purpose, so a query for "workflow.activated"
# returns rows whichever module wrote them -- but two copies of a string is the
# drift the ACTION_ constants were consolidated to stop, and the fix is for
# admin.py to import these once this module is the one the editor writes
# through. The state layer is the right home; the web view is not.
# ---------------------------------------------------------------------------

ACTION_WORKFLOW_CREATED = "workflow.created"
ACTION_WORKFLOW_SAVED = "workflow.saved"
ACTION_WORKFLOW_ACTIVATED = "workflow.activated"
ACTION_WORKFLOW_ARCHIVED = "workflow.archived"

# A run's life. started and refused are two halves of one decision, kept apart
# for the reason escalation.routed and escalation.unrouted are: "what did we
# fail to start a route for, and why" must be rows filtered, not prose parsed.
ACTION_RUN_STARTED = "workflow_run.started"
ACTION_RUN_REFUSED = "workflow_run.refused"
ACTION_RUN_COMPLETED = "workflow_run.completed"
ACTION_RUN_REJECTED = "workflow_run.rejected"

# What happened at one step. assigned and unrouted are the routing pair again,
# one level down: a step on a desk and a step nobody could be named for are
# different facts about whether the work is moving.
ACTION_STEP_ASSIGNED = "workflow_step.assigned"
ACTION_STEP_UNROUTED = "workflow_step.unrouted"
ACTION_STEP_REMINDED = "workflow_step.reminded"
ACTION_STEP_ESCALATED = "workflow_step.escalated"

# The refusal that must never be mistaken for a bypass. It is its own code so
# that "what did we try to escalate and could not" is one query, and so that
# nothing can read a failed escalation as the step having moved on.
ACTION_STEP_ESCALATION_FAILED = "workflow_step.escalation_failed"

ACTION_STEP_BYPASSED = "workflow_step.bypassed"

# The clock came due and the route said nothing usable -- no on_timeout, a
# reminder interval that is not a number, an arrow pointing at a step that is
# gone. Activation refuses every one of these, so a live route cannot reach this
# state through the product; a row written out of band can. The step stays
# pending and this says so rather than the engine picking a rule for it.
ACTION_STEP_STALLED = "workflow_step.stalled"


# ---------------------------------------------------------------------------
# Subject types. One spelling each, because a row filed under two names is two
# histories nobody joins.
# ---------------------------------------------------------------------------

SUBJECT_WORKFLOW = "workflow"
SUBJECT_RUN = "workflow_run"
SUBJECT_STEP_RUN = "workflow_step_run"


# ---------------------------------------------------------------------------
# Reason codes for starting a run. One code per fix, following routing.py: code
# for branching, text for reading.
# ---------------------------------------------------------------------------

RUN_OK = "RUN_OK"
# Not a refusal. The escalation is already walking the route and this call was a
# double click; the stored run comes back untouched.
RUN_ALREADY_STARTED = "RUN_ALREADY_STARTED"
RUN_NO_ESCALATION = "RUN_NO_ESCALATION"
# The company has drawn no live route, so there is nothing to walk an item
# through. This is the refusal the product must say out loud rather than
# silently doing nothing: an escalation that nobody is asked about looks the
# same from outside as one everybody approved.
RUN_NO_ACTIVE_WORKFLOW = "RUN_NO_ACTIVE_WORKFLOW"
# The live route has no step to enter at. Activation refuses that, so this is
# reachable only from a row written out of band.
RUN_ROUTE_EMPTY = "RUN_ROUTE_EMPTY"

RUN_REASON_CODES = (
    RUN_OK,
    RUN_ALREADY_STARTED,
    RUN_NO_ESCALATION,
    RUN_NO_ACTIVE_WORKFLOW,
    RUN_ROUTE_EMPTY,
)

# Who the clock is, when nobody named themselves. A system actor and never a
# person: a bypass attributed to the last admin who logged in would be a false
# attribution in the one table that exists to be believed.
CLOCK_ACTOR = "system:workflow-clock"

# The most reminder rows one tick will write for one step. A sweep that has not
# run for a month against a one-hour interval owes seven hundred reminders, and
# writing seven hundred rows into an append-only chain because of an argument
# somebody passed is a defect, not diligence. Past the cap the LAST few are
# written and the counter is moved to the full number, so the next tick repeats
# nothing -- and the row that is written says how many were owed. A fallback
# announces itself.
REMINDER_CATCH_UP_LIMIT = 24


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphError:
    """One refusal, and the step it belongs to. None means the graph itself.

    A fact about the whole graph -- no steps at all, nowhere to enter -- has no
    node to sit on, and inventing one would put the message on a step that is
    not at fault. Null is the honest answer and the editor renders those in a
    panel rather than on the canvas.

    The wire shape is exactly {"step_id", "message"} and app/web/views/admin.py
    declares the same dataclass. That is a second copy and it is on the same
    handoff list as the action codes above.
    """

    step_id: str | None
    message: str

    def as_dict(self) -> dict:
        return {"step_id": self.step_id, "message": self.message}


@dataclass(frozen=True, slots=True)
class GraphResult:
    """Refused, with every reason, or accepted with nothing to say.

    `ok` is derived from the errors rather than stored beside them, so the two
    cannot disagree -- a result that carries reasons can never also claim to
    have succeeded.

    A refusal carries EVERY reason, not the first. An admin fixing one message
    at a time and pressing the button between each is the workflow this list
    exists to avoid.
    """

    errors: tuple[GraphError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        if self.ok:
            return {"ok": True}
        return {"ok": False, "errors": [error.as_dict() for error in self.errors]}


@dataclass(frozen=True, slots=True)
class StartResult:
    """A run, or the reason there is not one. Never both, never neither.

    run is not None if and only if the route was entered. A caller that reads
    `.run` without reading `.reason_code` gets None and a crash rather than a
    quiet nothing, which is the failure mode to prefer here: an escalation that
    silently never started a route is invisible work.
    """

    run: WorkflowRun | None
    reason_code: str
    reason_text: str

    @property
    def started(self) -> bool:
        return self.run is not None


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one tick of the clock did. Lists, not counts, wherever a row moved.

    Ids rather than numbers because every one of these is a piece of work
    somebody has to look at. "Three steps were bypassed" is a statistic; three
    step-run ids can be opened.

    checked counts the RUNNING runs the tick looked at. A completed, rejected or
    cancelled run is not examined at all, which is what makes running the tick
    twice safe: there is nothing left in it to act on.
    """

    checked: int = 0
    reminders_sent: int = 0
    escalated_step_run_ids: tuple[int, ...] = ()
    bypassed_step_run_ids: tuple[int, ...] = ()
    # Open, overdue-or-not, and on nobody's desk. Not the clock's to fix.
    unrouted_step_run_ids: tuple[int, ...] = ()
    # Overdue, and the route could not act: an escalation that reached nobody,
    # or a rule the graph does not carry. Still pending, still assigned.
    stalled_step_run_ids: tuple[int, ...] = ()
    completed_run_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """Did every step of this run actually get approved, and which did not.

    THE LISTS ARE THE POINT AND fully_approved IS THE CONVENIENCE. A boolean can
    be rounded off by a caller in a hurry; a tuple naming STP-3 cannot be
    reported as an approval. Anything that files, publishes or cites a run has
    to read bypassed_steps, and the completion row in the audit chain names them
    too so that a reader who never calls this function still sees them.

    fully_approved is True only when the run reached the end AND every step on
    the record was approved by a named account. An open step, a rejected step, a
    bypassed step or a cancelled one all make it False. Absence is denial: a run
    still walking is not approved yet, it is simply not finished.
    """

    run_id: str
    status: str
    approved_steps: tuple[str, ...] = ()
    rejected_steps: tuple[str, ...] = ()
    bypassed_steps: tuple[str, ...] = ()
    escalated_steps: tuple[str, ...] = ()
    open_steps: tuple[str, ...] = ()

    @property
    def fully_approved(self) -> bool:
        return (
            self.status == WORKFLOW_RUN_COMPLETED
            and bool(self.approved_steps)
            and not self.rejected_steps
            and not self.bypassed_steps
            and not self.open_steps
        )


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def step_order(step_id: str) -> tuple:
    """STP-2 before STP-10. Lexical order would put ten before two.

    The canvas numbers its own nodes and a route with more than nine steps is
    ordinary, so sorting the id as text would reorder the list the moment a
    tenth node was added, with nothing on screen to say why.

    app/web/views/admin.py holds a copy of this function. Same handoff.
    """
    head, _, tail = (step_id or "").rpartition("-")
    if tail.isdigit():
        return (0, int(tail), head)
    return (1, 0, step_id or "")


def _whole_number(value) -> bool:
    """An integer, or nothing at all. A bool is not an integer here.

    True passes isinstance(value, int) in Python and would be stored as one
    hour. A JSON true in an hours field is a mistake upstream, not a deadline.

    Saving checks this; ACTIVATION checks the value is above zero. The split is
    the contract's: a draft may carry a wrong number and must not carry a word.
    """
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value) -> int | None:
    """An integer above zero, or None. What a live route needs and a draft does not."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _rule_is_well_formed(rule) -> bool:
    """Shape only: one of the two literals, or a prefix with something after it."""
    if not rule or not isinstance(rule, str):
        return False
    if rule in ASSIGNEE_RULE_LITERALS:
        return True
    return any(
        rule.startswith(prefix) and len(rule) > len(prefix)
        for prefix in ASSIGNEE_RULE_PREFIXES
    )


def _target_is_well_formed(target) -> bool:
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


def _display_name(session: Session, company_id: str, user_id: str | None) -> str:
    """A person's name for a reason line, or a sentence saying there is not one.

    Never the bare id silently. A reason that reads "passed to usr-mep-sarah"
    is a reason nobody can check at a glance, and one that reads "passed to
    somebody" is worse.
    """
    if not user_id:
        return "nobody"
    user = (
        session.query(User)
        .filter(User.company_id == company_id)
        .filter(User.id == user_id)
        .one_or_none()
    )
    if user is None:
        return f"account {user_id}, which this company no longer has"
    return user.display_name or user.email or user_id


def _already_recorded(
    session: Session, company_id: str, action: str, step_run_id: int
) -> bool:
    """Has this decision already been written for this step run?

    The idempotence guard for the two branches that change no column when they
    fire -- a failed escalation and a stall both leave the step exactly where it
    was, so there is nothing in workflow_step_runs to read back. The audit chain
    IS the record of what the engine decided, so it is what the engine asks.

    A column would be the alternative, and it would be a second copy of a fact
    the chain already holds -- one that a later writer could set without the
    chain agreeing. Reading the chain costs an indexed query per overdue step
    and only on the branches that stall.
    """
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .filter(AuditEvent.action == action)
        .filter(AuditEvent.subject_type == SUBJECT_STEP_RUN)
        .filter(AuditEvent.subject_id == str(step_run_id))
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------


def workflows_for_company(session: Session, company_id: str) -> list[ApprovalWorkflow]:
    """Every route this company has drawn, newest first, drafts included."""
    _require_scope(company_id)
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .order_by(ApprovalWorkflow.created_at.desc(), ApprovalWorkflow.id.desc())
        .all()
    )


def workflow_for_company(
    session: Session, company_id: str, workflow_id: str
) -> ApprovalWorkflow | None:
    """One route, or None. Another company's id resolves to None, not to a row."""
    _require_scope(company_id)
    if not workflow_id:
        return None
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .filter(ApprovalWorkflow.id == workflow_id)
        .one_or_none()
    )


def active_workflow(session: Session, company_id: str) -> ApprovalWorkflow | None:
    """The one live route, or None. The partial unique index makes "one" true."""
    _require_scope(company_id)
    return (
        session.query(ApprovalWorkflow)
        .filter(ApprovalWorkflow.company_id == company_id)
        .filter(ApprovalWorkflow.status == WORKFLOW_ACTIVE)
        .order_by(ApprovalWorkflow.id)
        .first()
    )


def _graph_rows(
    session: Session, workflow: ApprovalWorkflow
) -> tuple[list[WorkflowStep], list[WorkflowEdge]]:
    """The graph of one route. Takes the ROW, so tenancy is already settled.

    WorkflowStep and WorkflowEdge carry no company_id -- they are meaningless
    outside their workflow and tenancy comes through it, the same argument
    Passage takes from DocumentVersion. Taking the workflow row rather than an
    id is what makes that safe: the caller cannot reach this function without
    having resolved the route under a company scope first.
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
    return (
        sorted(steps, key=lambda row: step_order(row.id)),
        sorted(
            edges,
            key=lambda row: (
                step_order(row.from_step_id),
                step_order(row.to_step_id),
            ),
        ),
    )


def graph_dict(session: Session, company_id: str, workflow_id: str) -> dict | None:
    """The wire contract's JSON for one route, or None. No translation table.

    The columns ARE the contract, field for field, so this is a copy and not a
    mapping -- except for the edge keys. `from` is a Python keyword, which is
    why the columns are spelt the long way; turning them back into "from" and
    "to" is this function's job and nobody else's.
    """
    workflow = workflow_for_company(session, company_id, workflow_id)
    if workflow is None:
        return None
    steps, edges = _graph_rows(session, workflow)
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
        "edges": [{"from": edge.from_step_id, "to": edge.to_step_id} for edge in edges],
    }


def run_for_escalation(
    session: Session, company_id: str, escalation_id: str
) -> WorkflowRun | None:
    """The run walking this escalation, or None. One escalation, one run."""
    _require_scope(company_id)
    if not escalation_id:
        return None
    return (
        session.query(WorkflowRun)
        .filter(WorkflowRun.company_id == company_id)
        .filter(WorkflowRun.escalation_id == escalation_id)
        .order_by(WorkflowRun.started_at, WorkflowRun.id)
        .first()
    )


def run_for_company(
    session: Session, company_id: str, run_id: str
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
    session: Session, company_id: str, run_id: str
) -> list[WorkflowStepRun]:
    """Every attempt at every step of one run, in the order they happened.

    Ordered by the autoincrement id, which is the order the engine opened them.
    A step that escalated appears twice, and the first row -- who was asked and
    did not answer -- is the one an auditor came for.
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


def open_step_run(
    session: Session, company_id: str, run_id: str
) -> WorkflowStepRun | None:
    """The one step of this run that is still waiting on somebody, or None.

    At most one is open at a time because this engine walks one step at a time.
    None means the run is finished, rejected or cancelled -- or, on a run still
    marked running, that something wrote rows out of band.
    """
    _require_scope(company_id)
    if not run_id:
        return None
    return (
        session.query(WorkflowStepRun)
        .filter(WorkflowStepRun.company_id == company_id)
        .filter(WorkflowStepRun.run_id == run_id)
        .filter(WorkflowStepRun.outcome.is_(None))
        .order_by(WorkflowStepRun.id)
        .first()
    )


def desk_for_user(
    session: Session, company_id: str, user_id: str
) -> list[WorkflowStepRun]:
    """What is waiting on one person, soonest deadline first.

    The most frequent read in the product and the reason the step-run row
    carries its own company_id rather than joining for it. A step with no
    deadline sorts last: an item with no clock is not more urgent than one with
    a deadline this afternoon, and putting NULLs first would say it was.
    """
    _require_scope(company_id)
    if not user_id:
        return []
    rows = (
        session.query(WorkflowStepRun)
        .filter(WorkflowStepRun.company_id == company_id)
        .filter(WorkflowStepRun.assigned_to_user_id == user_id)
        .filter(WorkflowStepRun.outcome.is_(None))
        .all()
    )
    return sorted(
        rows, key=lambda row: (row.due_at is None, row.due_at or row.id, row.id)
    )


def unrouted_step_runs(session: Session, company_id: str) -> list[WorkflowStepRun]:
    """Open steps with nobody on them. The queue that must never empty itself.

    A step lands here when its assignee_rule reached no account at the moment
    the step opened -- no obligation owner, a role nobody active holds, a
    suspended account. It has NO clock, so it can never time out and can never
    be bypassed; it waits for a person to fix the duty behind it.

    The reason it is here is in the chain, under workflow_step.unrouted, and it
    is the reason AT THE TIME. Re-deriving it live -- the way routing.py's
    shared_queue does -- would be better and needs the resolution to be
    re-runnable against the run's escalation; that is a small function and it is
    not written. Said here rather than left to be discovered by somebody
    wondering why a cleared obligation did not change this list.
    """
    _require_scope(company_id)
    return (
        session.query(WorkflowStepRun)
        .filter(WorkflowStepRun.company_id == company_id)
        .filter(WorkflowStepRun.outcome.is_(None))
        .filter(WorkflowStepRun.assigned_to_user_id.is_(None))
        .order_by(WorkflowStepRun.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Drawing a route
# ---------------------------------------------------------------------------


def create_workflow(
    session: Session,
    company_id: str,
    *,
    workflow_id: str,
    name: str,
    actor: str,
    created_by_user_id: str | None = None,
    supersedes_id: str | None = None,
    now: datetime | None = None,
) -> ApprovalWorkflow:
    """Start a route as a draft. The status is stated, never defaulted.

    ApprovalWorkflow.status has no default in the schema on purpose: a route's
    state is a decision the caller makes, and a column that quietly said "draft"
    would let a writer that never thought about it look like one that had.

    Idempotent on the id, like ensure_obligation: creating the same route twice
    is a double click, it returns the stored row and it appends nothing. An id
    another company holds raises rather than resolving, because silently
    handing back that row is a tenant leak with a friendly face.

    That last check used to be written out here -- session.get, then compare
    company_id -- and ensure_obligation, which does the identical lookup, had
    been written without it. Three sites remembering the same rule separately is
    a rule that fails on the fourth, so the fetch and the check are one call now
    and this site reads the same as the other two.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")
    if not workflow_id:
        raise ValueError("a route needs an id; the canvas supplies it")
    if not name or not name.strip():
        raise ValueError("a route needs a name somebody can search for")

    # Re-raised in this module's own words, for the reason ensure_obligation
    # gives: the caller needs a sentence about a route, and the message must
    # read exactly as a missing row does rather than confirm that some other
    # tenant holds that id.
    try:
        stored = row_for_company(session, company_id, ApprovalWorkflow, workflow_id)
    except CrossTenantRow as crossed:
        raise ValueError(f"no workflow {workflow_id!r} for this company") from crossed
    if stored is not None:
        return stored

    workflow = ApprovalWorkflow(
        id=workflow_id,
        company_id=company_id,
        name=name.strip(),
        status=WORKFLOW_DRAFT,
        created_by_user_id=created_by_user_id or None,
        created_at=stamp,
        activated_at=None,
        supersedes_id=supersedes_id or None,
    )
    session.add(workflow)
    session.flush()
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_WORKFLOW_CREATED,
        subject_type=SUBJECT_WORKFLOW,
        subject_id=workflow_id,
        reason=(
            f"draft route {name.strip()!r} created"
            + (f", replacing {supersedes_id}" if supersedes_id else "")
        ),
        occurred_at=stamp,
        actor_user_id=created_by_user_id or None,
        actor_kind=ACTOR_USER if created_by_user_id else ACTOR_SYSTEM,
    )
    return workflow


def _read_graph(payload, workflow: ApprovalWorkflow) -> tuple[dict, list[GraphError]]:
    """Check a posted graph, or say what is wrong with it. Writes nothing.

    TYPES AND VOCABULARY ONLY. Whether the route makes sense is activation's
    question, and answering it here would stop an admin saving work in
    progress. A step may be saved with no deadline, no timeout rule and nobody
    to ask; it may not be saved with the word "soon" where a number goes,
    because that is not a half-finished decision, it is a value the column
    cannot hold.

    Returns the cleaned graph and every refusal. Both, rather than raising on
    the first: an admin who mistyped two fields should be told about both.
    """
    errors: list[GraphError] = []
    if not isinstance(payload, dict):
        return {}, [GraphError(None, "The body must be a graph object.")]

    # ABSENCE IS DENIAL, APPLIED TO A WRITE. A body with no `steps` key is not a
    # route with no steps: it is a save that did not say. Treating the two alike
    # would let an empty post -- a client bug, a retry that lost its body --
    # delete every step of a route and report success.
    for required in ("steps", "edges"):
        if required not in payload:
            errors.append(
                GraphError(
                    None,
                    f"This save does not say what the {required} are. A body with "
                    f"no {required} is not the same as a route with no {required}, "
                    "so nothing was written.",
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
        # A route's state moves through activation, which validates. A save that
        # could set it would be a way round every check there is.
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
            continue

        rule = raw.get("assignee_rule", ASSIGNEE_UNASSIGNED)
        if rule is None:
            # One spelling of absence. The contract has a word for nobody-yet
            # and NULL would be a second one, splitting every query over this
            # column without anyone noticing which half they got.
            rule = ASSIGNEE_UNASSIGNED
        if not _rule_is_well_formed(rule):
            errors.append(
                GraphError(
                    step_id,
                    f"{rule!r} is not an assignee rule. Use obligation_owner, "
                    "unassigned, role:<name> or user:<id>.",
                )
            )
            continue

        hours = raw.get("approval_hours")
        if not _whole_number(hours):
            errors.append(
                GraphError(
                    step_id,
                    "approval_hours is a whole number of hours, or blank while "
                    "you decide. Whether it makes sense is checked when you "
                    "activate.",
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
        if escalate_to is not None and not _target_is_well_formed(escalate_to):
            errors.append(
                GraphError(
                    step_id,
                    f"{escalate_to!r} is not an escalation target. Use "
                    "role:<name> or user:<id>.",
                )
            )
            continue

        remind = raw.get("remind_every_hours")
        if not _whole_number(remind):
            errors.append(
                GraphError(
                    step_id,
                    "remind_every_hours is a whole number of hours, or blank.",
                )
            )
            continue

        x, y = raw.get("x", 0), raw.get("y", 0)
        if not _whole_number(x) or not _whole_number(y) or x is None or y is None:
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
        # Refused rather than stored, and the message is filed against the step
        # THAT IS MISSING rather than the one that is present. "STP-9 is not in
        # this route" is a message an admin can act on; the same message filed
        # against STP-2 sends them to look at the wrong node.
        missing = [name for name in (tail, head) if name not in seen]
        if missing:
            for name in missing:
                errors.append(
                    GraphError(
                        name,
                        f"An arrow points at {name}, which is not a step in this "
                        "route.",
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


def save_graph(
    session: Session,
    company_id: str,
    workflow_id: str,
    payload: dict,
    *,
    actor: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> GraphResult:
    """Replace a draft's steps and edges with the posted ones, or refuse.

    WHOLE, NOT MERGED. The canvas holds the truth about the graph -- a node the
    admin deleted is gone from the post and has to be gone from the table -- and
    working out which rows to update, insert and delete would be a second model
    of the same graph living in this function. Edges go first so nothing is left
    pointing at a step about to be deleted.

    A ROUTE THAT IS NOT A DRAFT REFUSES. Activation freezes the graph, so a run
    in flight and a run finished last month both read the steps they actually
    ran under. Changing a live route means copying it into a new draft,
    activating that and letting the old one archive, and the message says so
    rather than leaving an admin to guess.

    A workflow this company does not have RAISES rather than returning a
    refusal. A missing route and a bad graph are different faults: one is a bug
    or a probe, the other is an admin's typo, and a caller that renders the
    second in a panel must not render the first there.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")

    workflow = workflow_for_company(session, company_id, workflow_id)
    if workflow is None:
        raise ValueError(f"no workflow {workflow_id!r} for this company")

    if workflow.status != WORKFLOW_DRAFT:
        return GraphResult(
            (
                GraphError(
                    None,
                    f"This route is {workflow.status} and its graph is frozen, so "
                    "nothing was written. A run in flight reads these steps. To "
                    "change it, copy it into a new draft, activate that, and this "
                    "one archives itself.",
                ),
            )
        )

    graph, errors = _read_graph(payload, workflow)
    if errors:
        return GraphResult(tuple(errors))

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

    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_WORKFLOW_SAVED,
        subject_type=SUBJECT_WORKFLOW,
        subject_id=workflow.id,
        reason=(
            f"draft saved with {len(graph['steps'])} step(s) and "
            f"{len(graph['edges'])} arrow(s)"
        ),
        occurred_at=stamp,
        actor_user_id=actor_user_id or None,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    return GraphResult()


# ---------------------------------------------------------------------------
# Validation: where absence becomes denial
# ---------------------------------------------------------------------------


def _reaches_itself(node: str, outgoing: dict[str, list[str]]) -> bool:
    """Can an item leaving this step come back to it? Then the step is on a loop.

    Asked per node rather than by finding the components once, because the
    MESSAGE has to name every step on the loop and a back-edge detector names
    only the one the edge closes on. An admin told "STP-3 loops back to STP-2"
    fixes STP-3 and reruns; an admin told that both are on a loop can see the
    shape. Routes have tens of nodes, so the cost of asking n times does not
    matter and being able to name them all does.
    """
    seen: set[str] = set()
    frontier = list(outgoing.get(node, ()))
    while frontier:
        nxt = frontier.pop()
        if nxt == node:
            return True
        if nxt in seen:
            continue
        seen.add(nxt)
        frontier.extend(outgoing.get(nxt, ()))
    return False


def validation_errors(
    session: Session, company_id: str, workflow_id: str
) -> list[GraphError]:
    """Every reason this route cannot go live, each named against its step.

    Returned, never raised, and never half-applied. A draft is allowed to be
    wrong; this is the moment the product stops allowing it, and the admin needs
    the whole list rather than one message per attempt.

    PER STEP: it has a label, it names somebody, that somebody exists in this
    company, it says how long it has, it says what happens when that runs out,
    and the answer it gives is complete -- a reminder needs an interval, an
    escalation needs a target that resolves to somebody here.

    PER GRAPH: at least one step, exactly one place to enter, no loop, no fork,
    every step reachable, and no arrow pointing at a step that is not here. A
    second entry point is a route with no answer to "where does an item start";
    a loop is a route an item never leaves; an unreachable step is work that
    silently never happens.

    WHAT IS CHECKED IS EXISTENCE, NOT AVAILABILITY. role:analyst passes if the
    company has that role, even if nobody holds it today. Whether a person can
    be named is a question about the moment an item arrives -- somebody may be
    granted the role tomorrow, or suspended tonight -- and answering it here
    would freeze this morning's staffing into the route. routing.resolve_assignee
    asks it again at every step, and refuses there.
    """
    _require_scope(company_id)
    workflow = workflow_for_company(session, company_id, workflow_id)
    if workflow is None:
        raise ValueError(f"no workflow {workflow_id!r} for this company")

    steps, edges = _graph_rows(session, workflow)
    errors: list[GraphError] = []

    if not steps:
        return [
            GraphError(
                None,
                "This route has no steps, so there is nothing to walk an item "
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
                    "This step's assignee_rule is unassigned, so an item reaching "
                    "it would stop there with nobody asked. Say who is asked.",
                )
            )
        elif not _rule_is_well_formed(rule):
            errors.append(
                GraphError(
                    step.id,
                    f"{rule!r} is not an assignee rule this product can read.",
                )
            )
        elif not _names_somebody_here(session, company_id, rule):
            errors.append(
                GraphError(
                    step.id,
                    f"The assignee_rule {rule} names nobody in this company. A "
                    "step assigned to somebody who does not exist routes into "
                    "silence.",
                )
            )

        if _positive_int(step.approval_hours) is None:
            errors.append(
                GraphError(
                    step.id,
                    "approval_hours must be a whole number of hours above zero. "
                    "Without a deadline nothing can time out and nothing can be "
                    "chased.",
                )
            )

        if step.on_timeout is None:
            errors.append(
                GraphError(
                    step.id,
                    "This step does not say what happens when its time runs out. "
                    "Set on_timeout to remind, escalate or bypass -- and read what "
                    "bypass does before choosing it.",
                )
            )
        elif step.on_timeout not in STEP_TIMEOUT_ACTIONS:
            errors.append(
                GraphError(
                    step.id,
                    f"on_timeout is {step.on_timeout!r}, which is not one of "
                    f"{', '.join(STEP_TIMEOUT_ACTIONS)}.",
                )
            )
        elif step.on_timeout == TIMEOUT_REMIND:
            if _positive_int(step.remind_every_hours) is None:
                errors.append(
                    GraphError(
                        step.id,
                        "This step reminds when it times out, so remind_every_hours "
                        "must be a whole number of hours above zero. A reminder "
                        "with no interval is never sent.",
                    )
                )
        elif step.on_timeout == TIMEOUT_ESCALATE:
            if not _target_is_well_formed(step.escalate_to):
                errors.append(
                    GraphError(
                        step.id,
                        "This step escalates when it times out, so escalate_to "
                        "must name role:<name> or user:<id>. An escalation with no "
                        "target cannot fall through to a bypass, so the step would "
                        "simply stop.",
                    )
                )
            elif not _names_somebody_here(session, company_id, step.escalate_to):
                errors.append(
                    GraphError(
                        step.id,
                        f"escalate_to is {step.escalate_to}, which names nobody in "
                        "this company, so an escalation from this step would reach "
                        "no one.",
                    )
                )

    known = {step.id for step in steps}
    # Re-checked rather than trusted. SQLite does not enforce foreign keys
    # unless asked, so a row written by a script can carry an arrow to a step
    # that is gone.
    live_edges = []
    for edge in edges:
        missing = [
            name
            for name in (edge.from_step_id, edge.to_step_id)
            if name not in known
        ]
        if missing:
            for name in missing:
                errors.append(
                    GraphError(
                        name,
                        f"An arrow runs from {edge.from_step_id} to "
                        f"{edge.to_step_id}, and {name} is not a step in this "
                        "route.",
                    )
                )
            continue
        live_edges.append(edge)

    incoming: dict[str, int] = {step.id: 0 for step in steps}
    outgoing: dict[str, list[str]] = {step.id: [] for step in steps}
    for edge in live_edges:
        incoming[edge.to_step_id] += 1
        outgoing[edge.from_step_id].append(edge.to_step_id)

    for step_id in sorted(known, key=step_order):
        branches = outgoing.get(step_id, [])
        if len(branches) > 1:
            errors.append(
                GraphError(
                    step_id,
                    f"This step has {len(branches)} arrows leaving it "
                    f"({', '.join(sorted(branches, key=step_order))}). This engine "
                    "walks an item one step at a time, so a step may have at most "
                    "one outgoing arrow. Two branches need a rule for joining back "
                    "together and there is not one.",
                )
            )

    roots = sorted(
        [step_id for step_id, count in incoming.items() if count == 0], key=step_order
    )
    if not roots:
        errors.append(
            GraphError(
                None,
                "Every step in this route has an arrow pointing at it, so there is "
                "nowhere for an item to start. Somewhere in here is a loop with no "
                "way in.",
            )
        )
    elif len(roots) > 1:
        named = ", ".join(roots)
        for step_id in roots:
            errors.append(
                GraphError(
                    step_id,
                    "Nothing points at this step, so it is somewhere an item could "
                    f"start -- and this route has {len(roots)} of those ({named}). "
                    "Nothing here can say which one an item uses. Connect them, or "
                    "split this into two routes.",
                )
            )

    for step_id in sorted(known, key=step_order):
        if _reaches_itself(step_id, outgoing):
            errors.append(
                GraphError(
                    step_id,
                    "This step sits on a loop: an item that reaches it can come "
                    "back to it. An item going round a loop never leaves the "
                    "route.",
                )
            )

    if roots:
        # Walked from the FIRST entry point in canvas order. With more than one
        # the route is refused anyway, and the alternative -- treating every
        # entry point as a start -- would report a stranded second entry point as
        # perfectly reachable, hiding the second half of the fault behind the
        # first.
        entry = roots[0]
        reached: set[str] = set()
        frontier = [entry]
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
                    f"Nothing reaches this step from {entry}, so an item walking "
                    "the route would never arrive at it.",
                )
            )

    return errors


def _names_somebody_here(session: Session, company_id: str, rule: str) -> bool:
    """Does this rule name a role or an account this company has?

    obligation_owner is always true: it resolves against the escalation and not
    against the route, so there is nothing here to check it against. That is not
    a hole -- routing.resolve_assignee refuses it per item, loudly, and the step
    opens unrouted rather than on somebody at random.
    """
    if rule == ASSIGNEE_OBLIGATION_OWNER:
        return True
    if rule.startswith(ASSIGNEE_ROLE_PREFIX):
        wanted = rule[len(ASSIGNEE_ROLE_PREFIX) :]
        return any(
            role.name == wanted for role in roles_for_company(session, company_id)
        )
    if rule.startswith(ASSIGNEE_USER_PREFIX):
        wanted = rule[len(ASSIGNEE_USER_PREFIX) :]
        return any(user.id == wanted for user in users_for_company(session, company_id))
    return False


def activate_workflow(
    session: Session,
    company_id: str,
    workflow_id: str,
    *,
    actor: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> GraphResult:
    """Put a route live, or hand back every reason it cannot go.

    A REFUSED ACTIVATION CHANGES NOTHING AT ALL. Not the status, not
    activated_at, not one row in the chain. Validation runs before any write and
    returns early, so there is no path where half a route goes live -- which is
    the difference between a validation and a migration.

    Activating the route that is already live is a no-op and writes nothing.
    Restating a fact is not a decision, and a chain full of them is a chain
    nobody reads; set_obligation_owner and set_user_status take the same line.

    AN ARCHIVED ROUTE CANNOT COME BACK. Its successor exists and runs may have
    walked it; reviving it would give the company two versions of one route with
    nothing saying which governs. Copy it into a new draft instead.

    The previous live route is archived FIRST and flushed before this one is set
    active, because the database holds a partial unique index over
    (company_id) where status is active. Writing both in one flush would leave
    the order to SQLAlchemy, and half the time that order is the one the index
    refuses.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")

    workflow = workflow_for_company(session, company_id, workflow_id)
    if workflow is None:
        raise ValueError(f"no workflow {workflow_id!r} for this company")

    if workflow.status == WORKFLOW_ACTIVE:
        return GraphResult()

    if workflow.status == WORKFLOW_ARCHIVED:
        return GraphResult(
            (
                GraphError(
                    None,
                    "This route is archived. An archived route cannot be brought "
                    "back, because runs walked the version that replaced it. Copy "
                    "it into a new draft and activate that.",
                ),
            )
        )

    errors = validation_errors(session, company_id, workflow_id)
    if errors:
        return GraphResult(tuple(errors))

    kind = ACTOR_USER if actor_user_id else ACTOR_SYSTEM
    live = active_workflow(session, company_id)
    if live is not None and live.id != workflow.id:
        live.status = WORKFLOW_ARCHIVED
        session.flush()
        record_event(
            session,
            company_id=company_id,
            actor=who,
            action=ACTION_WORKFLOW_ARCHIVED,
            subject_type=SUBJECT_WORKFLOW,
            subject_id=live.id,
            reason=(
                f"replaced by {workflow.id}. Runs already walking {live.id} keep "
                "reading its steps; nothing about them changes."
            ),
            occurred_at=stamp,
            actor_user_id=actor_user_id or None,
            actor_kind=kind,
        )

    workflow.status = WORKFLOW_ACTIVE
    workflow.activated_at = stamp
    session.flush()
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_WORKFLOW_ACTIVATED,
        subject_type=SUBJECT_WORKFLOW,
        subject_id=workflow.id,
        reason=(
            f"{workflow.name!r} is now the live route. Its graph is frozen from "
            "here; a change means a new draft."
        ),
        occurred_at=stamp,
        actor_user_id=actor_user_id or None,
        actor_kind=kind,
    )
    return GraphResult()


# ---------------------------------------------------------------------------
# Walking an item through
# ---------------------------------------------------------------------------


def _steps_by_id(session: Session, workflow_id: str) -> dict[str, WorkflowStep]:
    rows = (
        session.query(WorkflowStep)
        .filter(WorkflowStep.workflow_id == workflow_id)
        .all()
    )
    return {row.id: row for row in rows}


def _entry_step_id(session: Session, workflow_id: str) -> str | None:
    """The step an item enters at: the one with no arrow pointing at it."""
    steps = (
        session.query(WorkflowStep)
        .filter(WorkflowStep.workflow_id == workflow_id)
        .all()
    )
    if not steps:
        return None
    edges = (
        session.query(WorkflowEdge)
        .filter(WorkflowEdge.workflow_id == workflow_id)
        .all()
    )
    pointed_at = {edge.to_step_id for edge in edges}
    roots = sorted(
        [step.id for step in steps if step.id not in pointed_at], key=step_order
    )
    return roots[0] if roots else None


def _next_step_ids(session: Session, workflow_id: str, step_id: str) -> list[str]:
    edges = (
        session.query(WorkflowEdge)
        .filter(WorkflowEdge.workflow_id == workflow_id)
        .filter(WorkflowEdge.from_step_id == step_id)
        .all()
    )
    return sorted([edge.to_step_id for edge in edges], key=step_order)


def _open_step(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    step: WorkflowStep,
    *,
    actor: str,
    moment: datetime,
    note: str = "",
) -> WorkflowStepRun:
    """Put one step of one run on somebody's desk, or open it with nobody on it.

    ABSENCE IS DENIAL, AND THIS IS WHERE IT BITES IN THIS FILE. When the rule
    reaches no account the row is still written -- visible, open, on the record
    -- with assigned_to_user_id NULL, NO DEADLINE, and the reason in the chain.
    It is never given to the admin, to whoever raised it, or to the last person
    who touched anything nearby. Each of those is one line of code and each is
    worse than the refusal, because a wrong assignment looks handled.

    NO DEADLINE ON AN UNROUTED STEP, and that is deliberate rather than an
    oversight. A clock on a step nobody holds would run down and, if the step
    said bypass, would skip an approval nobody was ever asked for. The step
    waits instead, in unrouted_step_runs(), for a person to fix the duty.
    """
    resolution = resolve_assignee(
        session,
        company_id,
        rule=step.assignee_rule or ASSIGNEE_UNASSIGNED,
        escalation_id=run.escalation_id,
        now=moment,
    )
    routed = resolution.routed
    hours = _positive_int(step.approval_hours)
    row = WorkflowStepRun(
        company_id=company_id,
        run_id=run.id,
        step_id=step.id,
        assigned_to_user_id=resolution.user_id if routed else None,
        assigned_at=moment if routed else None,
        due_at=(moment + timedelta(hours=hours)) if (routed and hours) else None,
        outcome=None,
        acted_at=None,
        acted_by_user_id=None,
        reminder_count=0,
    )
    session.add(row)
    session.flush()

    tail = f" {note}" if note else ""
    if routed:
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_STEP_ASSIGNED,
            subject_type=SUBJECT_STEP_RUN,
            subject_id=str(row.id),
            reason=(
                f"{step.id} of run {run.id} is with "
                f"{_display_name(session, company_id, resolution.user_id)} until "
                f"{row.due_at.isoformat() if row.due_at else 'no deadline'} "
                f"({resolution.reason_code}: {resolution.reason_text}){tail}"
            ),
            occurred_at=moment,
        )
    else:
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_STEP_UNROUTED,
            subject_type=SUBJECT_STEP_RUN,
            subject_id=str(row.id),
            reason=(
                f"{step.id} of run {run.id} is open with nobody on it and no "
                f"deadline: {resolution.reason_code}: {resolution.reason_text} "
                "It stays visible until somebody fixes that; it will not time out "
                f"and it will not be bypassed.{tail}"
            ),
            occurred_at=moment,
        )
    return row


def _complete_run(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    *,
    actor: str,
    moment: datetime,
) -> None:
    """Mark the run finished, and say in the chain what "finished" means here.

    The reason text NAMES EVERY BYPASSED STEP. A reader of the log who never
    calls approval_summary must not be able to come away thinking a completed
    run was an approved one -- that reading is the whole failure this engine is
    written against.
    """
    run.status = WORKFLOW_RUN_COMPLETED
    run.current_step_id = None
    session.flush()

    summary = approval_summary(session, company_id, run.id)
    if summary.bypassed_steps:
        reason = (
            f"run {run.id} reached the end of the route with "
            f"{len(summary.bypassed_steps)} step(s) NOBODY APPROVED: "
            f"{', '.join(summary.bypassed_steps)} bypassed when the clock ran "
            "out. Completed is not approved and this run is not fully approved."
        )
    elif summary.rejected_steps:
        reason = f"run {run.id} reached the end after a rejection at " + ", ".join(
            summary.rejected_steps
        )
    else:
        reason = (
            f"run {run.id} reached the end and every step was approved by a named "
            f"account: {', '.join(summary.approved_steps)}"
        )
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_RUN_COMPLETED,
        subject_type=SUBJECT_RUN,
        subject_id=run.id,
        reason=reason,
        occurred_at=moment,
    )


def _advance(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    *,
    actor: str,
    moment: datetime,
) -> None:
    """Move the run off the step it is standing on, or finish it.

    One outgoing arrow or none, because activation refuses a fork. A route
    carrying two is only reachable through a write that went round this module,
    and rather than silently picking a branch the engine takes the first in
    canvas order and SAYS SO in the assignment row it writes. A fallback
    announces itself.

    A newly opened step is never overdue at the moment it opens -- its deadline
    is this moment plus its hours -- so one tick can move a run past at most one
    step. There is no cascade to guard against, and that is a property of the
    arithmetic rather than a rule enforced somewhere.
    """
    targets = _next_step_ids(session, run.workflow_id, run.current_step_id or "")
    if not targets:
        _complete_run(session, company_id, run, actor=actor, moment=moment)
        return

    note = ""
    if len(targets) > 1:
        note = (
            f"[{run.current_step_id} has {len(targets)} outgoing arrows, which "
            f"activation refuses; the engine took {targets[0]}, first in canvas "
            "order, and the other branches were not walked]"
        )

    steps = _steps_by_id(session, run.workflow_id)
    step = steps.get(targets[0])
    if step is None:
        # The arrow points at a step that is not in the route. Activation checks
        # this and SQLite does not enforce the composite key, so it is reachable
        # only out of band. The run is NOT completed: finishing a route early
        # would file the remaining approvals as if they had been asked for.
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_STEP_STALLED,
            subject_type=SUBJECT_RUN,
            subject_id=run.id,
            reason=(
                f"{run.current_step_id} points at {targets[0]}, which is not a step "
                "in this route. The run stops here rather than finishing early, "
                "because an early finish would look like approval."
            ),
            occurred_at=moment,
        )
        return

    run.current_step_id = step.id
    session.flush()
    _open_step(session, company_id, run, step, actor=actor, moment=moment, note=note)


def start_run(
    session: Session,
    company_id: str,
    *,
    escalation_id: str,
    actor: str,
    actor_user_id: str | None = None,
    now: datetime | None = None,
) -> StartResult:
    """Put one escalation onto the live route, or record why it did not go.

    EVERY REFUSAL IS AUDITED. An escalation that never entered a route looks,
    from outside, exactly like one everybody approved: nothing is waiting on
    anybody and nothing says why. The row under workflow_run.refused is what
    makes the difference visible.

    Starting a run twice for one escalation returns the first and writes
    nothing. The run id is derived from the escalation id, so the primary key
    enforces what this function intends rather than leaving it to the check
    above -- the same argument the partial unique index makes for one live route
    per company. THE LIMIT THAT CARRIES: an escalation cannot be re-run under a
    later route without changing the id scheme. Re-running is not a feature this
    build has, and when it is, this is the line that has to move.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")
    kind = ACTOR_USER if actor_user_id else ACTOR_SYSTEM

    def refuse(code: str, text: str) -> StartResult:
        record_event(
            session,
            company_id=company_id,
            actor=who,
            action=ACTION_RUN_REFUSED,
            subject_type="escalation",
            subject_id=escalation_id or "",
            reason=f"{code}: {text}",
            occurred_at=stamp,
            actor_user_id=actor_user_id or None,
            actor_kind=kind,
        )
        return StartResult(run=None, reason_code=code, reason_text=text)

    stored = run_for_escalation(session, company_id, escalation_id)
    if stored is not None:
        return StartResult(
            run=stored,
            reason_code=RUN_ALREADY_STARTED,
            reason_text=(
                f"escalation {escalation_id} is already walking route "
                f"{stored.workflow_id} as run {stored.id}"
            ),
        )

    escalation = (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one_or_none()
    )
    if escalation is None:
        return refuse(
            RUN_NO_ESCALATION, f"no escalation {escalation_id!r} for this company"
        )

    workflow = active_workflow(session, company_id)
    if workflow is None:
        return refuse(
            RUN_NO_ACTIVE_WORKFLOW,
            "this company has no live approval route, so there is nothing to walk "
            f"escalation {escalation_id} through. It stays where it is, waiting "
            "for a person, and nothing was approved. Draw a route and activate "
            "it.",
        )

    entry = _entry_step_id(session, workflow.id)
    if entry is None:
        return refuse(
            RUN_ROUTE_EMPTY,
            f"the live route {workflow.id} has no step for an item to enter at. "
            "Activation refuses that, so these rows were written some other way.",
        )

    steps = _steps_by_id(session, workflow.id)
    run = WorkflowRun(
        id=f"WFR-{escalation_id}",
        company_id=company_id,
        workflow_id=workflow.id,
        escalation_id=escalation_id,
        current_step_id=entry,
        started_at=stamp,
        status=WORKFLOW_RUN_RUNNING,
    )
    session.add(run)
    session.flush()
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_RUN_STARTED,
        subject_type=SUBJECT_RUN,
        subject_id=run.id,
        reason=(
            f"escalation {escalation_id} entered route {workflow.id} at {entry}. "
            "The route is frozen, so this run reads these steps whatever an admin "
            "draws later."
        ),
        occurred_at=stamp,
        actor_user_id=actor_user_id or None,
        actor_kind=kind,
    )
    _open_step(session, company_id, run, steps[entry], actor=who, moment=stamp)
    return StartResult(
        run=run,
        reason_code=RUN_OK,
        reason_text=f"run {run.id} started at {entry}",
    )


def record_decision(
    session: Session,
    company_id: str,
    *,
    run_id: str,
    outcome: str,
    actor: str,
    acting_user_id: str,
    now: datetime | None = None,
) -> WorkflowStepRun:
    """One person answers the step that is on their desk. Approved, or rejected.

    THE VOCABULARY HERE IS TWO WORDS, NOT FIVE. bypassed, escalated and
    cancelled are the clock's outcomes and no person may write them through this
    path: a bypass recorded as a decision would name an actor, and the clock is
    not a person. Anything else raises.

    ONLY THE PERSON THE STEP WAS GIVEN TO MAY ANSWER IT. Not their manager, not
    an admin, not somebody who happens to hold the same role. An approval is
    only evidence if it names the account that was asked, and a segregation of
    duties nobody can check afterwards is not one. Delegation is a real need and
    it is a reassignment -- a new step run with a new assignee, audited -- not a
    second person answering the first person's row. That path is not built.

    A step nobody holds cannot be answered here either, and that follows from
    the same rule: there is no assignee to match. Clearing an unrouted step
    means fixing the duty behind it so it can be assigned.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    stamp = _require_aware(now or _utcnow(), "now")

    if outcome not in (OUTCOME_APPROVED, OUTCOME_REJECTED):
        raise ValueError(
            f"a person may answer {OUTCOME_APPROVED} or {OUTCOME_REJECTED}; got "
            f"{outcome!r}. {OUTCOME_BYPASSED}, {OUTCOME_ESCALATED} and "
            f"{OUTCOME_CANCELLED} are what the clock and the operator write, and "
            "recording one of them as a decision would name a person for something "
            "nobody did."
        )

    run = run_for_company(session, company_id, run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} for this company")
    if run.status != WORKFLOW_RUN_RUNNING:
        raise ValueError(
            f"run {run_id} is {run.status}, so there is nothing on it to answer"
        )

    step_run = open_step_run(session, company_id, run_id)
    if step_run is None:
        raise ValueError(f"run {run_id} has no open step to answer")
    if not acting_user_id or step_run.assigned_to_user_id != acting_user_id:
        raise ValueError(
            f"step {step_run.step_id} of run {run_id} is with "
            f"{step_run.assigned_to_user_id or 'nobody'} and "
            f"{acting_user_id or 'nobody'} tried to answer it. An approval that "
            "does not name the account that was asked is not evidence of anything."
        )

    step_run.outcome = outcome
    step_run.acted_at = stamp
    step_run.acted_by_user_id = acting_user_id
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=(
            ACTION_ACTION_APPROVED
            if outcome == OUTCOME_APPROVED
            else ACTION_ACTION_REJECTED
        ),
        subject_type=SUBJECT_STEP_RUN,
        subject_id=str(step_run.id),
        reason=(
            f"{_display_name(session, company_id, acting_user_id)} {outcome} "
            f"{step_run.step_id} of run {run_id}"
        ),
        occurred_at=stamp,
        actor_user_id=acting_user_id,
        actor_kind=ACTOR_USER,
    )

    if outcome == OUTCOME_REJECTED:
        # The run stops where it stands. current_step_id goes NULL because the
        # item is on no step any more, and the status is what says which of the
        # two NULL meanings this is -- nothing may infer "finished" from a
        # missing step.
        run.status = WORKFLOW_RUN_REJECTED
        run.current_step_id = None
        session.flush()
        record_event(
            session,
            company_id=company_id,
            actor=who,
            action=ACTION_RUN_REJECTED,
            subject_type=SUBJECT_RUN,
            subject_id=run.id,
            reason=(
                f"stopped at {step_run.step_id}: "
                f"{_display_name(session, company_id, acting_user_id)} refused it. "
                "The steps after it were never asked."
            ),
            occurred_at=stamp,
            actor_user_id=acting_user_id,
            actor_kind=ACTOR_USER,
        )
        return step_run

    _advance(session, company_id, run, actor=who, moment=stamp)
    return step_run


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def _remind(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    step: WorkflowStep,
    step_run: WorkflowStepRun,
    *,
    actor: str,
    moment: datetime,
) -> int:
    """Chase the person who has it. Nothing moves and nobody is replaced.

    HOW MANY ARE OWED. One the moment the deadline passes, and one more for each
    whole interval since. At a deadline of 24 hours with a four-hour interval,
    a tick at 36 hours owes four: the deadline itself and three intervals.

    IDEMPOTENT BY COUNTER. reminder_count is what has been sent, the arithmetic
    says what is owed, and the difference is what this call writes. Running the
    tick twice at the same moment sends nothing the second time -- the counter
    already matches -- which is the property the whole sweep depends on.
    """
    interval = _positive_int(step.remind_every_hours)
    if interval is None or step_run.due_at is None:
        return 0

    owed = ((moment - step_run.due_at) // timedelta(hours=interval)) + 1
    already = step_run.reminder_count or 0
    if owed <= already:
        return 0

    first = already + 1
    dropped = 0
    if owed - already > REMINDER_CATCH_UP_LIMIT:
        # The sweep has not run for a long time. Write the last few rather than
        # hundreds, move the counter to the full number so the next tick repeats
        # nothing, and say in the row how many were owed.
        dropped = owed - already - REMINDER_CATCH_UP_LIMIT
        first = owed - REMINDER_CATCH_UP_LIMIT + 1

    for number in range(first, owed + 1):
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_STEP_REMINDED,
            subject_type=SUBJECT_STEP_RUN,
            subject_id=str(step_run.id),
            reason=(
                f"reminder {number} of {owed} for {step.id} of run {run.id}. It was "
                f"due {step_run.due_at.isoformat()} and is still with "
                f"{_display_name(session, company_id, step_run.assigned_to_user_id)}. "
                "A reminder is not an answer: the step is still open and nothing "
                "advanced."
                + (
                    f" {dropped} earlier reminder(s) were owed and are not written "
                    "one row each, because this sweep ran late."
                    if dropped and number == first
                    else ""
                )
            ),
            occurred_at=moment,
        )
    step_run.reminder_count = owed
    session.flush()
    return owed - already


def _escalate(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    step: WorkflowStep,
    step_run: WorkflowStepRun,
    *,
    actor: str,
    moment: datetime,
) -> tuple[bool, str]:
    """Pass the step to somebody else, or leave it exactly where it is.

    ESCALATION FAILING IS NOT PERMISSION TO SKIP, AND THIS FUNCTION HAS NO PATH
    TO A BYPASS. When escalate_to reaches nobody -- the role is empty, the
    account is suspended, two people hold the role -- the step STAYS PENDING and
    STAYS WITH THE PERSON WHO ALREADY HAD IT, and the failure is recorded once.
    Falling through to bypass here would turn a staffing problem into an
    unapproved action that reads as an approved one, which is the single worst
    thing this file could do.

    A target that resolves to the person already holding the step is the same
    refusal. Handing somebody their own overdue step is not an escalation, and
    doing it every tick would fill the record with movement that never happened.

    WHEN IT DOES ESCALATE, BOTH PEOPLE STAY ON THE RECORD. The first row closes
    with outcome escalated and acted_by_user_id NULL -- the clock is not a person
    -- and a NEW row opens for the same step with the new assignee. Who was asked
    first and did not answer is not overwritten.
    """
    target = step.escalate_to

    def fail(text: str) -> tuple[bool, str]:
        if not _already_recorded(
            session, company_id, ACTION_STEP_ESCALATION_FAILED, step_run.id
        ):
            record_event(
                session,
                company_id=company_id,
                actor=actor,
                action=ACTION_STEP_ESCALATION_FAILED,
                subject_type=SUBJECT_STEP_RUN,
                subject_id=str(step_run.id),
                reason=(
                    f"{step.id} of run {run.id} timed out and could not escalate: "
                    f"{text} It is STILL OPEN and still with "
                    f"{_display_name(session, company_id, step_run.assigned_to_user_id)}. "
                    "A failed escalation is not permission to skip the step, so "
                    "nothing was bypassed and nothing advanced."
                ),
                occurred_at=moment,
            )
        return False, text

    if not _target_is_well_formed(target):
        return fail(
            f"escalate_to is {target!r}, which names nobody. Activation refuses "
            "that, so this route was changed some other way."
        )

    resolution = resolve_assignee(
        session,
        company_id,
        rule=target,
        escalation_id=run.escalation_id,
        now=moment,
    )
    if not resolution.routed:
        return fail(f"{resolution.reason_code}: {resolution.reason_text}")
    if resolution.user_id == step_run.assigned_to_user_id:
        return fail(
            f"{target} resolves to "
            f"{_display_name(session, company_id, resolution.user_id)}, who already "
            "has this step. Escalating to the same desk would move nothing."
        )

    was = step_run.assigned_to_user_id
    step_run.outcome = OUTCOME_ESCALATED
    step_run.acted_at = moment
    # acted_by_user_id stays NULL. Nobody acted; a clock ran out.
    session.flush()

    hours = _positive_int(step.approval_hours)
    fresh = WorkflowStepRun(
        company_id=company_id,
        run_id=run.id,
        step_id=step.id,
        assigned_to_user_id=resolution.user_id,
        assigned_at=moment,
        due_at=moment + timedelta(hours=hours) if hours else None,
        outcome=None,
        acted_at=None,
        acted_by_user_id=None,
        reminder_count=0,
    )
    session.add(fresh)
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_STEP_ESCALATED,
        subject_type=SUBJECT_STEP_RUN,
        subject_id=str(step_run.id),
        reason=(
            f"{step.id} of run {run.id} was with "
            f"{_display_name(session, company_id, was)}, who did not answer by "
            f"{step_run.due_at.isoformat() if step_run.due_at else 'the deadline'}. "
            f"It passed to {_display_name(session, company_id, resolution.user_id)} "
            f"({resolution.reason_code}: {resolution.reason_text}) and is due "
            f"{fresh.due_at.isoformat() if fresh.due_at else 'with no deadline'}. "
            "The first attempt stays on the record."
        ),
        occurred_at=moment,
    )
    return True, "escalated"


def _bypass(
    session: Session,
    company_id: str,
    run: WorkflowRun,
    step: WorkflowStep,
    step_run: WorkflowStepRun,
    *,
    actor: str,
    moment: datetime,
) -> None:
    """The sanctioned fallback: the run moves on WITHOUT an approval.

    THE OUTCOME IS bypassed AND NEVER approved. Nobody answered, so nothing may
    later report that anybody did. acted_by_user_id stays NULL for the same
    reason a bypass names no actor: the clock is not a person, and a row naming
    one would be a false attribution in the one table that exists to be
    believed.

    It announces itself three times -- the outcome value, this audit row, and
    approval_summary().bypassed_steps -- because one of the three is the one a
    given reader will look at.
    """
    step_run.outcome = OUTCOME_BYPASSED
    step_run.acted_at = moment
    session.flush()
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_STEP_BYPASSED,
        subject_type=SUBJECT_STEP_RUN,
        subject_id=str(step_run.id),
        reason=(
            f"nobody answered {step.id} of run {run.id}. Its time ran out at "
            f"{step_run.due_at.isoformat() if step_run.due_at else 'its deadline'} "
            "and the route says bypass, so the run moved on WITHOUT an approval. "
            "It was with "
            f"{_display_name(session, company_id, step_run.assigned_to_user_id)}. "
            "This step is recorded as bypassed and never as approved."
        ),
        occurred_at=moment,
    )
    _advance(session, company_id, run, actor=actor, moment=moment)


def advance_overdue(
    session: Session,
    company_id: str,
    *,
    now: datetime | None = None,
    actor: str = CLOCK_ACTOR,
) -> SweepReport:
    """Run the clock once over every live run in one company. The whole scheduler.

    THERE IS NO SCHEDULER IN THIS PRODUCT, and this function is the admission
    rather than the workaround. Nothing here polls, nothing sleeps, and no
    deadline is noticed until somebody calls this. In production that caller is
    a cron entry or a small worker loop, once a minute per company; in this build
    it is the demo's own request path and a button on the admin screen. Whatever
    calls it, `now` is passed in, so a test drives a deadline two days past in one
    argument and nobody waits.

    IDEMPOTENT, AND EVERY BRANCH SAYS HOW. A reminder is counted, so the same
    moment twice sends nothing twice. A bypass closes the row and the run leaves
    the running set, so the second tick does not see it. An escalation closes its
    row too. A failed escalation and a stall change no column at all, so their
    guard is the audit chain: one row per step run, asked before writing.

    A COMPLETED, REJECTED OR CANCELLED RUN IS NOT EXAMINED. That is what makes
    "run it twice" cheap as well as safe, and it is why `checked` counts only the
    running ones.

    A step with nobody on it is never touched by the clock. It has no deadline,
    so it cannot be overdue, so it can never be bypassed however long it sits.
    It is reported in unrouted_step_run_ids and it needs a person.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    moment = _require_aware(now or _utcnow(), "now")

    checked = 0
    reminders = 0
    escalated: list[int] = []
    bypassed: list[int] = []
    unrouted: list[int] = []
    stalled: list[int] = []
    completed: list[str] = []

    runs = (
        session.query(WorkflowRun)
        .filter(WorkflowRun.company_id == company_id)
        .filter(WorkflowRun.status == WORKFLOW_RUN_RUNNING)
        .order_by(WorkflowRun.id)
        .all()
    )

    for run in runs:
        checked += 1
        step_run = open_step_run(session, company_id, run.id)
        if step_run is None:
            # A running run with no open step. Nothing here can invent one, and
            # guessing which step it should be standing on would be worse than
            # leaving it visible in the running list for somebody to look at.
            continue

        steps = _steps_by_id(session, run.workflow_id)
        step = steps.get(step_run.step_id)
        if step is None:
            stalled.append(step_run.id)
            if not _already_recorded(
                session, company_id, ACTION_STEP_STALLED, step_run.id
            ):
                record_event(
                    session,
                    company_id=company_id,
                    actor=who,
                    action=ACTION_STEP_STALLED,
                    subject_type=SUBJECT_STEP_RUN,
                    subject_id=str(step_run.id),
                    reason=(
                        f"run {run.id} stands on {step_run.step_id}, which route "
                        f"{run.workflow_id} no longer has. The clock cannot act on "
                        "a step it cannot read."
                    ),
                    occurred_at=moment,
                )
            continue

        if step_run.assigned_to_user_id is None or step_run.due_at is None:
            unrouted.append(step_run.id)
            continue

        if moment < step_run.due_at:
            continue

        rule = step.on_timeout
        if rule == TIMEOUT_REMIND:
            reminders += _remind(
                session, company_id, run, step, step_run, actor=who, moment=moment
            )
        elif rule == TIMEOUT_ESCALATE:
            moved, _text = _escalate(
                session, company_id, run, step, step_run, actor=who, moment=moment
            )
            if moved:
                escalated.append(step_run.id)
            else:
                stalled.append(step_run.id)
        elif rule == TIMEOUT_BYPASS:
            _bypass(
                session, company_id, run, step, step_run, actor=who, moment=moment
            )
            bypassed.append(step_run.id)
            if run.status == WORKFLOW_RUN_COMPLETED:
                completed.append(run.id)
        else:
            # No rule, or one this build cannot read. Activation refuses both, so
            # the route was changed some other way. The step stays pending: an
            # engine that picked a rule for it would be inventing a decision
            # nobody made, and the only one it could invent silently is the one
            # that skips an approval.
            stalled.append(step_run.id)
            if not _already_recorded(
                session, company_id, ACTION_STEP_STALLED, step_run.id
            ):
                record_event(
                    session,
                    company_id=company_id,
                    actor=who,
                    action=ACTION_STEP_STALLED,
                    subject_type=SUBJECT_STEP_RUN,
                    subject_id=str(step_run.id),
                    reason=(
                        f"{step.id} of run {run.id} timed out and on_timeout is "
                        f"{rule!r}, which says nothing this engine can act on. The "
                        "step is still open and still assigned; nothing was "
                        "bypassed."
                    ),
                    occurred_at=moment,
                )

    return SweepReport(
        checked=checked,
        reminders_sent=reminders,
        escalated_step_run_ids=tuple(escalated),
        bypassed_step_run_ids=tuple(bypassed),
        unrouted_step_run_ids=tuple(unrouted),
        stalled_step_run_ids=tuple(stalled),
        completed_run_ids=tuple(completed),
    )


# ---------------------------------------------------------------------------
# Did anybody actually approve this?
# ---------------------------------------------------------------------------


def approval_summary(
    session: Session, company_id: str, run_id: str
) -> ApprovalSummary:
    """What every step of this run came to, as lists a caller cannot round off.

    THE ANSWER TO "WAS THIS APPROVED" IS NOT A BOOLEAN, and that is the whole
    design of this function. A run that reached the end with a bypassed step is
    completed and is NOT approved, and anything downstream -- a filing, a
    deliverable that cites the approval, an auditor a year later -- has to be
    able to see WHICH steps nobody answered. Handing back True or False would let
    the caller report a run containing a bypass as approved, and the caller would
    not even be wrong to, because the word says nothing about the steps.

    An unknown run raises rather than returning an empty summary. An empty
    summary reads as "nothing was bypassed", which is a comforting answer to a
    question this function was not able to ask.
    """
    _require_scope(company_id)
    run = run_for_company(session, company_id, run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} for this company")

    approved: list[str] = []
    rejected: list[str] = []
    skipped: list[str] = []
    passed_on: list[str] = []
    open_now: list[str] = []
    for row in step_runs_for_run(session, company_id, run_id):
        if row.outcome == OUTCOME_APPROVED:
            approved.append(row.step_id)
        elif row.outcome == OUTCOME_REJECTED:
            rejected.append(row.step_id)
        elif row.outcome == OUTCOME_BYPASSED:
            skipped.append(row.step_id)
        elif row.outcome == OUTCOME_ESCALATED:
            # Not a verdict on the step, only on the attempt: another row for the
            # same step carries what happened next. It is listed so a reader can
            # see the step was chased, and it does not stop a run being fully
            # approved when the next attempt was approved.
            passed_on.append(row.step_id)
        elif row.outcome is None:
            open_now.append(row.step_id)

    return ApprovalSummary(
        run_id=run.id,
        status=run.status,
        approved_steps=tuple(approved),
        rejected_steps=tuple(rejected),
        bypassed_steps=tuple(skipped),
        escalated_steps=tuple(passed_on),
        open_steps=tuple(open_now),
    )


__all__ = [
    "ACTION_RUN_COMPLETED",
    "ACTION_RUN_REFUSED",
    "ACTION_RUN_REJECTED",
    "ACTION_RUN_STARTED",
    "ACTION_STEP_ASSIGNED",
    "ACTION_STEP_BYPASSED",
    "ACTION_STEP_ESCALATED",
    "ACTION_STEP_ESCALATION_FAILED",
    "ACTION_STEP_REMINDED",
    "ACTION_STEP_STALLED",
    "ACTION_STEP_UNROUTED",
    "ACTION_WORKFLOW_ACTIVATED",
    "ACTION_WORKFLOW_ARCHIVED",
    "ACTION_WORKFLOW_CREATED",
    "ACTION_WORKFLOW_SAVED",
    "ApprovalSummary",
    "CLOCK_ACTOR",
    "GraphError",
    "GraphResult",
    "REMINDER_CATCH_UP_LIMIT",
    "RUN_ALREADY_STARTED",
    "RUN_NO_ACTIVE_WORKFLOW",
    "RUN_NO_ESCALATION",
    "RUN_OK",
    "RUN_REASON_CODES",
    "RUN_ROUTE_EMPTY",
    "SUBJECT_RUN",
    "SUBJECT_STEP_RUN",
    "SUBJECT_WORKFLOW",
    "StartResult",
    "SweepReport",
    "activate_workflow",
    "active_workflow",
    "advance_overdue",
    "approval_summary",
    "create_workflow",
    "desk_for_user",
    "graph_dict",
    "open_step_run",
    "record_decision",
    "run_for_company",
    "run_for_escalation",
    "save_graph",
    "start_run",
    "step_order",
    "step_runs_for_run",
    "unrouted_step_runs",
    "validation_errors",
    "workflow_for_company",
    "workflows_for_company",
]
