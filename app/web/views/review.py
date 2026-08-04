"""The review queue: everything the product refused to assert, and two writes.

This is the screen that answers "what did the system refuse to assert, and
why". It is built from the same call the change screen uses --
app.state.claims.verified_claims() -- so the queue and the claim view cannot
disagree about what verifies. The verdict is recomputed on every request, never
read from a stored flag, which is the whole of ADR-003: fix the citation and the
row stops being a refusal on the next reload, with nobody clearing a status.

Two facts this screen must not blur, and both are in the copy:

  Resolving an escalation does not publish a claim. Approving records that the
  refusal was right; rejecting records that a person disputes it. Neither makes
  an unverified citation verify, so neither turns a withheld claim into an
  assertion. Only the source agreeing with the quote does that.

  A statement that failed its citation never reaches this module. The queue is
  built from WithheldClaim, which has no statement field and no __dict__ to
  attach one to. There is nothing here to leak.

Both writes are audited. An escalation carries who resolved it and when; the
chain carries why. Neither is deleted, because the record of a refusal that a
person overruled is the part an auditor asks for.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app.state.audit import record_event
from app.state.claims import (
    REASON_CODE_AMBIGUOUS_OCCURRENCE,
    WithheldClaim,
    change_for_company,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import session_scope
from app.state.models import Escalation
from app.state.queries import versions_for_company
from app.verification.verifier import Citation, occurrence_count, occurrence_index
from app.web import TEMPLATES_DIR

# Imported, not restated. The tenant and the page shell are decided in one
# place; a second copy here would drift from the screen it has to match.
from app.web.views.proceedings import (
    STATE_NO_ROWS,
    STATE_NO_TABLES,
    STATE_OK,
    chrome,
    claim_owners,
    current_company_id,
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)

ACTION_APPROVED = "escalation.approved"
ACTION_REJECTED = "escalation.rejected"

# There is no sign-in. Rather than record a name the product was never told,
# the log says plainly that nobody signed this one -- best-practices §26: a
# fallback must announce itself.
ANONYMOUS_ACTOR = "person:unauthenticated"

REASON_REQUIRED = "a rejection needs a reason; an unexplained one is not auditable"
ALREADY_RESOLVED = "this escalation was already resolved"

# How much of the source to show on each side of the cited span. Enough to read
# the sentence the offsets land in, short enough that the queue stays a queue.
CONTEXT_CHARS = 320


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One refusal, with everything needed to judge it and nothing more.

    There is no statement field, on purpose and by construction: this is built
    from WithheldClaim, which does not carry one. What a reader gets instead is
    the reason, the citation address, what the claim quoted, and the bytes the
    source really holds at those offsets.

    now_verifies exists because the verdict is recomputed on every request. An
    escalation raised last week against a source that has since been corrected
    shows up here as a refusal that no longer applies, and says so, rather than
    sitting in the queue as a fact that quietly stopped being true.
    """

    escalation_id: str
    claim_id: str
    reason_code: str
    reason_text: str
    detail: str
    citation: str
    version_id: str
    version_label: str
    citation_start: int
    citation_end: int
    quoted: str
    source_lead: str
    source_marked: str
    source_tail: str
    lead_clipped: bool
    tail_clipped: bool
    source_readable: bool
    change_id: str | None
    change_url: str | None
    section: str
    claim_readable: bool
    now_verifies: bool
    occurrences: int
    occurrence_position: int
    resolved_at: str | None
    resolved_by: str | None


@dataclass(frozen=True, slots=True)
class ReviewPage:
    waiting: tuple[QueueItem, ...]
    settled: tuple[QueueItem, ...]
    state: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _actor(name: str) -> str:
    """Who to record. An unsigned resolution says so rather than borrowing one."""
    cleaned = " ".join((name or "").split())
    return f"person:{cleaned}" if cleaned else ANONYMOUS_ACTOR


def _window(source: str, start: int, end: int) -> tuple[str, str, str, bool, bool, bool]:
    """The cited span with a little source either side, and whether it was real.

    Offsets outside the source return nothing readable rather than a clamped
    window. A window that silently slid to the nearest valid position would show
    an analyst text the citation does not point at, which is the same error the
    verifier exists to catch.
    """
    if start < 0 or end < start or end > len(source):
        return "", "", "", False, False, False
    lead_from = max(0, start - CONTEXT_CHARS)
    tail_to = min(len(source), end + CONTEXT_CHARS)
    return (
        source[lead_from:start],
        source[start:end],
        source[end:tail_to],
        lead_from > 0,
        tail_to < len(source),
        True,
    )


def _ambiguity(held: WithheldClaim, source: str) -> tuple[int, int]:
    """How many times the quote appears, and which one the citation points at.

    Only computed for an ambiguous citation, and only then because the two
    strings a mismatch normally shows are identical in this case: the words do
    match the source. What is missing is which of the three identical sentences
    the claim relies on, and a reader cannot see that from the quote. The count
    is the fact that makes the refusal legible.

    Position is one-based for reading, and 0 when the cited span is not one of
    the occurrences at all.
    """
    if held.reason_code != REASON_CODE_AMBIGUOUS_OCCURRENCE or not source:
        return 0, 0
    citation = Citation(
        version_id=held.citation_version_id,
        char_start=held.citation_start,
        char_end=held.citation_end,
        quoted_text=held.citation_quote,
    )
    found = occurrence_index(citation, source)
    return occurrence_count(held.citation_quote, source), found + 1


def _page(session, company_id: str) -> ReviewPage:
    """Build the queue from the rows, re-verifying every citation on the way.

    Ordering is the escalation id, which is stable, so the queue does not
    reshuffle under someone mid-review.
    """
    escalations = escalations_for_company(session, company_id)
    if not escalations:
        return ReviewPage(waiting=(), settled=(), state=STATE_NO_ROWS)

    owners: dict[str, str] = {}
    for proceeding in proceedings_for_company(session, company_id):
        owners.update(claim_owners(session, company_id, proceeding.id))

    sources = {
        version.id: (version.label, version.source_text)
        for version in versions_for_company(session, company_id)
    }

    # Re-verify once per change that actually has something escalated against
    # it, rather than once per escalation.
    withheld: dict[str, WithheldClaim] = {}
    verified_now: dict[str, VerifiedClaim] = {}
    for change_id in sorted(
        {owners[row.claim_id] for row in escalations if row.claim_id in owners}
    ):
        verified, held = verified_claims(session, company_id, change_id)
        verified_now.update({claim.claim_id: claim for claim in verified})
        withheld.update({claim.claim_id: claim for claim in held})

    waiting: list[QueueItem] = []
    settled: list[QueueItem] = []
    for row in escalations:
        change_id = owners.get(row.claim_id)
        change = (
            change_for_company(session, company_id, change_id) if change_id else None
        )
        held = withheld.get(row.claim_id)
        fixed = verified_now.get(row.claim_id)

        # A claim that verifies now carries the same four citation fields as one
        # that does not, and neither carries anything else this page renders.
        # What it does not carry here is its statement: VerifiedClaim has one,
        # this queue never reads it, and the withheld form has none to read.
        cited = held if held is not None else fixed
        if cited is not None:
            version_id = cited.citation_version_id
            start, end = cited.citation_start, cited.citation_end
            quoted = cited.citation_quote
        else:
            # The claim behind this escalation cannot be read under this
            # company's scope. The escalation is still shown, from what it
            # stored when it was raised -- an item that vanished because its
            # claim moved would be the queue lying about what is outstanding.
            version_id, start, end, quoted = "", 0, 0, ""

        # The reason is the refusal's own, except while it still stands, when it
        # is the verifier's. Rewording either would give the analyst a second
        # vocabulary to learn.
        if held is not None:
            reason_code, reason_text = held.reason_code, held.reason_text
        else:
            reason_code, reason_text = row.reason_code, row.reason_text

        label, source = sources.get(version_id, ("", ""))
        lead, marked, tail, lead_clipped, tail_clipped, readable = (
            _window(source, start, end)
            if version_id
            else ("", "", "", False, False, False)
        )
        occurrences, position = (
            _ambiguity(held, source) if held is not None else (0, 0)
        )

        item = QueueItem(
            escalation_id=row.id,
            claim_id=row.claim_id,
            reason_code=reason_code,
            reason_text=reason_text,
            detail=row.detail or "",
            citation=f"{version_id}:{start}:{end}" if version_id else "not readable",
            version_id=version_id,
            version_label=label,
            citation_start=start,
            citation_end=end,
            quoted=quoted,
            source_lead=lead,
            source_marked=marked,
            source_tail=tail,
            lead_clipped=lead_clipped,
            tail_clipped=tail_clipped,
            source_readable=readable,
            change_id=change.id if change else None,
            change_url=f"/changes/{change.id}" if change else None,
            section=(change.section if change and change.section else "not numbered"),
            claim_readable=row.claim_id in owners,
            now_verifies=row.claim_id in verified_ids,
            occurrences=occurrences,
            occurrence_position=position,
            resolved_at=_stamp(row.resolved_at),
            resolved_by=row.resolved_by,
        )
        (settled if row.resolved_at is not None else waiting).append(item)

    return ReviewPage(
        waiting=tuple(waiting), settled=tuple(settled), state=STATE_OK
    )


def _context(company_id: str, page: ReviewPage, error: dict | None = None) -> dict:
    context = chrome(
        company_id,
        page_title="Review queue",
        nav_active="review",
        review_count=len(page.waiting),
    )
    context.update(
        {
            "waiting": page.waiting,
            "settled": page.settled,
            "state": page.state,
            "error": error,
            "decision_approve": DECISION_APPROVE,
            "decision_reject": DECISION_REJECT,
        }
    )
    return context


@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request) -> HTMLResponse:
    """Everything held back, with the reason in plain words and the source."""
    company_id = current_company_id()
    try:
        with session_scope() as session:
            page = _page(session, company_id)
    except SQLAlchemyError:
        page = ReviewPage(waiting=(), settled=(), state=STATE_NO_TABLES)

    return templates.TemplateResponse(request, "review.html", _context(company_id, page))


@router.post("/review/{escalation_id}/resolve")
def resolve_escalation(
    request: Request,
    escalation_id: str,
    decision: str = Form(default=""),
    reason: str = Form(default=""),
    reviewer: str = Form(default=""),
):
    """Close one refusal, either upholding it or disputing it. Both audited.

    A rejection with no reason is refused and nothing is written: the point of
    overruling a verification failure is the explanation, and an unexplained
    override is exactly the record an auditor cannot use.

    Another company's escalation id is a 404. Nothing about it is read, nothing
    about it is written, and the message says nothing about whether the id
    exists somewhere else.
    """
    company_id = current_company_id()
    if decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="decision must be approve or reject")

    note = " ".join((reason or "").split())
    actor = _actor(reviewer)

    try:
        with session_scope() as session:
            target = _target(session, company_id, escalation_id)
            if target is None:
                raise HTTPException(status_code=404, detail="no such escalation")
            if target.resolved_at is not None:
                raise HTTPException(status_code=409, detail=ALREADY_RESOLVED)

            if decision == DECISION_REJECT and not note:
                page = _page(session, company_id)
                context = _context(
                    company_id,
                    page,
                    error={
                        "title": "That rejection needs a reason.",
                        "body": (
                            f"{escalation_id} was left as it was. "
                            f"{REASON_REQUIRED.capitalize()}."
                        ),
                        "escalation_id": escalation_id,
                    },
                )
                return templates.TemplateResponse(
                    request, "review.html", context, status_code=400
                )

            citation = _citation_of(session, company_id, target)
            target.resolved_at = _now()
            target.resolved_by = actor
            session.flush()

            if decision == DECISION_APPROVE:
                action = ACTION_APPROVED
                summary = f"refusal upheld for {target.claim_id}: {target.reason_code}"
            else:
                action = ACTION_REJECTED
                summary = f"refusal disputed for {target.claim_id}: {note}"

            record_event(
                session,
                company_id=company_id,
                actor=actor,
                action=action,
                subject_type="escalation",
                subject_id=target.id,
                reason=summary,
                citation=citation,
            )
    except SQLAlchemyError:
        raise HTTPException(status_code=404, detail="no such escalation") from None

    # 303, so a reload of the review page does not repost the decision.
    return RedirectResponse(url="/review", status_code=303)


def _target(session, company_id: str, escalation_id: str) -> Escalation | None:
    """Pick the escalation out of the company's own list, or None.

    Read through escalations_for_company() rather than by primary key. A
    get-by-id here would be a second path to the same row with the scope
    enforced by hand, and hand-enforced scope is what the chokepoint exists to
    remove.
    """
    for row in escalations_for_company(session, company_id):
        if row.id == escalation_id:
            return row
    return None


def _citation_of(session, company_id: str, target: Escalation) -> str:
    """The citation address for the audit entry, or "" when it cannot be read.

    Empty rather than invented. The audit row still names the escalation and the
    claim, so nothing is lost except a coordinate the product could not confirm.
    """
    for proceeding in proceedings_for_company(session, company_id):
        owners = claim_owners(session, company_id, proceeding.id)
        change_id = owners.get(target.claim_id)
        if change_id is None:
            continue
        _verified, held = verified_claims(session, company_id, change_id)
        for claim in held:
            if claim.claim_id == target.claim_id:
                return (
                    f"{claim.citation_version_id}:"
                    f"{claim.citation_start}:{claim.citation_end}"
                )
    return ""


__all__ = [
    "ACTION_APPROVED",
    "ACTION_REJECTED",
    "ANONYMOUS_ACTOR",
    "DECISIONS",
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "QueueItem",
    "ReviewPage",
    "router",
    "templates",
]
