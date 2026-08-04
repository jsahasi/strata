"""The review centre: everything a project rests on, and everything it does not.

This is the screen the demo notes describe -- sources, analytics, expert review,
findings, questions, the collective take, deliverables and steer -- built on the
one rule that makes breadth safe:

    EVERY SURFACE THAT SYNTHESISES OR COUNTS DISPLAYS ITS OWN COVERAGE.

The coverage strip leads the page. It is not a summary badge at the bottom, it
is the first thing read, and it carries the withheld number at the same weight
as the included one. A take, a deliverable and the page itself each state how
many findings they rest on and how many were left out. None of them can omit
the second number: app/state/review.py refuses to compose a take that hides its
exclusions, and app/web/static/strata.css draws an alarm outline around a
deliverable rendered without a coverage strip.

WHAT A WITHHELD THING MAY SAY, WHICH IS NOTHING.

A claim whose citation fails verification never reaches a template with its
statement. That guarantee is structural, not editorial: app.state.claims builds
WithheldClaim, which has no statement field and no __dict__ to attach one to.
This module applies the same construction one level up. A finding is rendered
as a VerifiedFinding, which carries its headline, or as an UnsubstantiatedFinding,
which has no headline field and no detail field. The template cannot print what
the object does not have, so no stylesheet change, no stray `{{ row.headline }}`
and no helpful refactor can put an unsubstantiated assertion on the page.

Which of the two a finding becomes is decided by exactly the test
coverage_for_project() counts with -- the claim id appearing in the verified set
returned by verified_claims(), re-checked against the stored source on this
request. The number at the top of the page and the rows further down cannot
disagree, because one test produces both.

ANALYTICS ARE COUNTS, AND EACH ONE NAMES ITS DENOMINATOR.

No trend lines and no percentages. A share computed over eight findings is
false precision dressed as insight; the raw counts are shorter to read and
cannot mislead. The one ratio on the page is the coverage bar, which is drawn
rather than asserted and is the design system's own component.

WHAT IS IMPORTED RATHER THAN REBUILT.

The escalation queue is app/web/views/review.py. The review centre embeds that
module's page builder instead of writing a second one. A second implementation
of "what did this product refuse to assert, and what does the source really say
at those offsets" would be a second answer to the same question, and on the day
the two disagreed there would be no way to tell which screen was lying. Same
argument as app/state/claims.py importing _require_scope rather than restating
it. The names are private to that module and imported here deliberately; this
module is a composition of that screen, not a rival to it.

ROUTING NOTE for whoever assembles the application. This router serves GET
/review and GET /review/{project_id}. app/web/views/review.py serves GET /review
as the narrow queue. Mount this router first: the review centre contains the
queue as its expert-review section, so it supersedes the narrow screen where
both are present. The two POST paths here are distinct from that module's, so
the routers can coexist without either shadowing a write.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app.state.audit import record_event
from app.state.claims import verified_claims
from app.state.db import session_scope
from app.state.models import (
    DELIVERABLE_STATES,
    FINDING_STATUSES,
    Project,
)
from app.state.projects import changes_for_project, project_for_company, projects_for_company
from app.state.review import (
    KIND_EXTERNAL,
    KIND_INTERNAL,
    Coverage,
    collective_take_for_project,
    coverage_for_project,
    deliverables_for_project,
    findings_for_project,
    issue_steer,
    questions_for_project,
    sources_for_project,
    steer_directives_for_project,
)

# Private, imported on purpose. This is the same claim-to-change resolution
# coverage_for_project() uses. Writing a second one here would let the number in
# the coverage strip and the rows underneath it disagree about which findings
# are substantiated, which is precisely the failure this page exists to expose.
from app.state.review import _claims_by_id
from app.web import TEMPLATES_DIR
from app.web.views.proceedings import (
    STATE_NO_ROWS,
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    current_company_id,
    unresolved_escalations,
)

# The escalation queue, embedded rather than rebuilt. See the module docstring.
from app.web.views.review import (
    ACTION_APPROVED,
    ACTION_REJECTED,
    ALREADY_RESOLVED,
    DECISION_APPROVE,
    DECISION_REJECT,
    DECISIONS,
    REASON_REQUIRED,
    QueueItem,
    _actor,
    _citation_of,
    _page,
    _stamp,
    _target,
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

CENTRE_URL = "/review"

# Reasons a finding is not evidence, in the product's own vocabulary. The first
# two are this module's; the third defers to whatever the verifier said, because
# a second wording for one refusal is a second thing for an analyst to learn.
REASON_CODE_UNCITED = "FINDING_UNCITED"
REASON_TEXT_UNCITED = (
    "raised from a reading, with no claim attached, so nothing verifies it"
)
REASON_CODE_CLAIM_UNREADABLE = "FINDING_CLAIM_UNREADABLE"
REASON_TEXT_CLAIM_UNREADABLE = (
    "the claim this finding names cannot be read under this company's scope"
)

STEER_EMPTY = "a steer needs an instruction; an empty one steers nothing"


# ---------------------------------------------------------------------------
# What the templates see. Plain frozen data, never an ORM row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A project as a link and a label. Enough for a picker, no more."""

    project_id: str
    name: str
    url: str
    status: str
    jurisdiction: str
    docket_ref: str | None


@dataclass(frozen=True, slots=True)
class CoverageView:
    """The coverage strip's data, including the share the bar is drawn at.

    withheld_share is a CSS length for --withheld-share and is never printed as
    text. Drawing a ratio and asserting a percentage are different acts: the bar
    shows a reader the proportion at a glance without claiming a precision the
    counts do not support.

    A view with no findings at all is `empty`, and the strip still renders. A
    coverage strip that disappears when it is convenient is worth nothing.
    """

    scope: str
    included: int
    withheld: int
    total: int
    withheld_share: str
    claims_verified: int
    claims_withheld: int
    open_questions: int
    blocking_questions: int
    sources_internal: int
    sources_external: int
    empty: bool


@dataclass(frozen=True, slots=True)
class SourceRow:
    source_id: str
    project_id: str
    project_name: str
    kind: str
    label: str
    locator: str
    version_id: str | None
    retrieved_at: str | None
    trusted: bool


@dataclass(frozen=True, slots=True)
class VerifiedFinding:
    """A finding whose claim's citation held when this page was built."""

    finding_id: str
    project_id: str
    project_name: str
    status: str
    claim_id: str
    change_id: str | None
    change_url: str | None
    citation: str
    headline: str
    detail: str
    raised_by: str
    raised_at: str | None


@dataclass(frozen=True, slots=True)
class UnsubstantiatedFinding:
    """A finding nothing verifies. It cannot carry what it would have said.

    There is no headline field and no detail field, and slots=True means neither
    can be attached at runtime. This is the WithheldClaim design applied to the
    layer above: a finding resting on a failed citation is an assertion the
    product has not earned, and a greyed-out assertion is still an assertion.
    What the reader gets instead is the reason, the claim it named, and the
    citation address to go and check.
    """

    finding_id: str
    project_id: str
    project_name: str
    status: str
    claim_id: str | None
    change_id: str | None
    change_url: str | None
    citation: str
    reason_code: str
    reason_text: str
    raised_by: str
    raised_at: str | None


@dataclass(frozen=True, slots=True)
class QuestionRow:
    question_id: str
    project_id: str
    project_name: str
    body: str
    asked_by: str
    asked_at: str | None
    blocking: bool
    answered: bool
    answer: str | None
    answered_by: str | None
    answered_at: str | None


@dataclass(frozen=True, slots=True)
class TakeCard:
    """A composed synthesis and the coverage it was composed against.

    included and withheld are the row's own stored counts, written at compose
    time. They are shown rather than recomputed, because the take is a statement
    about what was known then. `moved` says the live coverage has since changed,
    which is a fact about the take's age and not a correction to it.
    """

    project_id: str
    project_name: str
    url: str
    body: str
    composed_by: str
    composed_at: str | None
    included: int
    withheld: int
    withheld_share: str
    moved: bool
    live_included: int
    live_withheld: int


@dataclass(frozen=True, slots=True)
class DeliverableRow:
    deliverable_id: str
    project_id: str
    project_name: str
    title: str
    kind: str
    state: str
    state_class: str
    body: str
    created_at: str | None
    approved_by: str | None
    included: int
    withheld: int
    withheld_share: str


@dataclass(frozen=True, slots=True)
class SteerRow:
    directive_id: str
    project_id: str
    project_name: str
    instruction: str
    issued_by: str
    issued_at: str | None
    applied_at: str | None
    effect: str | None
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class Count:
    label: str
    n: int


@dataclass(frozen=True, slots=True)
class AnalyticsGroup:
    """One count and the denominator it was taken over.

    basis is not decoration. "3 open" means nothing without "of 3 questions on
    these projects", and a panel of bare numbers invites the reader to supply a
    denominator of their own.
    """

    key: str
    title: str
    basis: str
    rows: tuple[Count, ...]


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _share(withheld: int, total: int) -> str:
    """The bar's width. No findings means the whole bar is withheld.

    Defaulting to 100% rather than 0% matches the stylesheet's own default and
    its reasoning: a strip told nothing about what verified must not answer
    "nothing was withheld". Absence is denial here as well.
    """
    if total <= 0:
        return "100%"
    return f"{round(withheld * 100 / total)}%"


def _citation_of_claim(claim) -> str:
    if claim is None:
        return "not readable"
    return (
        f"{claim.citation_version_id}:{claim.citation_start}:{claim.citation_end}"
    )


def _ref(project: Project) -> ProjectRef:
    return ProjectRef(
        project_id=project.id,
        name=project.name,
        url=f"{CENTRE_URL}/{project.id}",
        status=project.status,
        jurisdiction=project.jurisdiction,
        docket_ref=project.docket_ref,
    )


def _sum(coverages: list[Coverage]) -> Coverage:
    """Add per-project coverage into one.

    Findings, questions and sources each belong to exactly one project, so those
    totals are exact. Claim counts are summed per project, which means a change
    reached from two projects is counted on both; the page says so rather than
    presenting the sum as a distinct count.
    """
    return Coverage(
        findings_total=sum(c.findings_total for c in coverages),
        findings_verified=sum(c.findings_verified for c in coverages),
        findings_withheld=sum(c.findings_withheld for c in coverages),
        claims_verified=sum(c.claims_verified for c in coverages),
        claims_withheld=sum(c.claims_withheld for c in coverages),
        open_questions=sum(c.open_questions for c in coverages),
        blocking_questions=sum(c.blocking_questions for c in coverages),
        sources_internal=sum(c.sources_internal for c in coverages),
        sources_external=sum(c.sources_external for c in coverages),
    )


def _view(coverage: Coverage, scope: str) -> CoverageView:
    return CoverageView(
        scope=scope,
        included=coverage.findings_verified,
        withheld=coverage.findings_withheld,
        total=coverage.findings_total,
        withheld_share=_share(coverage.findings_withheld, coverage.findings_total),
        claims_verified=coverage.claims_verified,
        claims_withheld=coverage.claims_withheld,
        open_questions=coverage.open_questions,
        blocking_questions=coverage.blocking_questions,
        sources_internal=coverage.sources_internal,
        sources_external=coverage.sources_external,
        empty=coverage.findings_total == 0,
    )


# ---------------------------------------------------------------------------
# Findings: the split that decides what may be printed
# ---------------------------------------------------------------------------


def _findings(
    session, company_id: str, project: Project
) -> tuple[list[VerifiedFinding], list[UnsubstantiatedFinding], list[str]]:
    """Split one project's findings, and report the changes they reach.

    The change set is built exactly as coverage_for_project() builds it -- from
    each finding's own change and from the change behind its claim -- so the
    verification run here is the same run the counting used. The third return
    value is that change set, which the escalation queue and the analytics panel
    reuse rather than recomputing under a slightly different rule.
    """
    rows = findings_for_project(session, company_id, project.id)
    claims = _claims_by_id(
        session, company_id, {row.claim_id for row in rows if row.claim_id}
    )

    change_ids: list[str] = []
    for finding in rows:
        claim = claims.get(finding.claim_id) if finding.claim_id else None
        for change_id in (finding.change_id, claim.change_id if claim else None):
            if change_id and change_id not in change_ids:
                change_ids.append(change_id)

    verified_claims_by_id = {}
    withheld_claims_by_id = {}
    for change_id in change_ids:
        verified, withheld = verified_claims(session, company_id, change_id)
        verified_claims_by_id.update({claim.claim_id: claim for claim in verified})
        withheld_claims_by_id.update({claim.claim_id: claim for claim in withheld})

    good: list[VerifiedFinding] = []
    bad: list[UnsubstantiatedFinding] = []
    for finding in rows:
        change_url = f"/changes/{finding.change_id}" if finding.change_id else None
        stamp = _stamp(finding.raised_at)
        verified = verified_claims_by_id.get(finding.claim_id or "")

        if verified is not None:
            good.append(
                VerifiedFinding(
                    finding_id=finding.id,
                    project_id=project.id,
                    project_name=project.name,
                    status=finding.status,
                    claim_id=verified.claim_id,
                    change_id=finding.change_id,
                    change_url=change_url,
                    citation=_citation_of_claim(verified),
                    headline=finding.headline,
                    detail=finding.detail or "",
                    raised_by=finding.raised_by,
                    raised_at=stamp,
                )
            )
            continue

        held = withheld_claims_by_id.get(finding.claim_id or "")
        if finding.claim_id is None:
            code, text, citation = REASON_CODE_UNCITED, REASON_TEXT_UNCITED, "no citation"
        elif held is None:
            code, text = REASON_CODE_CLAIM_UNREADABLE, REASON_TEXT_CLAIM_UNREADABLE
            citation = "not readable"
        else:
            code, text = held.reason_code, held.reason_text
            citation = _citation_of_claim(held)

        bad.append(
            UnsubstantiatedFinding(
                finding_id=finding.id,
                project_id=project.id,
                project_name=project.name,
                status=finding.status,
                claim_id=finding.claim_id,
                change_id=finding.change_id,
                change_url=change_url,
                citation=citation,
                reason_code=code,
                reason_text=text,
                raised_by=finding.raised_by,
                raised_at=stamp,
            )
        )

    return good, bad, change_ids


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def _analytics(
    *,
    changes,
    verified_findings: list[VerifiedFinding],
    unsubstantiated: list[UnsubstantiatedFinding],
    coverage: Coverage,
    questions: list[QuestionRow],
    waiting: int,
    settled: int,
    projects: int,
) -> list[AnalyticsGroup]:
    """Counts, each with the denominator it was taken over. No percentages.

    A share over eight findings would be false precision, and a trend line over
    three weeks of a synthetic corpus would be worse: it would look like
    evidence. Raw counts are shorter to read and cannot mislead. This is a
    deliberate refusal, stated on the page as well as here.
    """
    by_type: dict[str, int] = {}
    for change in changes:
        by_type[change.change_type] = by_type.get(change.change_type, 0) + 1

    findings_total = len(verified_findings) + len(unsubstantiated)
    by_status: dict[str, int] = {status: 0 for status in FINDING_STATUSES}
    for row in verified_findings:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    for row in unsubstantiated:
        by_status[row.status] = by_status.get(row.status, 0) + 1

    answered = sum(1 for row in questions if row.answered)
    over = "project" if projects == 1 else "projects"

    return [
        AnalyticsGroup(
            key="changes",
            title="Changes by type",
            basis=f"{len(changes)} attached to these {projects} {over}",
            rows=tuple(
                Count(label=name, n=count) for name, count in sorted(by_type.items())
            ),
        ),
        AnalyticsGroup(
            key="findings",
            title="Findings by status",
            basis=f"{findings_total} raised, of which {len(unsubstantiated)} verify nothing",
            rows=tuple(
                Count(label=status, n=by_status.get(status, 0))
                for status in FINDING_STATUSES
            ),
        ),
        AnalyticsGroup(
            key="claims",
            title="Claims behind those findings",
            basis=(
                f"{coverage.claims_verified + coverage.claims_withheld} on the "
                "changes these findings reach, re-checked just now"
            ),
            rows=(
                Count(label="citation verifies", n=coverage.claims_verified),
                Count(label="withheld", n=coverage.claims_withheld),
            ),
        ),
        AnalyticsGroup(
            key="questions",
            title="Questions",
            basis=f"{len(questions)} asked on these {projects} {over}",
            rows=(
                Count(label="open", n=coverage.open_questions),
                Count(label="answered", n=answered),
                Count(label="blocking and open", n=coverage.blocking_questions),
            ),
        ),
        AnalyticsGroup(
            key="escalations",
            title="Escalations",
            basis=f"{waiting + settled} raised against these changes",
            rows=(
                Count(label="outstanding", n=waiting),
                Count(label="resolved", n=settled),
            ),
        ),
    ]


def _state_class(state: str) -> str:
    """The deliverable's state as a class name. Unknown states get none.

    An unrecognised state renders plain rather than borrowing the look of a
    final document, the same rule the version badges follow.
    """
    if state not in DELIVERABLE_STATES:
        return ""
    return f"deliverable--{state.replace('_', '-')}"


def _collect(session, company_id: str, projects: list[Project]) -> dict:
    """Build every section from the given projects. One tenant, one pass."""
    refs = [_ref(project) for project in projects]
    names = {project.id: project.name for project in projects}

    coverages: list[Coverage] = []
    sources: list[SourceRow] = []
    verified_findings: list[VerifiedFinding] = []
    unsubstantiated: list[UnsubstantiatedFinding] = []
    questions: list[QuestionRow] = []
    takes: list[TakeCard] = []
    deliverables: list[DeliverableRow] = []
    steers: list[SteerRow] = []
    # Keyed by change id, because a change attached to two projects is one
    # change. Appending to a list instead would count it twice and the analytics
    # panel would report more changes than the company has.
    changes: dict[str, object] = {}
    change_ids: set[str] = set()

    for project in projects:
        coverage = coverage_for_project(session, company_id, project.id)
        coverages.append(coverage)

        for row in sources_for_project(session, company_id, project.id):
            sources.append(
                SourceRow(
                    source_id=row.id,
                    project_id=project.id,
                    project_name=project.name,
                    kind=row.kind,
                    label=row.label,
                    locator=row.locator,
                    version_id=row.version_id,
                    retrieved_at=_stamp(row.retrieved_at),
                    trusted=bool(row.trusted),
                )
            )

        good, bad, reached = _findings(session, company_id, project)
        verified_findings.extend(good)
        unsubstantiated.extend(bad)
        change_ids.update(reached)

        for change in changes_for_project(session, company_id, project.id):
            change_ids.add(change.id)
            changes.setdefault(change.id, change)

        for row in questions_for_project(session, company_id, project.id):
            questions.append(
                QuestionRow(
                    question_id=row.id,
                    project_id=project.id,
                    project_name=project.name,
                    body=row.body,
                    asked_by=row.asked_by,
                    asked_at=_stamp(row.asked_at),
                    blocking=bool(row.blocking),
                    answered=row.answered_at is not None,
                    answer=row.answer,
                    answered_by=row.answered_by,
                    answered_at=_stamp(row.answered_at),
                )
            )

        take = collective_take_for_project(session, company_id, project.id)
        if take is not None:
            takes.append(
                TakeCard(
                    project_id=project.id,
                    project_name=project.name,
                    url=f"{CENTRE_URL}/{project.id}",
                    body=take.body,
                    composed_by=take.composed_by,
                    composed_at=_stamp(take.composed_at),
                    included=take.findings_included,
                    withheld=take.findings_withheld,
                    withheld_share=_share(
                        take.findings_withheld,
                        take.findings_included + take.findings_withheld,
                    ),
                    moved=(take.findings_included, take.findings_withheld)
                    != (coverage.findings_verified, coverage.findings_withheld),
                    live_included=coverage.findings_verified,
                    live_withheld=coverage.findings_withheld,
                )
            )

        for row in deliverables_for_project(session, company_id, project.id):
            deliverables.append(
                DeliverableRow(
                    deliverable_id=row.id,
                    project_id=project.id,
                    project_name=project.name,
                    title=row.title,
                    kind=row.kind,
                    state=row.state,
                    state_class=_state_class(row.state),
                    body=row.body or "",
                    created_at=_stamp(row.created_at),
                    approved_by=row.approved_by,
                    included=row.findings_included,
                    withheld=row.findings_withheld,
                    withheld_share=_share(
                        row.findings_withheld,
                        row.findings_included + row.findings_withheld,
                    ),
                )
            )

        for row in steer_directives_for_project(
            session, company_id, project.id, include_revoked=True
        ):
            steers.append(
                SteerRow(
                    directive_id=row.id,
                    project_id=project.id,
                    project_name=project.name,
                    instruction=row.instruction,
                    issued_by=row.issued_by,
                    issued_at=_stamp(row.issued_at),
                    applied_at=_stamp(row.applied_at),
                    effect=row.effect,
                    revoked_at=_stamp(row.revoked_at),
                )
            )

    # The escalation queue, built once for the company by the queue screen and
    # then narrowed to the changes these projects reach. Narrowing rather than
    # rebuilding keeps one answer to "what was refused".
    queue = _page(session, company_id)
    waiting = [item for item in queue.waiting if item.change_id in change_ids]
    settled = [item for item in queue.settled if item.change_id in change_ids]

    coverage = _sum(coverages)
    over = "project" if len(projects) == 1 else "projects"
    scope = (
        f"Built from {coverage.findings_total} findings across "
        f"{len(projects)} {over}."
    )

    open_questions = [row for row in questions if not row.answered]
    open_questions.sort(key=lambda row: (not row.blocking, row.question_id))

    return {
        "projects": refs,
        "project_names": names,
        "coverage": _view(coverage, scope),
        "sources": sources,
        "analytics": _analytics(
            changes=list(changes.values()),
            verified_findings=verified_findings,
            unsubstantiated=unsubstantiated,
            coverage=coverage,
            questions=questions,
            waiting=len(waiting),
            settled=len(settled),
            projects=len(projects),
        ),
        "waiting": waiting,
        "settled": settled,
        "verified_findings": verified_findings,
        "unsubstantiated": unsubstantiated,
        "questions_open": open_questions,
        "questions_answered": [row for row in questions if row.answered],
        "takes": takes,
        "deliverables": deliverables,
        "steers": [row for row in steers if row.revoked_at is None],
        "steers_revoked": [row for row in steers if row.revoked_at is not None],
    }


def _context(
    company_id: str,
    *,
    page_title: str,
    scope_project: ProjectRef | None,
    body: dict,
    state: str,
    waiting_count: int,
    error: dict | None = None,
) -> dict:
    back = scope_project.url if scope_project else CENTRE_URL
    context = chrome(
        company_id,
        page_title=page_title,
        nav_active="review",
        review_count=waiting_count,
    )
    context.update(body)
    context.update(
        {
            "state": state,
            "error": error,
            "scope_project": scope_project,
            "show_project": scope_project is None,
            "back": back,
            "centre_url": CENTRE_URL,
            "decision_approve": DECISION_APPROVE,
            "decision_reject": DECISION_REJECT,
            "kind_internal": KIND_INTERNAL,
            "kind_external": KIND_EXTERNAL,
        }
    )
    return context


def _blank() -> dict:
    """Every section, empty. Used when there are no tables and no projects."""
    return {
        "projects": [],
        "project_names": {},
        "coverage": _view(
            Coverage(0, 0, 0, 0, 0, 0, 0, 0, 0),
            "Nothing has been counted.",
        ),
        "sources": [],
        "analytics": [],
        "waiting": [],
        "settled": [],
        "verified_findings": [],
        "unsubstantiated": [],
        "questions_open": [],
        "questions_answered": [],
        "takes": [],
        "deliverables": [],
        "steers": [],
        "steers_revoked": [],
    }


@router.get(CENTRE_URL, response_class=HTMLResponse)
def review_centre(request: Request) -> HTMLResponse:
    """The whole company: what every project rests on, and what it does not."""
    company_id = current_company_id()
    try:
        with session_scope() as session:
            projects = projects_for_company(session, company_id)
            if not projects:
                body, state = _blank(), STATE_NO_ROWS
            else:
                body, state = _collect(session, company_id, projects), STATE_OK
            waiting_count = len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        body, state, waiting_count = _blank(), STATE_NO_TABLES, 0

    return templates.TemplateResponse(
        request,
        "review_centre.html",
        _context(
            company_id,
            page_title="Review",
            scope_project=None,
            body=body,
            state=state,
            waiting_count=waiting_count,
        ),
    )


@router.get(CENTRE_URL + "/{project_id}", response_class=HTMLResponse)
def review_project(request: Request, project_id: str) -> HTMLResponse:
    """The same page, scoped to one project.

    Another company's project id is a 404, not an empty page and not a redirect.
    The message says nothing about whether the id exists elsewhere.
    """
    company_id = current_company_id()
    try:
        with session_scope() as session:
            project = project_for_company(session, company_id, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="no such project")
            ref = _ref(project)
            body = _collect(session, company_id, [project])
            waiting_count = len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail="no such project") from None

    return templates.TemplateResponse(
        request,
        "review_project.html",
        _context(
            company_id,
            page_title=project.name,
            scope_project=ref,
            body=body,
            state=STATE_OK,
            waiting_count=waiting_count,
        ),
    )


# ---------------------------------------------------------------------------
# Writes. Both audited, both scoped, neither able to leave this screen.
# ---------------------------------------------------------------------------


def _safe_back(back: str) -> str:
    """Where to return after a POST. Only ever a path on this screen.

    A redirect target taken from a form field is an open redirect unless it is
    checked, and "it only ever comes from our own template" is not a check --
    the field is on a page anyone can submit from. Anything that is not a review
    path falls back to the centre.
    """
    candidate = (back or "").strip()
    if not candidate.startswith(CENTRE_URL):
        return CENTRE_URL
    if candidate.startswith(CENTRE_URL + "//") or "\\" in candidate:
        return CENTRE_URL
    if any(char in candidate for char in "\r\n"):
        return CENTRE_URL
    return candidate


def _rerender(
    request: Request,
    company_id: str,
    scope_project: ProjectRef | None,
    error: dict,
    status_code: int,
):
    """Redraw the page with an error, rather than redirecting away from it.

    A refused write leaves the analyst on the page with the form they filled in
    and the reason it was refused. A redirect would lose both.
    """
    try:
        with session_scope() as session:
            if scope_project is not None:
                project = project_for_company(
                    session, company_id, scope_project.project_id
                )
                projects = [project] if project is not None else []
            else:
                projects = projects_for_company(session, company_id)
            body = _collect(session, company_id, projects) if projects else _blank(
                STATE_NO_ROWS
            )
            state = STATE_OK if projects else STATE_NO_ROWS
            waiting_count = len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        body, state, waiting_count = _blank(), STATE_NO_TABLES, 0

    template = "review_project.html" if scope_project else "review_centre.html"
    return templates.TemplateResponse(
        request,
        template,
        _context(
            company_id,
            page_title="Review",
            scope_project=scope_project,
            body=body,
            state=state,
            waiting_count=waiting_count,
            error=error,
        ),
        status_code=status_code,
    )


@router.post(CENTRE_URL + "/escalations/{escalation_id}/resolve")
def resolve(
    request: Request,
    escalation_id: str,
    decision: str = Form(default=""),
    reason: str = Form(default=""),
    reviewer: str = Form(default=""),
    back: str = Form(default=CENTRE_URL),
):
    """Close one refusal from the expert-review queue. Both outcomes audited.

    Resolving does not publish a claim. Approving records that the refusal was
    right; rejecting records that a person disputes it. Neither makes an
    unverified citation verify, so the claim stays withheld either way and the
    coverage strip does not move.

    A rejection with no reason is refused and nothing is written. The point of
    overruling a verification failure is the explanation; an unexplained
    override is exactly the record an auditor cannot use.
    """
    company_id = current_company_id()
    target_url = _safe_back(back)
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="decision must be approve or reject")

    note = " ".join((reason or "").split())
    actor = _actor(reviewer)
    scope = _scope_of(company_id, target_url)

    try:
        with session_scope() as session:
            row = _target(session, company_id, escalation_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such escalation")
            if row.resolved_at is not None:
                raise HTTPException(status_code=409, detail=ALREADY_RESOLVED)

            if decision == DECISION_REJECT and not note:
                blocked = True
            else:
                blocked = False
                citation = _citation_of(session, company_id, row)
                row.resolved_at = _now()
                row.resolved_by = actor
                session.flush()

                if decision == DECISION_APPROVE:
                    action = ACTION_APPROVED
                    summary = f"refusal upheld for {row.claim_id}: {row.reason_code}"
                else:
                    action = ACTION_REJECTED
                    summary = f"refusal disputed for {row.claim_id}: {note}"

                record_event(
                    session,
                    company_id=company_id,
                    actor=actor,
                    action=action,
                    subject_type="escalation",
                    subject_id=row.id,
                    reason=summary,
                    citation=citation,
                )
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail="no such escalation") from None

    if blocked:
        return _rerender(
            request,
            company_id,
            scope,
            {
                "title": "That rejection needs a reason.",
                "body": (
                    f"{escalation_id} was left as it was. "
                    f"{REASON_REQUIRED.capitalize()}."
                ),
                "subject": escalation_id,
            },
            400,
        )

    # 303, so a reload does not repost the decision.
    return RedirectResponse(url=target_url, status_code=303)


@router.post(CENTRE_URL + "/steer")
def steer(
    request: Request,
    project_id: str = Form(default=""),
    instruction: str = Form(default=""),
    issued_by: str = Form(default=""),
    back: str = Form(default=CENTRE_URL),
):
    """Issue a directive that shifts what the research looks for.

    One route for both pages. The company page names the project in a field, the
    project page in a hidden one, and a second route for the second form would
    be a second place the audit could be got wrong.

    issue_steer() writes the row and the audit entry together. A directive that
    changes what the system looks for, with nobody's name and no time on it,
    cannot be reconciled afterwards with the findings it produced.
    """
    company_id = current_company_id()
    target_url = _safe_back(back)
    scope = _scope_of(company_id, target_url)
    text = " ".join((instruction or "").split())
    actor = _actor(issued_by)

    if not text:
        return _rerender(
            request,
            company_id,
            scope,
            {
                "title": "That steer needs an instruction.",
                "body": f"Nothing was written. {STEER_EMPTY.capitalize()}.",
                "subject": project_id,
            },
            400,
        )

    try:
        with session_scope() as session:
            project = project_for_company(session, company_id, project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="no such project")
            issue_steer(session, company_id, project.id, text, actor)
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail="no such project") from None
    except ValueError as failure:
        return _rerender(
            request,
            company_id,
            scope,
            {
                "title": "That steer was refused.",
                "body": str(failure),
                "subject": project_id,
            },
            400,
        )

    return RedirectResponse(url=target_url, status_code=303)


def _scope_of(company_id: str, target_url: str) -> ProjectRef | None:
    """Which page a refused write should be redrawn on.

    Read back from the return path rather than trusted from a second field, so
    the error page and the redirect cannot end up describing different projects.
    An id this company cannot read falls back to the whole-company page.
    """
    if target_url == CENTRE_URL or not target_url.startswith(CENTRE_URL + "/"):
        return None
    project_id = target_url[len(CENTRE_URL) + 1 :]
    if not project_id or "/" in project_id:
        return None
    try:
        with session_scope() as session:
            project = project_for_company(session, company_id, project_id)
            return _ref(project) if project is not None else None
    except SQLAlchemyError:
        return None


__all__ = [
    "CENTRE_URL",
    "REASON_CODE_CLAIM_UNREADABLE",
    "REASON_CODE_UNCITED",
    "AnalyticsGroup",
    "Count",
    "CoverageView",
    "DeliverableRow",
    "ProjectRef",
    "QueueItem",
    "QuestionRow",
    "SourceRow",
    "SteerRow",
    "TakeCard",
    "UnsubstantiatedFinding",
    "VerifiedFinding",
    "router",
    "templates",
]
