"""The project surface: the list of live work, and one project in full.

This is the spine the demo describes -- a project list where each project
carries a running list of regulatory changes, plus its threads, its plan, its
re-runs and the knowledge it has accumulated. Everything here reads through the
tenant-scoped functions in app/state/projects.py and app/state/review.py, and
the only write is opening a project, which those functions audit.

Four rules this module holds itself to.

ONE ROUTE OWNS ONE SCREEN. The list is served at both "/" and "/projects". The
first is where a reviewer lands; the second is the canonical path the nav points
at, so base.html never has to guess. Whoever mounts the routers decides which
router wins "/" -- this one answers it, and answers /projects either way.

NO ORM ROW REACHES A TEMPLATE. Each section builds frozen view rows first. That
is not tidiness: the knowledge section has to show live items and count the
superseded ones behind them, and the only safe way to count a row without
showing it is to never hand the row over. tests/test_project_views.py asserts a
superseded body is absent from the response.

A CITATION IS RE-CHECKED, NEVER REMEMBERED. A turn that cites a claim shows the
citation only when that claim's citation verifies against the stored source on
this request, through verified_claims(). When it does not, the turn keeps its
own words and says plainly that the claim behind it is held for review. No
statement of a withheld claim is read here, and none could be: WithheldClaim
has no statement field.

EVERY COUNT SAYS WHAT IT LEFT OUT. The page opens with the coverage strip from
app/state/review.py -- how many findings this project rests on and how many were
withheld -- because a project page is a surface that counts, and a count that
hides its exclusions is the failure this product exists to prevent.

There is no sign-in (docs/security.html says so plainly), so the owner typed
into the new-project form is the name recorded in the audit chain. The form says
so rather than implying the name was authenticated.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from app.state.claims import (
    claims_for_change,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import session_scope
from app.state.models import PROJECT_STATUSES
from app.state.projects import (
    changes_for_project,
    create_project,
    knowledge_for_company,
    knowledge_for_project,
    project_card,
    project_cards_for_company,
    project_for_company,
    scheduled_runs_for_project,
    steps_for_plan,
    threads_for_project,
    turns_for_thread,
    work_plans_for_project,
)
from app.state.review import coverage_for_project
from app.web.deps import company_name, current_company
from app.web.templating import build_templates

# Imported, not restated. The three states an empty screen can be in, the two
# commands that fix the last one, and the claim-to-change map are decided in one
# place; a second copy here would drift from the screens that share them.
from app.web.views.proceedings import (
    MAKE_COMMAND,
    SEED_COMMAND,
    STATE_NO_ROWS,
    STATE_NO_TABLES,
    STATE_OK,
    claim_owners,
)

router = APIRouter()
templates = build_templates()

LIST_URL = "/projects"
NEW_URL = "/projects/new"

# Both facts get the same answer and the same body. A 403 for another tenant's
# project and a 404 for one that never existed would let anyone enumerate which
# project ids exist somewhere in the system.
NOT_FOUND = "no project with that id for this company"

NAME_REQUIRED = "A project needs a name. Nothing was created."
JURISDICTION_REQUIRED = (
    "A project needs a jurisdiction. It decides what binds the work, so it is "
    "not something to fill in later. Nothing was created."
)
OWNER_REQUIRED = (
    "A project needs an owner. There is no sign-in here, so the name typed is "
    "the name the audit chain records. Nothing was created."
)

_STATUS_WORD = {"active": "Active", "monitoring": "Monitoring", "closed": "Closed"}
_CHANGE_BADGE = {"DRAFT": "badge--draft", "FINAL": "badge--final"}

# What a change's materiality reads as when no model has judged it. This build
# makes no model call, so every change carries NULL and the screen says which of
# the two facts that is rather than printing a blank cell.
MATERIALITY_UNASSESSED = "not assessed"


# ---------------------------------------------------------------------------
# What the templates are handed. Plain frozen rows, no ORM objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectTile:
    """One card on the grid. Every number is derived by project_card()."""

    project_id: str
    url: str
    name: str
    jurisdiction: str
    docket_ref: str | None
    status: str
    status_word: str
    badge: str
    change_count: int
    unreviewed_count: int
    open_thread_count: int
    next_run: str | None
    last_activity: str


@dataclass(frozen=True, slots=True)
class ChangeRow:
    change_id: str
    url: str
    change_type: str
    section: str
    materiality: str
    status: str
    badge: str
    claims: int
    held: int


@dataclass(frozen=True, slots=True)
class TurnCite:
    """What a turn's claim_id resolves to on this request.

    `verifies` decides which of two things the template renders, and the two are
    built from different fields. A verified citation carries a coordinate, the
    source's own bytes and a link to the claim. One that does not verify carries
    the reason and a link to the review queue -- no quote, because a failed
    quotation shown away from the source it failed against is the mistake this
    product is built to refuse.
    """

    claim_id: str
    verifies: bool
    coordinate: str
    source_text: str
    url: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class TurnLine:
    author_kind: str
    who: str
    when: str
    body: str
    cite: TurnCite | None


@dataclass(frozen=True, slots=True)
class ThreadPanel:
    thread_id: str
    question: str
    status: str
    is_open: bool
    opened_by: str
    opened_at: str
    last_activity: str
    turns: tuple[TurnLine, ...]


@dataclass(frozen=True, slots=True)
class StepRow:
    step_id: str
    ordinal: int
    description: str
    owner: str
    state: str
    due: str | None
    change_id: str | None
    change_url: str | None
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class PlanPanel:
    plan_id: str
    title: str
    created: str
    due: str | None
    steps: tuple[StepRow, ...]


@dataclass(frozen=True, slots=True)
class RunRow:
    run_id: str
    cadence: str
    enabled: bool
    overdue: bool
    last_run: str | None
    last_result: str | None
    next_run: str


@dataclass(frozen=True, slots=True)
class KnowledgeRow:
    item_id: str
    kind: str
    title: str
    body: str
    confirmed_by: str | None
    created: str
    source_claim_id: str | None


@dataclass(frozen=True, slots=True)
class Section:
    """One entry in the section index at the top of a project."""

    anchor: str
    label: str
    count: int
    waiting: int


@dataclass(frozen=True, slots=True)
class FormState:
    """The new-project form, and why it came back if it did."""

    name: str
    jurisdiction: str
    owner: str
    docket_ref: str
    summary: str
    error: str | None


# ---------------------------------------------------------------------------
# Small helpers. No request, no session.
# ---------------------------------------------------------------------------


def project_url(project_id: str) -> str:
    return f"/projects/{project_id}"


def change_url(change_id: str) -> str:
    return f"/changes/{change_id}"


def claim_anchor(claim_id: str) -> str:
    """The anchor the change screen gives a claim. Imported by nobody, matched by URL."""
    return f"claim-{claim_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _status_badge(status: str) -> str:
    """The badge class for a project status, or "" for a word we were not taught.

    An unknown status renders as a plain badge carrying its own text. It must not
    borrow the look of a closed project.
    """
    return f"badge--{status}" if status in PROJECT_STATUSES else ""


def withheld_share(total: int, withheld: int) -> int:
    """The percentage of the coverage bar the withheld portion takes.

    Zero findings gives 100, not 0. A strip that reports "nothing withheld" when
    it was told nothing is the lie the component exists to stop, and the
    stylesheet defaults the same way for the same reason.
    """
    if total <= 0:
        return 100
    return round(100 * withheld / total)


# ---------------------------------------------------------------------------
# Section builders. Each takes an open session and a scoped project id.
# ---------------------------------------------------------------------------


def _tile(card) -> ProjectTile:
    return ProjectTile(
        project_id=card.project_id,
        url=project_url(card.project_id),
        name=card.name,
        jurisdiction=card.jurisdiction,
        docket_ref=card.docket_ref,
        status=card.status,
        status_word=_STATUS_WORD.get(card.status, card.status),
        badge=_status_badge(card.status),
        change_count=card.change_count,
        unreviewed_count=card.unreviewed_count,
        open_thread_count=card.open_thread_count,
        next_run=_day(card.next_run_at),
        last_activity=_day(card.last_activity_at),
    )


def _change_rows(session, company_id: str, project_id: str) -> list[ChangeRow]:
    """The changes attached to this project, each with its unresolved refusals.

    The held count is read from the open escalations rather than recomputed, so
    this list and the review queue cannot disagree about how many refusals are
    outstanding. One claims query per change is an N+1 and it is the same
    deliberate one app/web/views/proceedings.py documents: claims_for_change()
    is the audited chokepoint, and a faster unscoped query written here would be
    a second place tenancy could be got wrong.
    """
    held_claim_ids = {
        row.claim_id
        for row in escalations_for_company(session, company_id, unresolved_only=True)
    }

    rows: list[ChangeRow] = []
    for change in changes_for_project(session, company_id, project_id):
        claims = claims_for_change(session, company_id, change.id)
        rows.append(
            ChangeRow(
                change_id=change.id,
                url=change_url(change.id),
                change_type=change.change_type,
                section=change.section or "not numbered",
                materiality=change.materiality or MATERIALITY_UNASSESSED,
                status=change.status,
                badge=_CHANGE_BADGE.get(change.status, ""),
                claims=len(claims),
                held=sum(1 for claim in claims if claim.id in held_claim_ids),
            )
        )
    return rows


def _cited_claims(session, company_id: str, claim_ids: set[str]) -> tuple[dict, dict]:
    """Re-verify every claim a turn points at, and hand back both verdicts.

    Returns two maps keyed by claim id: the claims that verify now, and the
    claims the product refuses. A claim in neither map cannot be read under this
    company's scope at all, and the caller says so rather than dropping the
    citation, which would leave a turn looking like a sourced finding.
    """
    if not claim_ids:
        return {}, {}

    owners: dict[str, str] = {}
    for proceeding in proceedings_for_company(session, company_id):
        owners.update(claim_owners(session, company_id, proceeding.id))

    verified: dict = {}
    withheld: dict = {}
    for change_id in sorted(
        {owners[claim_id] for claim_id in claim_ids if claim_id in owners}
    ):
        now_verified, now_withheld = verified_claims(session, company_id, change_id)
        verified.update({claim.claim_id: (claim, change_id) for claim in now_verified})
        withheld.update({claim.claim_id: (claim, change_id) for claim in now_withheld})
    return verified, withheld


def _cite(claim_id: str, verified: dict, withheld: dict) -> TurnCite:
    """Three outcomes, three different sentences. None of them shows a statement."""
    if claim_id in verified:
        claim, change_id = verified[claim_id]
        return TurnCite(
            claim_id=claim_id,
            verifies=True,
            coordinate=(
                f"{claim.citation_version_id} "
                f"{claim.citation_start}-{claim.citation_end}"
            ),
            source_text=claim.actual_text,
            url=f"{change_url(change_id)}#{claim_anchor(claim_id)}",
            reason="",
        )

    if claim_id in withheld:
        claim, _change_id = withheld[claim_id]
        return TurnCite(
            claim_id=claim_id,
            verifies=False,
            coordinate=(
                f"{claim.citation_version_id} "
                f"{claim.citation_start}-{claim.citation_end}"
            ),
            source_text="",
            url="/review",
            reason=claim.reason_text,
        )

    return TurnCite(
        claim_id=claim_id,
        verifies=False,
        coordinate="",
        source_text="",
        url=None,
        reason="The claim behind this turn cannot be read for this company.",
    )


def _thread_panels(session, company_id: str, project_id: str) -> list[ThreadPanel]:
    threads = threads_for_project(session, company_id, project_id)
    if not threads:
        return []

    turns = {
        thread.id: turns_for_thread(session, company_id, thread.id)
        for thread in threads
    }
    cited = {
        turn.claim_id
        for rows in turns.values()
        for turn in rows
        if turn.claim_id is not None
    }
    verified, withheld = _cited_claims(session, company_id, cited)

    panels: list[ThreadPanel] = []
    for thread in threads:
        # An analyst turn is attributed to "Analyst" and not to whoever opened
        # the thread. ResearchTurn records the kind of author, not the person,
        # and putting the opener's name on a line they may not have written
        # would be the interface asserting something the row does not say.
        lines = tuple(
            TurnLine(
                author_kind=turn.author_kind,
                who="Strata" if turn.author_kind == "system" else "Analyst",
                when=_stamp(turn.created_at) or "",
                body=turn.body,
                cite=(
                    _cite(turn.claim_id, verified, withheld)
                    if turn.claim_id is not None
                    else None
                ),
            )
            for turn in turns[thread.id]
        )
        panels.append(
            ThreadPanel(
                thread_id=thread.id,
                question=thread.question,
                status=thread.status,
                is_open=thread.status == "open",
                opened_by=thread.opened_by,
                opened_at=_day(thread.opened_at) or "",
                last_activity=_day(thread.last_activity_at) or "",
                turns=lines,
            )
        )
    return panels


def _blocked_reason(session, company_id: str, change_id: str | None) -> str | None:
    """Why a blocked step is blocked, when the answer is a failed citation.

    None where the plan records the step as blocked and nothing in the data says
    why. That is the honest answer: inventing a cause for a state somebody set by
    hand would be the product speaking where it has not checked.
    """
    if change_id is None:
        return None
    _verified, withheld = verified_claims(session, company_id, change_id)
    if not withheld:
        return None
    first = withheld[0]
    return (
        f"A claim behind {change_id} did not verify: {first.reason_text} "
        "The step cannot proceed on it until a person resolves the refusal."
    )


def _plan_panels(session, company_id: str, project_id: str) -> list[PlanPanel]:
    panels: list[PlanPanel] = []
    for plan in work_plans_for_project(session, company_id, project_id):
        steps = tuple(
            StepRow(
                step_id=step.id,
                ordinal=step.ordinal,
                description=step.description,
                owner=step.owner,
                state=step.state,
                due=_day(step.due_at),
                change_id=step.change_id,
                change_url=change_url(step.change_id) if step.change_id else None,
                blocked_reason=(
                    _blocked_reason(session, company_id, step.change_id)
                    if step.state == "blocked"
                    else None
                ),
            )
            for step in steps_for_plan(session, company_id, plan.id)
        )
        panels.append(
            PlanPanel(
                plan_id=plan.id,
                title=plan.title,
                created=_day(plan.created_at) or "",
                due=_day(plan.due_at),
                steps=steps,
            )
        )
    return panels


def _run_rows(session, company_id: str, project_id: str) -> list[RunRow]:
    """Every schedule on the project, with whether it is late.

    last_run is None where a schedule has never fired, and the template says
    "never run" rather than leaving the cell blank. A schedule that has never
    run and one that ran an hour ago must not read the same, or a broken
    scheduler looks like a quiet week.
    """
    now = _now()
    return [
        RunRow(
            run_id=run.id,
            cadence=run.cadence,
            enabled=run.enabled,
            overdue=run.enabled and run.next_run_at <= now,
            last_run=_stamp(run.last_run_at),
            last_result=run.last_result,
            next_run=_stamp(run.next_run_at) or "",
        )
        for run in scheduled_runs_for_project(session, company_id, project_id)
    ]


def _knowledge(
    session, company_id: str, project_id: str
) -> tuple[list[KnowledgeRow], int]:
    """The live items newest first, and how many superseded rows sit behind them.

    The count is taken from rows this function reads and never returns. A
    superseded body is history, readable by id, and it does not belong on a
    screen that shows what the company believes now -- so nothing carrying one
    is handed to the template.
    """
    live = list(reversed(knowledge_for_project(session, company_id, project_id)))
    superseded = sum(
        1
        for item in knowledge_for_company(session, company_id, include_superseded=True)
        if item.project_id == project_id and item.superseded_by is not None
    )

    rows = [
        KnowledgeRow(
            item_id=item.id,
            kind=item.kind,
            title=item.kind.capitalize(),
            body=item.body,
            confirmed_by=item.confirmed_by,
            created=_day(item.created_at) or "",
            source_claim_id=item.source_claim_id,
        )
        for item in live
    ]
    return rows, superseded


# ---------------------------------------------------------------------------
# The page shell
# ---------------------------------------------------------------------------


def _chrome(company_id: str, *, page_title: str, review_count: int) -> dict:
    """The context base.html reads, built once so every screen here agrees.

    review_count is passed in rather than read here. This function has no
    session on purpose: it is also what the no-tables branch renders, and a
    shell that needed a working database to draw itself could not report that
    the database is not there.
    """
    name = company_name(company_id)
    return {
        "page_title": page_title,
        "company_name": name,
        # Dropped when the name is only the id echoed back, rather than printing
        # "MEP MEP" in the masthead.
        "company_id": company_id if name != company_id else None,
        "tenant": company_id,
        "nav_active": "projects",
        "nav_projects_url": LIST_URL,
        "review_count": review_count,
        "seed_command": SEED_COMMAND,
        "make_command": MAKE_COMMAND,
    }


def _waiting(session, company_id: str) -> int:
    """How many refusals are outstanding, for the count on the nav."""
    return len(escalations_for_company(session, company_id, unresolved_only=True))


def _list_page(
    request: Request, company_id: str, *, form: FormState | None
) -> HTMLResponse:
    """The card grid, with the new-project form above it when one is in hand.

    An unseeded clone has no tables. That is a different fact from a company
    with no projects, and the two get different sentences: one says how to load
    the corpus, the other says the grid is empty because nobody has opened a
    project yet.
    """
    try:
        with session_scope() as session:
            tiles = [
                _tile(card) for card in project_cards_for_company(session, company_id)
            ]
            waiting = _waiting(session, company_id)
        state = STATE_OK if tiles else STATE_NO_ROWS
    except SQLAlchemyError:
        tiles, waiting, state = [], 0, STATE_NO_TABLES

    context = _chrome(company_id, page_title="Projects", review_count=waiting)
    context.update(
        {
            "tiles": tiles,
            "state": state,
            "form": form,
            "new_url": NEW_URL,
            "post_url": LIST_URL,
        }
    )
    status_code = 400 if form is not None and form.error else 200
    return templates.TemplateResponse(
        request, "project_list.html", context, status_code=status_code
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def projects_index(
    request: Request, company_id: str = Depends(current_company)
) -> HTMLResponse:
    """Where a reviewer lands: every live project as a card."""
    return _list_page(request, company_id, form=None)


@router.get(LIST_URL, response_class=HTMLResponse)
def projects_list(
    request: Request, company_id: str = Depends(current_company)
) -> HTMLResponse:
    """The canonical path for the same screen. base.html's nav points here."""
    return _list_page(request, company_id, form=None)


@router.get(NEW_URL, response_class=HTMLResponse)
def new_project_form(
    request: Request, company_id: str = Depends(current_company)
) -> HTMLResponse:
    """The form, above the grid the analyst is adding to.

    Declared before /projects/{project_id} on purpose: FastAPI matches in
    declaration order, and the other way round "new" would be read as a project
    id and answered with a 404.
    """
    return _list_page(
        request,
        company_id,
        form=FormState(
            name="", jurisdiction="", owner="", docket_ref="", summary="", error=None
        ),
    )


@router.post(LIST_URL)
def create_project_post(
    request: Request,
    company_id: str = Depends(current_company),
    name: str = Form(default=""),
    jurisdiction: str = Form(default=""),
    owner: str = Form(default=""),
    docket_ref: str = Form(default=""),
    summary: str = Form(default=""),
):
    """Open a project, or say why it was refused and give the form back.

    A refusal redisplays what was typed with the reason above it. Nothing is
    written, and the analyst does not lose the summary they just wrote. The
    alternative -- a 500, or a redirect to an empty form -- teaches people to
    keep a copy of everything in a text file, which is where the product loses.

    create_project() checks the same three fields and raises. The check here is
    not a duplicate of that guard so much as the place its answer is turned into
    a sentence; the ValueError is still caught, because a rule enforced in one
    place must not be able to reach a user as a stack trace from the other.
    """
    fields = {
        "name": " ".join((name or "").split()),
        "jurisdiction": " ".join((jurisdiction or "").split()),
        "owner": " ".join((owner or "").split()),
        "docket_ref": " ".join((docket_ref or "").split()),
        "summary": (summary or "").strip(),
    }

    error = None
    if not fields["name"]:
        error = NAME_REQUIRED
    elif not fields["jurisdiction"]:
        error = JURISDICTION_REQUIRED
    elif not fields["owner"]:
        error = OWNER_REQUIRED

    if error is None:
        try:
            with session_scope() as session:
                project = create_project(
                    session,
                    company_id,
                    name=fields["name"],
                    jurisdiction=fields["jurisdiction"],
                    owner=fields["owner"],
                    docket_ref=fields["docket_ref"] or None,
                    summary=fields["summary"],
                )
                project_id = project.id
        except ValueError as refusal:
            error = f"{refusal} Nothing was created."
        else:
            # 303, so a reload of the project does not repost the form.
            return RedirectResponse(url=project_url(project_id), status_code=303)

    return _list_page(request, company_id, form=FormState(**fields, error=error))


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(
    request: Request,
    project_id: str,
    company_id: str = Depends(current_company),
) -> HTMLResponse:
    """One project: its changes, threads, plan, schedules and knowledge.

    Another company's project id is a 404 with the same body an unknown id gets.
    The row never reaches this function -- project_for_company() resolves it to
    None -- so there is nothing here that could leak it.
    """
    try:
        with session_scope() as session:
            project = project_for_company(session, company_id, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail=NOT_FOUND)

            card = project_card(session, company_id, project_id)
            coverage = coverage_for_project(session, company_id, project_id)

            changes = _change_rows(session, company_id, project_id)
            threads = _thread_panels(session, company_id, project_id)
            plans = _plan_panels(session, company_id, project_id)
            runs = _run_rows(session, company_id, project_id)
            knowledge, superseded = _knowledge(session, company_id, project_id)

            context = _chrome(
                company_id,
                page_title=project.name,
                review_count=_waiting(session, company_id),
            )
            detail = {
                "id": project.id,
                "name": project.name,
                "docket_ref": project.docket_ref,
                "jurisdiction": project.jurisdiction,
                "status": project.status,
                "status_word": _STATUS_WORD.get(project.status, project.status),
                "badge": _status_badge(project.status),
                "owner": project.owner,
                "summary": project.summary,
                "created": _day(project.created_at),
                "updated": _day(project.updated_at),
            }
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None

    open_threads = sum(1 for thread in threads if thread.is_open)
    blocked_steps = sum(
        1 for plan in plans for step in plan.steps if step.state == "blocked"
    )
    sections = [
        Section("changes", "Changes", len(changes), card.unreviewed_count),
        Section("threads", "Threads", len(threads), open_threads),
        Section("plans", "Work plan", sum(len(plan.steps) for plan in plans), blocked_steps),
        Section("runs", "Re-runs", len(runs), sum(1 for run in runs if run.overdue)),
        Section("knowledge", "Knowledge", len(knowledge), 0),
    ]

    context.update(
        {
            "project": detail,
            "card": _tile(card),
            "coverage": coverage,
            "withheld_share": withheld_share(
                coverage.findings_total, coverage.findings_withheld
            ),
            "sections": sections,
            "changes": changes,
            "threads": threads,
            "plans": plans,
            "runs": runs,
            "knowledge": knowledge,
            "superseded_count": superseded,
            "materiality_unassessed": MATERIALITY_UNASSESSED,
        }
    )
    return templates.TemplateResponse(request, "project_detail.html", context)


__all__ = [
    "JURISDICTION_REQUIRED",
    "LIST_URL",
    "MATERIALITY_UNASSESSED",
    "NAME_REQUIRED",
    "NEW_URL",
    "NOT_FOUND",
    "OWNER_REQUIRED",
    "ChangeRow",
    "FormState",
    "KnowledgeRow",
    "PlanPanel",
    "ProjectTile",
    "RunRow",
    "Section",
    "StepRow",
    "ThreadPanel",
    "TurnCite",
    "TurnLine",
    "change_url",
    "claim_anchor",
    "project_url",
    "router",
    "templates",
    "withheld_share",
]
