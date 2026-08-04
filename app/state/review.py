"""The review centre: scoped reads, two audited writes, and one honest count.

Three rules govern this module.

First, tenancy. Every read takes a company_id and a project_id and refuses a
call missing either, in the style of app/state/queries.py. The company guard is
imported from there rather than copied, because two copies of a tenant check
drift and the point of a chokepoint is that there is only one.

Second, coverage. coverage_for_project() is computed from the rows on every
call and never stored. Every surface that synthesises or counts renders it: the
collective take, the analytics panel, each deliverable. A stored count is a
second copy of the truth that nothing recomputes, and on the day it disagrees
with the rows it counts, the count is the thing people read.

Third, and this is the one the product rests on: a synthesis states what it
excluded. compose_take() writes findings_included and findings_withheld
together, from coverage taken at that moment, and refuses to write a take that
would hide its exclusions. A take saying "rests on nine findings" and a take
saying "rests on nine, excludes four that failed verification" are different
documents, and only the second one can be handed to a regulator.

What counts as verified, precisely. A finding is verified when it names a claim
and that claim's citation verifies against the stored source right now --
re-checked through verified_claims(), never read from a stored verdict. A
finding with no claim, a finding whose claim cannot be read under this
company's scope, and a finding whose citation fails all count as withheld. An
unsourced finding is not evidence, whoever raised it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.state.audit import record_event
from app.state.claims import verified_claims
from app.state.models import (
    Change,
    Claim,
    CollectiveTake,
    Deliverable,
    Finding,
    Question,
    Source,
    SteerDirective,
)

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

KIND_INTERNAL = "internal"
KIND_EXTERNAL = "external"

ACTION_STEER_ISSUED = "steer.issued"
ACTION_STEER_REVOKED = "steer.revoked"
ACTION_TAKE_COMPOSED = "take.composed"

_SILENT_TAKE = (
    "a collective take must state what it excluded; refusing to compose one "
    "that would hide withheld findings"
)
_MISCOUNTED_TAKE = (
    "a collective take must carry the coverage it was composed against; "
    "refusing to write counts that do not match the rows"
)


def _require_project(project_id: str) -> str:
    """A project-scoped read with no project is refused, not answered broadly.

    Same failure as an unscoped company read, one level down: a filter that
    quietly matches every project returns a coverage number for work the caller
    never asked about, and the number looks perfectly reasonable.
    """
    if not project_id or not isinstance(project_id, str):
        raise ValueError("project_id is required; refusing an unscoped project read")
    return project_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def sources_for_project(
    session: Session, company_id: str, project_id: str
) -> list[Source]:
    """This project's sources, internal and external, in id order."""
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(Source)
        .filter(Source.company_id == company_id)
        .filter(Source.project_id == project_id)
        .order_by(Source.id)
        .all()
    )


def findings_for_project(
    session: Session, company_id: str, project_id: str
) -> list[Finding]:
    """Every finding on the project, whatever its status, in id order.

    Raw rows. A Finding row is not permission to display its headline: the
    caller has to know whether the claim behind it verifies, which is what
    coverage_for_project() works out.
    """
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(Finding)
        .filter(Finding.company_id == company_id)
        .filter(Finding.project_id == project_id)
        .order_by(Finding.id)
        .all()
    )


def questions_for_project(
    session: Session, company_id: str, project_id: str
) -> list[Question]:
    """Every question on the project, answered or not, oldest first."""
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(Question)
        .filter(Question.company_id == company_id)
        .filter(Question.project_id == project_id)
        .order_by(Question.asked_at, Question.id)
        .all()
    )


def open_questions(
    session: Session, company_id: str, project_id: str
) -> list[Question]:
    """The unanswered questions, blocking ones first, then oldest first.

    Blocking first because that is the order the work is done in. A list that
    buries the three questions stopping the deliverable among seventeen that do
    not has handed the sorting back to the analyst, which is the job the
    product is supposed to be doing.

    Answered means answered_at is set. A question answered by somebody stops
    blocking, and the row stays readable through questions_for_project().
    """
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(Question)
        .filter(Question.company_id == company_id)
        .filter(Question.project_id == project_id)
        .filter(Question.answered_at.is_(None))
        .order_by(Question.blocking.desc(), Question.asked_at, Question.id)
        .all()
    )


def steer_directives_for_project(
    session: Session,
    company_id: str,
    project_id: str,
    *,
    include_revoked: bool = False,
) -> list[SteerDirective]:
    """The directives steering this project. Active only, unless asked.

    include_revoked is keyword-only so it cannot land in the project_id slot by
    accident. The default is the active set, because that is what the system is
    actually looking for now. Audit asks for all of them, which is why revoking
    stamps a time rather than deleting a row.
    """
    _require_scope(company_id)
    _require_project(project_id)
    query = (
        session.query(SteerDirective)
        .filter(SteerDirective.company_id == company_id)
        .filter(SteerDirective.project_id == project_id)
    )
    if not include_revoked:
        query = query.filter(SteerDirective.revoked_at.is_(None))
    return query.order_by(SteerDirective.issued_at, SteerDirective.id).all()


def deliverables_for_project(
    session: Session, company_id: str, project_id: str
) -> list[Deliverable]:
    """Everything drafted, in review or final on this project, newest last."""
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(Deliverable)
        .filter(Deliverable.company_id == company_id)
        .filter(Deliverable.project_id == project_id)
        .order_by(Deliverable.created_at, Deliverable.id)
        .all()
    )


def collective_take_for_project(
    session: Session, company_id: str, project_id: str
) -> CollectiveTake | None:
    """The current take: the latest one nothing has superseded, or None.

    None rather than an empty object, because a project with no take yet is a
    real state and the interface must say so rather than render a blank
    synthesis, which reads as "nothing to report".
    """
    _require_scope(company_id)
    _require_project(project_id)
    return (
        session.query(CollectiveTake)
        .filter(CollectiveTake.company_id == company_id)
        .filter(CollectiveTake.project_id == project_id)
        .filter(CollectiveTake.superseded_by.is_(None))
        .order_by(CollectiveTake.composed_at.desc(), CollectiveTake.id.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Coverage -- the number every synthesis has to show
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    """What a project's synthesis rests on, and what it does not.

    findings_verified plus findings_withheld always equals findings_total.
    There is no third bucket and no rounding: every finding is either backed by
    a citation that verifies right now, or it is not.

    claims_verified and claims_withheld are the claim-level picture underneath,
    summed over every change these findings reach. They are usually larger than
    the finding counts, because one change carries several claims and not every
    claim has been raised as a finding.

    sources_internal and sources_external count the two named kinds. A source
    row with any other kind counts in neither, so a typo shows up as a total
    that does not add up rather than as a source silently filed under the wrong
    heading.

    Frozen and slotted: this object is handed to templates, and a surface must
    not be able to adjust the coverage it is about to display.
    """

    findings_total: int
    findings_verified: int
    findings_withheld: int
    claims_verified: int
    claims_withheld: int
    open_questions: int
    blocking_questions: int
    sources_internal: int
    sources_external: int


def _claims_by_id(
    session: Session, company_id: str, claim_ids: set[str]
) -> dict[str, Claim]:
    """Load named claims under this company's scope. Others simply are not here.

    Joined to Change and filtered on both company columns, the same belt and
    braces claims_for_change() uses: a write bug that stamped the wrong company
    on a claim would otherwise hand one tenant another's row, and the column
    meant to prevent that is the column that let it through.
    """
    if not claim_ids:
        return {}
    rows = (
        session.query(Claim)
        .join(Change, Claim.change_id == Change.id)
        .filter(Claim.company_id == company_id)
        .filter(Change.company_id == company_id)
        .filter(Claim.id.in_(sorted(claim_ids)))
        .all()
    )
    return {claim.id: claim for claim in rows}


def coverage_for_project(
    session: Session, company_id: str, project_id: str
) -> Coverage:
    """Count what this project rests on, from the rows, every time.

    Nothing here is cached or stored. Verification runs through
    verified_claims(), which re-reads the stored source at the cited offsets,
    so editing a source flips a finding from verified to withheld on the next
    call. A stored coverage number would keep saying what was true last week,
    and it would say it with the same confidence.
    """
    _require_scope(company_id)
    _require_project(project_id)

    findings = findings_for_project(session, company_id, project_id)
    questions = questions_for_project(session, company_id, project_id)
    sources = sources_for_project(session, company_id, project_id)

    claims = _claims_by_id(
        session, company_id, {f.claim_id for f in findings if f.claim_id}
    )

    # Every change these findings touch, whether named directly or reached
    # through a claim. Order kept stable so the counting is repeatable.
    change_ids: list[str] = []
    for finding in findings:
        claim = claims.get(finding.claim_id) if finding.claim_id else None
        for change_id in (finding.change_id, claim.change_id if claim else None):
            if change_id and change_id not in change_ids:
                change_ids.append(change_id)

    verified_claim_ids: set[str] = set()
    claims_verified = 0
    claims_withheld = 0
    for change_id in change_ids:
        verified, withheld = verified_claims(session, company_id, change_id)
        verified_claim_ids.update(claim.claim_id for claim in verified)
        claims_verified += len(verified)
        claims_withheld += len(withheld)

    findings_verified = sum(
        1 for finding in findings if finding.claim_id in verified_claim_ids
    )
    unanswered = [q for q in questions if q.answered_at is None]

    return Coverage(
        findings_total=len(findings),
        findings_verified=findings_verified,
        findings_withheld=len(findings) - findings_verified,
        claims_verified=claims_verified,
        claims_withheld=claims_withheld,
        open_questions=len(unanswered),
        blocking_questions=sum(1 for q in unanswered if q.blocking),
        sources_internal=sum(1 for s in sources if s.kind == KIND_INTERNAL),
        sources_external=sum(1 for s in sources if s.kind == KIND_EXTERNAL),
    )


# ---------------------------------------------------------------------------
# Writes -- both audited
# ---------------------------------------------------------------------------


def issue_steer(
    session: Session,
    company_id: str,
    project_id: str,
    instruction: str,
    actor: str,
) -> SteerDirective:
    """Record a directive that shifts what the research looks for.

    A steer is an audit event as well as a row, and both are written here. A
    directive that changes what the system looks for, with no record of who
    issued it and when, is not auditable: the findings it produced can no
    longer be reconciled with the instruction behind them, and the difference
    between a deliberate narrowing and a bug becomes unanswerable.
    """
    _require_scope(company_id)
    _require_project(project_id)

    text = (instruction or "").strip()
    if not text:
        raise ValueError("a steer needs an instruction; an empty one steers nothing")
    if not actor:
        raise ValueError(
            "a steer needs an issuer; an unattributed directive is not auditable"
        )

    directive = SteerDirective(
        id=_new_id("STEER"),
        company_id=company_id,
        project_id=project_id,
        instruction=text,
        issued_by=actor,
        issued_at=_now(),
        applied_at=None,
        effect=None,
        revoked_at=None,
    )
    session.add(directive)
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_STEER_ISSUED,
        subject_type="steer_directive",
        subject_id=directive.id,
        reason=f"steer on {project_id}: {text}",
    )
    return directive


def revoke_steer(
    session: Session, company_id: str, directive_id: str, actor: str
) -> SteerDirective:
    """Stand a directive down. It leaves the active list and stays readable.

    Audited for the same reason issuing is. Withdrawing an instruction changes
    what the system looks for exactly as issuing one did, and a revocation
    nobody signed leaves the same gap.

    Another company's directive id raises rather than resolving. The message
    says nothing about whether the id exists elsewhere.
    """
    _require_scope(company_id)
    if not directive_id:
        raise ValueError("directive_id is required")
    if not actor:
        raise ValueError(
            "a revocation needs an actor; an unattributed one is not auditable"
        )

    directive = (
        session.query(SteerDirective)
        .filter(SteerDirective.company_id == company_id)
        .filter(SteerDirective.id == directive_id)
        .one_or_none()
    )
    if directive is None:
        raise ValueError("no such steer directive for this company")
    if directive.revoked_at is not None:
        # Already down. Returning it beats a second audit event saying the same
        # thing, and beats moving revoked_at, which would rewrite when it
        # stopped applying to the findings raised in between.
        return directive

    directive.revoked_at = _now()
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_STEER_REVOKED,
        subject_type="steer_directive",
        subject_id=directive.id,
        reason=f"steer withdrawn: {directive.instruction}",
    )
    return directive


def _refuse_a_silent_take(
    coverage: Coverage, *, findings_included: int, findings_withheld: int
) -> None:
    """Refuse counts that would let a take hide what it left out.

    Two checks, and the first is the one that matters. A take composed while
    findings were withheld must say so; writing zero there turns an omission
    into a clean-looking synthesis, which is the failure this product exists to
    prevent. The second check catches the quieter version -- counts that
    disagree with the coverage they were supposedly taken from.

    Called with coverage's own numbers, this cannot fail, and that is the
    point. It stands between coverage and the row so that a later edit passing
    anything else fails loudly instead of writing a take nobody can trust.
    """
    if coverage.findings_withheld > 0 and findings_withheld <= 0:
        raise ValueError(_SILENT_TAKE)
    if (findings_included, findings_withheld) != (
        coverage.findings_verified,
        coverage.findings_withheld,
    ):
        raise ValueError(_MISCOUNTED_TAKE)


def compose_take(
    session: Session,
    company_id: str,
    project_id: str,
    body: str,
    actor: str,
) -> CollectiveTake:
    """Write the project's synthesis, with the coverage it rests on.

    The counts come from coverage_for_project() taken here, at compose time,
    and are stored on the row. They are a statement about what was known when
    the take was written; recomputing them at read time would answer a
    different question and would quietly rewrite history every time the source
    moved.

    Composing supersedes the previous take rather than editing it. What the
    project believed last week survives being wrong, which is the question an
    auditor asks.
    """
    _require_scope(company_id)
    _require_project(project_id)

    text = (body or "").strip()
    if not text:
        raise ValueError(
            "a collective take needs a body; an empty synthesis says nothing"
        )
    if not actor:
        raise ValueError(
            "a take needs a composer; an unattributed synthesis is not auditable"
        )

    coverage = coverage_for_project(session, company_id, project_id)
    _refuse_a_silent_take(
        coverage,
        findings_included=coverage.findings_verified,
        findings_withheld=coverage.findings_withheld,
    )

    previous = collective_take_for_project(session, company_id, project_id)

    take = CollectiveTake(
        id=_new_id("TAKE"),
        company_id=company_id,
        project_id=project_id,
        body=text,
        composed_at=_now(),
        composed_by=actor,
        findings_included=coverage.findings_verified,
        findings_withheld=coverage.findings_withheld,
        superseded_by=None,
    )
    session.add(take)
    session.flush()

    if previous is not None:
        previous.superseded_by = take.id
        session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_TAKE_COMPOSED,
        subject_type="collective_take",
        subject_id=take.id,
        reason=(
            f"rests on {coverage.findings_verified} verified findings; "
            f"{coverage.findings_withheld} withheld"
        ),
    )
    return take


__all__ = [
    "ACTION_STEER_ISSUED",
    "ACTION_STEER_REVOKED",
    "ACTION_TAKE_COMPOSED",
    "KIND_EXTERNAL",
    "KIND_INTERNAL",
    "Coverage",
    "collective_take_for_project",
    "compose_take",
    "coverage_for_project",
    "deliverables_for_project",
    "findings_for_project",
    "issue_steer",
    "open_questions",
    "questions_for_project",
    "revoke_steer",
    "sources_for_project",
    "steer_directives_for_project",
]
