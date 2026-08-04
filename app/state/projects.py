"""The project workspace: projects, their changes, threads, plans, runs, knowledge.

Three rules govern this module. The first two are inherited; the third is new
here and is written down so the next function follows it rather than a habit.

First, every function takes a company_id and refuses an unscoped call. The
guard is imported from app/state/queries.py, not copied, because two copies of
a tenant guard drift and the point of a chokepoint is that there is only one.

Second, an id is never trusted on its own. A project id, change id, thread id,
plan id or step id arriving from a URL is checked against the company that owns
it before any row is read or written. Where a row hangs off a parent -- a step
off a plan off a project -- the join walks all the way up and every hop is
filtered on the same company. The child's own company_id would usually do; it
is the column a bad write would have got wrong, so it cannot be the only thing
asked.

Third, what earns an audit event. An event is appended when the company commits
to something: a project opened, a change attached to a project, a plan step
moved, a piece of knowledge superseded. Opening a research thread and adding a
turn are enquiry, not commitment -- they are append-only rows with their own
timestamps that nothing edits -- so they stay out of the hash chain. Adding
every keystroke to the chain would make it long and make it ignored, and a log
nobody reads proves nothing under dispute.

Nothing here returns a stored count. project_card() derives every number it
shows from the rows underneath it, so a tile cannot go stale against the data
it summarises.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.state.audit import record_event

# Imported, not copied. Same lookup, same tenant join, one place to audit.
from app.state.claims import change_for_company
from app.state.models import (
    KNOWLEDGE_KINDS,
    PROJECT_STATUSES,
    RUN_CADENCES,
    STEP_STATES,
    THREAD_STATUSES,
    TURN_AUTHOR_KINDS,
    Change,
    Claim,
    Escalation,
    KnowledgeItem,
    Project,
    ProjectChange,
    ResearchThread,
    ResearchTurn,
    ScheduledRun,
    WorkPlan,
    WorkPlanStep,
)
from app.state.queries import _require_scope


def _new_id(prefix: str) -> str:
    """A short, opaque id. Callers may pass their own; seeds and tests do."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_actor(actor: str) -> str:
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    return actor


def _require_one_of(value: str, allowed: tuple[str, ...], field: str) -> str:
    """Reject an unknown value at the write, where the caller can still fix it.

    A free-text status column fills up with near-synonyms -- done, DONE,
    complete -- and every read afterwards has to guess which ones meant the
    same thing. Refusing at the write keeps that guess from ever being needed.
    """
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(
            f"{field} must carry a timezone; a naive timestamp is a defect"
        )
    return moment


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def projects_for_company(session: Session, company_id: str) -> list[Project]:
    _require_scope(company_id)
    return (
        session.query(Project)
        .filter(Project.company_id == company_id)
        .order_by(Project.id)
        .all()
    )


def project_for_company(
    session: Session, company_id: str, project_id: str
) -> Project | None:
    """One project, or None. Another company's project id resolves to None."""
    _require_scope(company_id)
    return (
        session.query(Project)
        .filter(Project.company_id == company_id)
        .filter(Project.id == project_id)
        .one_or_none()
    )


def create_project(
    session: Session,
    company_id: str,
    *,
    name: str,
    jurisdiction: str,
    owner: str,
    docket_ref: str | None = None,
    summary: str = "",
    status: str = "active",
    project_id: str | None = None,
    actor: str | None = None,
    created_at: datetime | None = None,
) -> Project:
    """Open a project and record that it was opened.

    The owner is the actor unless one is given, so a project opened on somebody
    else's behalf records who actually opened it rather than quietly crediting
    the owner.
    """
    _require_scope(company_id)
    _require_one_of(status, PROJECT_STATUSES, "status")
    if not name:
        raise ValueError("a project needs a name")
    if not jurisdiction:
        raise ValueError("a project needs a jurisdiction; it decides what binds it")
    _require_actor(owner)
    who = _require_actor(actor or owner)

    stamp = _require_aware(created_at or _utcnow(), "created_at")
    project = Project(
        id=project_id or _new_id("proj"),
        company_id=company_id,
        name=name,
        docket_ref=docket_ref,
        jurisdiction=jurisdiction,
        status=status,
        owner=owner,
        created_at=stamp,
        updated_at=stamp,
        summary=summary,
    )
    session.add(project)
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action="project.created",
        subject_type="project",
        subject_id=project.id,
        reason=f"opened {name} in {jurisdiction}, owner {owner}",
        occurred_at=stamp,
    )
    session.flush()
    return project


# ---------------------------------------------------------------------------
# Changes attached to a project
# ---------------------------------------------------------------------------


def changes_for_project(
    session: Session, company_id: str, project_id: str
) -> list[Change]:
    """The changes attached to one project, in id order, scoped both ways.

    Three company filters on one query is not belt and braces for its own sake.
    The attachment row, the project it names and the change it names were each
    written by a different call, and a bug in any one of them is what hands a
    tenant somebody else's docket.
    """
    _require_scope(company_id)
    return (
        session.query(Change)
        .join(ProjectChange, ProjectChange.change_id == Change.id)
        .join(Project, ProjectChange.project_id == Project.id)
        .filter(Change.company_id == company_id)
        .filter(ProjectChange.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ProjectChange.project_id == project_id)
        .order_by(Change.id)
        .all()
    )


def attachments_for_project(
    session: Session, company_id: str, project_id: str
) -> list[ProjectChange]:
    """The attachment rows themselves -- who attached what, and when."""
    _require_scope(company_id)
    return (
        session.query(ProjectChange)
        .join(Project, ProjectChange.project_id == Project.id)
        .filter(ProjectChange.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ProjectChange.project_id == project_id)
        .order_by(ProjectChange.id)
        .all()
    )


def attach_change(
    session: Session,
    company_id: str,
    project_id: str,
    change_id: str,
    actor: str,
) -> ProjectChange:
    """Bind a change to a project. The same change may bind several projects.

    Attaching twice returns the first attachment rather than raising or writing
    a second row. A double click is not a decision, and recording it as one
    would put a duplicate in the audit chain that a reader would have to work
    out the meaning of.

    An unknown project or change raises. Returning None here would let a caller
    carry on believing the attachment happened, and the failure would surface
    later as a project that quietly lists nothing.
    """
    _require_scope(company_id)
    who = _require_actor(actor)

    project = project_for_company(session, company_id, project_id)
    if project is None:
        raise ValueError(f"no project {project_id!r} for this company")
    change = change_for_company(session, company_id, change_id)
    if change is None:
        raise ValueError(f"no change {change_id!r} for this company")

    existing = (
        session.query(ProjectChange)
        .filter(ProjectChange.company_id == company_id)
        .filter(ProjectChange.project_id == project_id)
        .filter(ProjectChange.change_id == change_id)
        .one_or_none()
    )
    if existing is not None:
        return existing

    stamp = _utcnow()
    link = ProjectChange(
        company_id=company_id,
        project_id=project_id,
        change_id=change_id,
        attached_at=stamp,
        attached_by=who,
    )
    session.add(link)
    project.updated_at = stamp
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action="project.change_attached",
        subject_type="project_change",
        subject_id=f"{project_id}:{change_id}",
        reason=f"change {change_id} attached to project {project_id}",
        occurred_at=stamp,
    )
    session.flush()
    return link


def projects_for_change(
    session: Session, company_id: str, change_id: str
) -> list[Project]:
    """Every project one change binds. The other direction of the same join."""
    _require_scope(company_id)
    return (
        session.query(Project)
        .join(ProjectChange, ProjectChange.project_id == Project.id)
        .filter(Project.company_id == company_id)
        .filter(ProjectChange.company_id == company_id)
        .filter(ProjectChange.change_id == change_id)
        .order_by(Project.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Research threads
# ---------------------------------------------------------------------------


def threads_for_project(
    session: Session, company_id: str, project_id: str
) -> list[ResearchThread]:
    """A project's threads, most recently active first."""
    _require_scope(company_id)
    return (
        session.query(ResearchThread)
        .join(Project, ResearchThread.project_id == Project.id)
        .filter(ResearchThread.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ResearchThread.project_id == project_id)
        .order_by(ResearchThread.last_activity_at.desc(), ResearchThread.id)
        .all()
    )


def thread_for_company(
    session: Session, company_id: str, thread_id: str
) -> ResearchThread | None:
    _require_scope(company_id)
    return (
        session.query(ResearchThread)
        .join(Project, ResearchThread.project_id == Project.id)
        .filter(ResearchThread.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ResearchThread.id == thread_id)
        .one_or_none()
    )


def turns_for_thread(
    session: Session, company_id: str, thread_id: str
) -> list[ResearchTurn]:
    """A thread's turns in the order they were written. Never reordered."""
    _require_scope(company_id)
    return (
        session.query(ResearchTurn)
        .join(ResearchThread, ResearchTurn.thread_id == ResearchThread.id)
        .join(Project, ResearchThread.project_id == Project.id)
        .filter(ResearchTurn.company_id == company_id)
        .filter(ResearchThread.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ResearchTurn.thread_id == thread_id)
        .order_by(ResearchTurn.id)
        .all()
    )


def open_thread(
    session: Session,
    company_id: str,
    project_id: str,
    *,
    question: str,
    opened_by: str,
    thread_id: str | None = None,
    opened_at: datetime | None = None,
) -> ResearchThread:
    """Open a line of enquiry against a project. Starts with status "open"."""
    _require_scope(company_id)
    who = _require_actor(opened_by)
    if not question:
        raise ValueError("a thread is a question; it cannot be empty")

    if project_for_company(session, company_id, project_id) is None:
        raise ValueError(f"no project {project_id!r} for this company")

    stamp = _require_aware(opened_at or _utcnow(), "opened_at")
    thread = ResearchThread(
        id=thread_id or _new_id("thr"),
        company_id=company_id,
        project_id=project_id,
        question=question,
        status="open",
        opened_by=who,
        opened_at=stamp,
        last_activity_at=stamp,
    )
    session.add(thread)
    session.flush()
    return thread


def add_turn(
    session: Session,
    company_id: str,
    thread_id: str,
    *,
    author_kind: str,
    body: str,
    claim_id: str | None = None,
    created_at: datetime | None = None,
) -> ResearchTurn:
    """Append a turn and move the thread's last_activity_at.

    A cited claim is checked to belong to this company before it is stored. A
    turn is rendered next to the claim it cites, so an unchecked claim id here
    is a way to pull another tenant's text onto this tenant's page -- and it
    would arrive looking like the company's own research.
    """
    _require_scope(company_id)
    _require_one_of(author_kind, TURN_AUTHOR_KINDS, "author_kind")
    if not body:
        raise ValueError("a turn needs a body")

    thread = thread_for_company(session, company_id, thread_id)
    if thread is None:
        raise ValueError(f"no thread {thread_id!r} for this company")

    if claim_id is not None:
        owned = (
            session.query(Claim)
            .filter(Claim.company_id == company_id)
            .filter(Claim.id == claim_id)
            .one_or_none()
        )
        if owned is None:
            raise ValueError(f"no claim {claim_id!r} for this company")

    stamp = _require_aware(created_at or _utcnow(), "created_at")
    turn = ResearchTurn(
        thread_id=thread_id,
        company_id=company_id,
        author_kind=author_kind,
        body=body,
        created_at=stamp,
        claim_id=claim_id,
    )
    session.add(turn)
    if stamp > thread.last_activity_at:
        thread.last_activity_at = stamp
    session.flush()
    return turn


def set_thread_status(
    session: Session, company_id: str, thread_id: str, status: str
) -> ResearchThread:
    """Answer or park a thread. The question and its turns stay as written."""
    _require_scope(company_id)
    _require_one_of(status, THREAD_STATUSES, "status")
    thread = thread_for_company(session, company_id, thread_id)
    if thread is None:
        raise ValueError(f"no thread {thread_id!r} for this company")
    thread.status = status
    session.flush()
    return thread


# ---------------------------------------------------------------------------
# Work plans
# ---------------------------------------------------------------------------


def work_plans_for_project(
    session: Session, company_id: str, project_id: str
) -> list[WorkPlan]:
    _require_scope(company_id)
    return (
        session.query(WorkPlan)
        .join(Project, WorkPlan.project_id == Project.id)
        .filter(WorkPlan.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(WorkPlan.project_id == project_id)
        .order_by(WorkPlan.created_at, WorkPlan.id)
        .all()
    )


def create_work_plan(
    session: Session,
    company_id: str,
    project_id: str,
    *,
    title: str,
    plan_id: str | None = None,
    created_at: datetime | None = None,
    due_at: datetime | None = None,
) -> WorkPlan:
    _require_scope(company_id)
    if not title:
        raise ValueError("a plan needs a title")
    if project_for_company(session, company_id, project_id) is None:
        raise ValueError(f"no project {project_id!r} for this company")

    plan = WorkPlan(
        id=plan_id or _new_id("plan"),
        company_id=company_id,
        project_id=project_id,
        title=title,
        created_at=_require_aware(created_at or _utcnow(), "created_at"),
        due_at=None if due_at is None else _require_aware(due_at, "due_at"),
    )
    session.add(plan)
    session.flush()
    return plan


def steps_for_plan(
    session: Session, company_id: str, plan_id: str
) -> list[WorkPlanStep]:
    """A plan's steps in the order the analyst set."""
    _require_scope(company_id)
    return (
        session.query(WorkPlanStep)
        .join(WorkPlan, WorkPlanStep.plan_id == WorkPlan.id)
        .join(Project, WorkPlan.project_id == Project.id)
        .filter(WorkPlanStep.company_id == company_id)
        .filter(WorkPlan.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(WorkPlanStep.plan_id == plan_id)
        .order_by(WorkPlanStep.ordinal, WorkPlanStep.id)
        .all()
    )


def add_step(
    session: Session,
    company_id: str,
    plan_id: str,
    *,
    description: str,
    owner: str,
    ordinal: int | None = None,
    state: str = "todo",
    change_id: str | None = None,
    due_at: datetime | None = None,
    step_id: str | None = None,
) -> WorkPlanStep:
    """Append a step. ordinal defaults to the end of the plan.

    A step tied to a change is checked against this company, for the same
    reason a cited claim is: the step renders the change's words beside it.
    """
    _require_scope(company_id)
    _require_one_of(state, STEP_STATES, "state")
    _require_actor(owner)
    if not description:
        raise ValueError("a step needs a description")

    plan = (
        session.query(WorkPlan)
        .join(Project, WorkPlan.project_id == Project.id)
        .filter(WorkPlan.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(WorkPlan.id == plan_id)
        .one_or_none()
    )
    if plan is None:
        raise ValueError(f"no work plan {plan_id!r} for this company")

    if change_id is not None:
        if change_for_company(session, company_id, change_id) is None:
            raise ValueError(f"no change {change_id!r} for this company")

    if ordinal is None:
        ordinal = len(steps_for_plan(session, company_id, plan_id)) + 1

    step = WorkPlanStep(
        id=step_id or _new_id("step"),
        plan_id=plan_id,
        company_id=company_id,
        ordinal=ordinal,
        description=description,
        owner=owner,
        state=state,
        change_id=change_id,
        due_at=None if due_at is None else _require_aware(due_at, "due_at"),
    )
    session.add(step)
    session.flush()
    return step


def set_step_state(
    session: Session, company_id: str, step_id: str, state: str, actor: str
) -> WorkPlanStep:
    """Move a step and record who moved it.

    This one earns an audit event where opening a thread does not. A step is a
    commitment about what the company will do by when, so "who marked the
    filing done, and when" is a question that gets asked afterwards.
    """
    _require_scope(company_id)
    _require_one_of(state, STEP_STATES, "state")
    who = _require_actor(actor)

    step = (
        session.query(WorkPlanStep)
        .join(WorkPlan, WorkPlanStep.plan_id == WorkPlan.id)
        .join(Project, WorkPlan.project_id == Project.id)
        .filter(WorkPlanStep.company_id == company_id)
        .filter(WorkPlan.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(WorkPlanStep.id == step_id)
        .one_or_none()
    )
    if step is None:
        raise ValueError(f"no work plan step {step_id!r} for this company")

    was = step.state
    step.state = state
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action="work_plan_step.state_changed",
        subject_type="work_plan_step",
        subject_id=step_id,
        reason=f"step moved from {was} to {state}",
    )
    session.flush()
    return step


# ---------------------------------------------------------------------------
# Scheduled re-runs
# ---------------------------------------------------------------------------


def scheduled_runs_for_project(
    session: Session, company_id: str, project_id: str
) -> list[ScheduledRun]:
    _require_scope(company_id)
    return (
        session.query(ScheduledRun)
        .join(Project, ScheduledRun.project_id == Project.id)
        .filter(ScheduledRun.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ScheduledRun.project_id == project_id)
        .order_by(ScheduledRun.next_run_at, ScheduledRun.id)
        .all()
    )


def schedule_run(
    session: Session,
    company_id: str,
    project_id: str,
    *,
    cadence: str,
    next_run_at: datetime,
    run_id: str | None = None,
    enabled: bool = True,
) -> ScheduledRun:
    _require_scope(company_id)
    _require_one_of(cadence, RUN_CADENCES, "cadence")
    if project_for_company(session, company_id, project_id) is None:
        raise ValueError(f"no project {project_id!r} for this company")

    run = ScheduledRun(
        id=run_id or _new_id("run"),
        company_id=company_id,
        project_id=project_id,
        cadence=cadence,
        last_run_at=None,
        next_run_at=_require_aware(next_run_at, "next_run_at"),
        enabled=enabled,
        last_result=None,
    )
    session.add(run)
    session.flush()
    return run


def due_runs(
    session: Session, company_id: str, now: datetime
) -> list[ScheduledRun]:
    """Enabled runs whose next_run_at has passed, earliest first.

    The company and enabled filters run in SQL; the time comparison runs in
    Python. UtcDateTime stores an ISO string, and comparing those as text is
    right almost always -- which is the property that makes it a trap rather
    than a bug you find. Aware datetimes compare exactly, the set of runs per
    company is small, and when it is not, the fix is a real timestamp column in
    Postgres rather than a cleverer string.
    """
    _require_scope(company_id)
    _require_aware(now, "now")

    enabled = (
        session.query(ScheduledRun)
        .join(Project, ScheduledRun.project_id == Project.id)
        .filter(ScheduledRun.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ScheduledRun.enabled.is_(True))
        .all()
    )
    due = [run for run in enabled if run.next_run_at <= now]
    return sorted(due, key=lambda run: (run.next_run_at, run.id))


def record_run(
    session: Session,
    company_id: str,
    run_id: str,
    *,
    ran_at: datetime,
    result: str,
    next_run_at: datetime,
) -> ScheduledRun:
    """Stamp a run as done and set when it fires next.

    result is written even when the run found nothing. A schedule that reports
    only on the days it found something looks identical to a schedule that
    stopped running, and that is the exact staleness this table exists to make
    visible.
    """
    _require_scope(company_id)
    if not result:
        raise ValueError("a run records what happened, including 'no change found'")

    run = (
        session.query(ScheduledRun)
        .join(Project, ScheduledRun.project_id == Project.id)
        .filter(ScheduledRun.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(ScheduledRun.id == run_id)
        .one_or_none()
    )
    if run is None:
        raise ValueError(f"no scheduled run {run_id!r} for this company")

    run.last_run_at = _require_aware(ran_at, "ran_at")
    run.last_result = result
    run.next_run_at = _require_aware(next_run_at, "next_run_at")
    session.flush()
    return run


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


def knowledge_for_project(
    session: Session, company_id: str, project_id: str
) -> list[KnowledgeItem]:
    """A project's live knowledge. Superseded items are excluded, not deleted.

    Excluded means the current view shows what the company believes now. The
    superseded row is still readable by id, and knowledge_history() walks the
    chain, so "what did we believe in March" stays answerable.
    """
    _require_scope(company_id)
    return (
        session.query(KnowledgeItem)
        .join(Project, KnowledgeItem.project_id == Project.id)
        .filter(KnowledgeItem.company_id == company_id)
        .filter(Project.company_id == company_id)
        .filter(KnowledgeItem.project_id == project_id)
        .filter(KnowledgeItem.superseded_by.is_(None))
        .order_by(KnowledgeItem.created_at, KnowledgeItem.id)
        .all()
    )


def knowledge_for_company(
    session: Session, company_id: str, *, include_superseded: bool = False
) -> list[KnowledgeItem]:
    """Every live item the company holds, project-scoped and company-wide alike."""
    _require_scope(company_id)
    query = session.query(KnowledgeItem).filter(KnowledgeItem.company_id == company_id)
    if not include_superseded:
        query = query.filter(KnowledgeItem.superseded_by.is_(None))
    return query.order_by(KnowledgeItem.created_at, KnowledgeItem.id).all()


def knowledge_item_for_company(
    session: Session, company_id: str, item_id: str
) -> KnowledgeItem | None:
    """One item, superseded or not. This is how history stays readable."""
    _require_scope(company_id)
    return (
        session.query(KnowledgeItem)
        .filter(KnowledgeItem.company_id == company_id)
        .filter(KnowledgeItem.id == item_id)
        .one_or_none()
    )


def add_knowledge(
    session: Session,
    company_id: str,
    *,
    kind: str,
    body: str,
    project_id: str | None = None,
    source_claim_id: str | None = None,
    confirmed_by: str | None = None,
    item_id: str | None = None,
    created_at: datetime | None = None,
) -> KnowledgeItem:
    """Record something the company learned. project_id None means company-wide."""
    _require_scope(company_id)
    _require_one_of(kind, KNOWLEDGE_KINDS, "kind")
    if not body:
        raise ValueError("a knowledge item needs a body")

    if project_id is not None:
        if project_for_company(session, company_id, project_id) is None:
            raise ValueError(f"no project {project_id!r} for this company")

    if source_claim_id is not None:
        owned = (
            session.query(Claim)
            .filter(Claim.company_id == company_id)
            .filter(Claim.id == source_claim_id)
            .one_or_none()
        )
        if owned is None:
            raise ValueError(f"no claim {source_claim_id!r} for this company")

    item = KnowledgeItem(
        id=item_id or _new_id("kn"),
        company_id=company_id,
        project_id=project_id,
        kind=kind,
        body=body,
        source_claim_id=source_claim_id,
        created_at=_require_aware(created_at or _utcnow(), "created_at"),
        confirmed_by=confirmed_by,
        superseded_by=None,
    )
    session.add(item)
    session.flush()
    return item


def supersede_knowledge(
    session: Session, company_id: str, item_id: str, new_body: str, actor: str
) -> KnowledgeItem:
    """Replace an item with a new one and point the old at it. Returns the new one.

    The old row's body is never touched. superseded_by is the only field this
    table ever writes twice, and it is written once -- an item already
    superseded is refused, because a second replacement would fork the history
    into two chains and neither would be the record.

    The actor's name goes on the new item as confirmed_by. Somebody just put
    their name to a changed belief, and that is worth more than the timestamp.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    if not new_body:
        raise ValueError("superseding needs a new body; use a status field to retire")

    old = knowledge_item_for_company(session, company_id, item_id)
    if old is None:
        raise ValueError(f"no knowledge item {item_id!r} for this company")
    if old.superseded_by is not None:
        raise ValueError(
            f"knowledge item {item_id!r} is already superseded by "
            f"{old.superseded_by!r}; supersede the current one"
        )

    stamp = _utcnow()
    replacement = KnowledgeItem(
        id=_new_id("kn"),
        company_id=company_id,
        project_id=old.project_id,
        kind=old.kind,
        body=new_body,
        source_claim_id=old.source_claim_id,
        created_at=stamp,
        confirmed_by=who,
        superseded_by=None,
    )
    session.add(replacement)
    session.flush()

    old.superseded_by = replacement.id
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action="knowledge.superseded",
        subject_type="knowledge_item",
        subject_id=item_id,
        reason=f"superseded by {replacement.id}",
        occurred_at=stamp,
    )
    session.flush()
    return replacement


def knowledge_history(
    session: Session, company_id: str, item_id: str
) -> list[KnowledgeItem]:
    """One item and everything that replaced it, oldest first.

    Starts wherever the caller points and walks forward. A cycle would loop
    forever, so seen ids stop it -- superseded_by is write-once and a cycle
    should be impossible, which is the usual reason nobody checks.
    """
    _require_scope(company_id)
    chain: list[KnowledgeItem] = []
    seen: set[str] = set()
    current = knowledge_item_for_company(session, company_id, item_id)
    while current is not None and current.id not in seen:
        chain.append(current)
        seen.add(current.id)
        if current.superseded_by is None:
            break
        current = knowledge_item_for_company(session, company_id, current.superseded_by)
    return chain


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectCard:
    """Exactly what a project tile needs, computed at read time.

    Frozen, and never stored. Every number here is derived from the rows it
    counts on the call that builds it, so a tile cannot disagree with the
    project behind it. Section 26 of the playbook says a fallback must announce
    itself; the same argument applies to a count -- a stale one announces
    nothing at all, it just looks like a quiet week.

    project_id is here because a tile is a link. The rest is the tile's text.
    """

    project_id: str
    name: str
    jurisdiction: str
    docket_ref: str | None
    status: str
    change_count: int
    unreviewed_count: int
    open_thread_count: int
    next_run_at: datetime | None
    last_activity_at: datetime


def project_card(
    session: Session, company_id: str, project_id: str
) -> ProjectCard | None:
    """Build one project's tile, or None if this company has no such project.

    unreviewed_count is the number of attached changes still carrying an
    unresolved escalation -- work waiting on a person, not work nobody has read.
    It is defined against escalations rather than a "seen" flag because a flag
    would say somebody opened the page, and opening a page is not review.

    last_activity_at is the latest of: the project's own updated_at, any
    attachment, any thread's last activity, and any run that fired. Stated
    plainly because a tile that says "quiet for three weeks" has to be either
    checkable or absent.
    """
    _require_scope(company_id)
    project = project_for_company(session, company_id, project_id)
    if project is None:
        return None

    attachments = attachments_for_project(session, company_id, project_id)
    change_ids = [row.change_id for row in attachments]

    unreviewed = 0
    if change_ids:
        unreviewed = (
            session.query(func.count(func.distinct(Claim.change_id)))
            .select_from(Escalation)
            .join(Claim, Escalation.claim_id == Claim.id)
            .filter(Escalation.company_id == company_id)
            .filter(Claim.company_id == company_id)
            .filter(Claim.change_id.in_(change_ids))
            .filter(Escalation.resolved_at.is_(None))
            .scalar()
            or 0
        )

    threads = threads_for_project(session, company_id, project_id)
    runs = scheduled_runs_for_project(session, company_id, project_id)

    upcoming = [run.next_run_at for run in runs if run.enabled]
    fired = [run.last_run_at for run in runs if run.last_run_at is not None]

    moments = [project.updated_at]
    moments.extend(row.attached_at for row in attachments)
    moments.extend(thread.last_activity_at for thread in threads)
    moments.extend(fired)

    return ProjectCard(
        project_id=project.id,
        name=project.name,
        jurisdiction=project.jurisdiction,
        docket_ref=project.docket_ref,
        status=project.status,
        change_count=len(attachments),
        unreviewed_count=int(unreviewed),
        open_thread_count=sum(1 for t in threads if t.status == "open"),
        next_run_at=min(upcoming) if upcoming else None,
        last_activity_at=max(moments),
    )


def project_cards_for_company(session: Session, company_id: str) -> list[ProjectCard]:
    """Every project as a tile. The list screen from the demo, in one call."""
    _require_scope(company_id)
    cards = [
        project_card(session, company_id, project.id)
        for project in projects_for_company(session, company_id)
    ]
    return [card for card in cards if card is not None]


__all__ = [
    "ProjectCard",
    "add_knowledge",
    "add_step",
    "add_turn",
    "attach_change",
    "attachments_for_project",
    "changes_for_project",
    "create_project",
    "create_work_plan",
    "due_runs",
    "knowledge_for_company",
    "knowledge_for_project",
    "knowledge_history",
    "knowledge_item_for_company",
    "open_thread",
    "project_card",
    "project_cards_for_company",
    "project_for_company",
    "projects_for_change",
    "projects_for_company",
    "record_run",
    "schedule_run",
    "scheduled_runs_for_project",
    "set_step_state",
    "set_thread_status",
    "steps_for_plan",
    "supersede_knowledge",
    "thread_for_company",
    "threads_for_project",
    "turns_for_thread",
    "work_plans_for_project",
]
