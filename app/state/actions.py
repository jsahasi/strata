"""Proposed actions: the decision app/auth/policy.py::can_approve exists to gate.

docs/prd.html named this gap before anybody built it: "the monitor, comment and
comply vocabulary exists in code and no screen renders it". The vocabulary sat
in app/interpretation/action.py with no row to hang it on, so there was nothing
to propose, nothing to show and nothing to approve -- and the segregation-of-
duties control, written and unit-tested, had no caller anywhere in the product.
Two attempts to give it one wired it into the escalation queue instead, and both
were reverted, because that queue is the analyst's own screen and gate 2 of
can_approve demands a permission the analyst does not hold. This module is the
decision the control was written for.

FOUR RULES, and each one is a failure this repository has already had.

**Tenancy through the same chokepoint.** Every read and every write takes a
company_id and refuses an unscoped call, and the claim and the change are both
resolved through app/state/claims.py rather than by a query written here. Two
copies of a tenant guard drift, and the copy nobody audited is the one that
leaks.

**The vocabulary is checked at write time, against the status already frozen on
the change.** A draft order binds nobody. ADR-005 keeps DRAFT and FINAL on
separate paths and app/interpretation/action.py holds the words; comply on a
draft is refused here rather than stored and explained later. Checking at read
time would let a stored row change meaning when a version moved on.

**A proposal is authorship, and it says so in the chain.** propose_action
appends under ACTION_ACTION_PROPOSED, naming the proposer with their user id.
That row IS the evidence gate 4 reads back: app/auth/policy.py has no
authored_by column and does not want one -- it reads who did what from the
append-only chain, which is the artefact a regulator would be shown.

**This module never decides who may decide.** It writes what a caller tells it
and audits it. The gate is asked in app/web/views/actions.py, at the moment of
the click, because a permission read taken anywhere earlier is a statement about
the past. Putting can_approve in here would also make it unreachable from the
one place it has to be visible: the screen.

**WHAT THIS DOES NOT DO.** It does not act. Approving a comply action records
that the company decided to comply; nothing files anything, nothing tells
anybody, and no project state moves. That is the honest limit of a 48-hour
build, and the screen says it on the page rather than in this comment alone.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.interpretation.action import action_vocabulary

# Imported, never restated. tests/test_audit.py walks every module under app/
# for a retyped action code, because a row written under the wrong spelling
# hashes perfectly and no query for the right one will ever return it.
from app.state.audit import (
    ACTION_ACTION_APPROVED,
    ACTION_ACTION_PROPOSED,
    ACTION_ACTION_REJECTED,
    ACTOR_SYSTEM,
    ACTOR_USER,
    record_event,
)
from app.state.claims import change_for_company
from app.state.models import (
    PROPOSAL_STATES,
    STATE_APPROVED,
    STATE_PROPOSED,
    STATE_REJECTED,
    Change,
    Claim,
    ProposedAction,
)

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

#: The permission a proposal needs. It sits on ROLE_ANALYST in
#: app/state/identity.py and nowhere else in the seeded grid, which is the
#: other half of the control: the person who shapes what reaches an approver
#: is not the approver.
PROPOSE = "action.propose"

#: Refused a proposal with nothing to review in it.
RATIONALE_REQUIRED = (
    "a proposed action needs a reason; an unexplained one cannot be reviewed, "
    "and reviewing it is the only thing this record is for"
)

__all__ = [
    "PROPOSE",
    "RATIONALE_REQUIRED",
    "STATE_APPROVED",
    "STATE_PROPOSED",
    "STATE_REJECTED",
    "claim_for_action",
    "propose_action",
    "proposed_action_for_company",
    "proposed_actions_for_company",
    "record_decision",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    """A readable prefix and a uuid4. The prefix is for a person reading a log."""
    return f"ACT-{uuid.uuid4().hex[:12]}"


def claim_for_company(session: Session, company_id: str, claim_id: str) -> Claim | None:
    """One claim row, scoped, or None. Another company's id resolves to None.

    Deliberately narrow, and it returns the row rather than anything rendered.
    app/state/claims.py gates claim TEXT behind verified_claims() so nothing
    renders a statement whose citation did not verify, and this is not a way
    around that: callers here want the change id and the citation address, and
    the screen above never prints claim.statement.
    """
    _require_scope(company_id)
    return (
        session.query(Claim)
        .join(Change, Claim.change_id == Change.id)
        .filter(Claim.company_id == company_id)
        .filter(Change.company_id == company_id)
        .filter(Claim.id == claim_id)
        .one_or_none()
    )


def claim_for_action(
    session: Session, company_id: str, action: ProposedAction
) -> Claim | None:
    """The claim a proposal was written against, scoped. None when it cannot be read."""
    return claim_for_company(session, company_id, action.claim_id)


def _citation_of(claim: Claim) -> str:
    return f"{claim.citation_version_id}:{claim.citation_start}:{claim.citation_end}"


def propose_action(
    session: Session,
    company_id: str,
    *,
    claim_id: str,
    kind: str,
    rationale: str,
    actor: str,
    actor_user_id: str | None = None,
) -> ProposedAction:
    """Record that somebody proposes this action on the change under this claim.

    Raises LookupError when the claim does not resolve in this company, so the
    caller can answer 404 without learning whether the id exists elsewhere.
    Raises ValueError when the action is not one the change's status allows, or
    when there is no reason given.

    The audit row's subject is the CLAIM, not the proposal. That is what makes
    it evidence: app/auth/policy.py::_authoring_touch searches for rows naming
    the claim, the change beneath it or an escalation raised against it, and a
    row filed under the proposal's own id would be a decision the gate cannot
    see. The proposal id travels in the reason instead, so a reader can still
    get from the chain to the row.
    """
    _require_scope(company_id)
    if not actor:
        raise ValueError("actor is required; an unattributed proposal is not auditable")

    claim = claim_for_company(session, company_id, claim_id)
    if claim is None:
        raise LookupError(f"no claim {claim_id!r} in this company")

    change = change_for_company(session, company_id, claim.change_id)
    if change is None:
        # A claim whose change cannot be read under this scope. Refusing beats
        # guessing a vocabulary: without the status there is no way to know
        # whether comply is even an available word.
        raise LookupError(f"no change {claim.change_id!r} in this company")

    allowed = action_vocabulary(change.status)
    if kind not in allowed:
        raise ValueError(
            f"{kind!r} is not an action a {change.status} change may produce. "
            f"A {change.status} order allows {', '.join(allowed)}. "
            "This is ADR-005: a draft binds nobody, so complying with one is "
            "the most expensive error available in this domain."
        )

    reason = " ".join((rationale or "").split())
    if not reason:
        raise ValueError(RATIONALE_REQUIRED)

    row = ProposedAction(
        id=_new_id(),
        company_id=company_id,
        claim_id=claim.id,
        change_id=change.id,
        kind=kind,
        rationale=reason,
        proposed_by=actor,
        proposed_by_user_id=actor_user_id or None,
        proposed_at=_utcnow(),
        state=STATE_PROPOSED,
    )
    session.add(row)
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_ACTION_PROPOSED,
        subject_type="claim",
        subject_id=claim.id,
        reason=f"{kind} proposed on {change.id} as {row.id}: {reason}",
        citation=_citation_of(claim),
        actor_user_id=actor_user_id or None,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    return row


def proposed_actions_for_company(
    session: Session, company_id: str, *, waiting_only: bool = False
) -> list[ProposedAction]:
    """Every proposal for this company, oldest first.

    Oldest first so the queue does not reshuffle under somebody mid-review, and
    so the thing that has waited longest is at the top. Ordered by the moment
    and then the id, because two proposals written in the same transaction share
    a timestamp and an unstable order is a queue that reads differently twice.

    waiting_only is keyword-only so it cannot land in the company_id slot.
    """
    _require_scope(company_id)
    query = session.query(ProposedAction).filter(
        ProposedAction.company_id == company_id
    )
    if waiting_only:
        query = query.filter(ProposedAction.state == STATE_PROPOSED)
    return query.order_by(ProposedAction.proposed_at, ProposedAction.id).all()


def proposed_action_for_company(
    session: Session, company_id: str, action_id: str
) -> ProposedAction | None:
    """One proposal, or None. Another company's id resolves to None."""
    _require_scope(company_id)
    if not action_id:
        return None
    return (
        session.query(ProposedAction)
        .filter(ProposedAction.company_id == company_id)
        .filter(ProposedAction.id == action_id)
        .one_or_none()
    )


def record_decision(
    session: Session,
    company_id: str,
    action_id: str,
    *,
    approved: bool,
    actor: str,
    reason: str,
    actor_user_id: str | None = None,
) -> ProposedAction:
    """Write the decision onto the proposal and into the chain. One row each.

    TAKES AN ID AND RE-READS THE ROW, rather than taking the row its caller
    already has. The caller has resolved it once to answer 404 and 409, so this
    is a second query for a row it could have been handed -- and that is the
    price of the scope being enforced here rather than trusted from outside. A
    writer that accepts a row accepts whatever scope the caller applied to fetch
    it, which is hand-enforced tenancy, which is the thing every read in this
    codebase goes through a chokepoint to avoid. Raises LookupError when the id
    names nothing in this company.

    THIS DOES NOT ASK WHETHER THE ACTOR MAY DECIDE. app/web/views/actions.py
    asks app/auth/policy.py::can_approve immediately before calling this, at the
    moment of the click. Asking again here would be a second copy of a rule --
    and the copy that goes stale is the one nobody is reading.

    Refuses a proposal that has already been decided rather than overwriting it.
    A decision taken twice is two decisions, and the second silently replacing
    the first is how an approval loses the name of whoever really made it.
    """
    _require_scope(company_id)
    action = proposed_action_for_company(session, company_id, action_id)
    if action is None:
        raise LookupError(f"no proposed action {action_id!r} in this company")
    if action.state != STATE_PROPOSED:
        raise ValueError(f"{action.id} was already {action.state}")
    if not actor:
        raise ValueError("actor is required; an unattributed decision is not auditable")

    note = " ".join((reason or "").split())
    if not note:
        raise ValueError("a decision needs a reason; an unexplained one is not auditable")

    action.state = STATE_APPROVED if approved else STATE_REJECTED
    action.decided_by = actor
    action.decided_by_user_id = actor_user_id or None
    action.decided_at = _utcnow()
    action.decision_reason = note
    session.flush()

    claim = claim_for_company(session, company_id, action.claim_id)
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_ACTION_APPROVED if approved else ACTION_ACTION_REJECTED,
        # The proposal, not the claim. The claim is where authorship is read
        # from, and app/auth/policy.py exempts these two codes from that read
        # precisely so an approver does not become an author by approving --
        # but filing them against the proposal as well keeps "what happened to
        # this action" a one-subject search.
        subject_type="action",
        subject_id=action.id,
        reason=(
            f"{action.kind} on {action.change_id} "
            f"{'approved' if approved else 'rejected'} for claim "
            f"{action.claim_id}: {note}"
        ),
        citation=_citation_of(claim) if claim is not None else "",
        actor_user_id=actor_user_id or None,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    return action


# The vocabulary this module writes is the one models.py declares. Named here so
# a state added there and never handled in this file fails at import rather than
# becoming a row every query above silently ignores.
_UNHANDLED = set(PROPOSAL_STATES) - {STATE_PROPOSED, STATE_APPROVED, STATE_REJECTED}
if _UNHANDLED:
    raise ValueError(
        f"app/state/models.py declares proposal states this module does not "
        f"handle: {sorted(_UNHANDLED)}"
    )
