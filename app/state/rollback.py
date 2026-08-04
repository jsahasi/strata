"""Taking a decision back, in the only way an append-only log allows.

WHAT THIS FIXES. A reviewer closes an escalation -- records that a refusal was
right, or that a person disputes it -- and is wrong. Until now that decision
stood for ever, and the only remedy was a second decision beside it saying
something else. An auditor reading two rows could not tell a correction from a
disagreement. This module writes the difference down: a new event that names the
one it takes back, so "does this still stand" has an answer instead of an
argument.

WHAT A REVERSAL IS. A row. The original event is not edited, not deleted and not
flagged -- writing anything on it would be the rewrite the whole table exists to
prevent, and the record of a decision somebody later took back is precisely what
an auditor asks for. Both rows stay in the chain and the chain verifies over
both. The pointer sits inside the row's digest, under scheme 3, so re-pointing a
reversal at a different event turns the chain red rather than quietly changing
what the log says was undone.

WHAT A REVERSAL IS NOT, AND THIS IS THE SENTENCE TO READ TWICE. Reverting a
decision restores the state that decision changed. It does not make an
unverified citation verify. NOTHING in this module may turn a withheld claim
into an assertion -- only the source agreeing with the quote does that. Undoing
the approval of a refusal reopens the escalation and changes nothing whatsoever
about what the source says, and there is a test that reads this file's own
source to keep a later edit from blurring it.

TAKING BACK A REVERSAL DOES NOT REINSTATE. A reversal can itself be taken back,
and doing so writes a third row -- but the decision the reversal undid does not
come back into force. Putting it back would mean writing a resolver's name onto
state nobody re-decided: either the person who only withdrew the undo, who never
made that judgement, or the original reviewer at a time when the escalation was
demonstrably open. Both are false statements, so neither is written. The subject
stays where the reversal left it and waits for a fresh decision, the row carries
its own action code saying so, and event_stands() agrees -- a withdrawn reversal
never makes the original decision stand again.

WHAT IS REVERSIBLE. Only what RESTORERS names: today, closing an escalation.
An action with no entry there is refused rather than recorded, because a row
saying a decision was taken back when no state moved is a fallback that did not
announce itself. Adding one means writing the function that puts the state back
and registering it under the exact (action, subject_type) pair the writer uses.

TRANSACTIONS. revert_event() restores state and then appends the row, in that
order and in the caller's session. It must be called inside a transaction the
caller aborts on failure -- app.state.db.session_scope does exactly that. Called
outside one, a failure between the two writes would leave state restored with
nothing in the log to say why.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

# Imported, never restated. Two spellings of one action code drift, and a
# resolution written under a code this module does not hold is a decision
# nobody can take back -- with nothing anywhere saying so.
from app.state.audit import (
    ACTION_DECISION_REVERTED,
    ACTION_ESCALATION_APPROVED,
    ACTION_ESCALATION_REJECTED,
    ACTION_ESCALATION_RESOLVED,
    ACTION_REVERSAL_WITHDRAWN,
    ALREADY_REVERTED,
    NO_SUCH_EVENT,
    AuditTamperError,
    _reversing_row,
    record_event,
)
from app.state.models import AuditEvent, Escalation

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

SUBJECT_ESCALATION = "escalation"

REASON_REQUIRED = (
    "a reversal needs a reason; an unexplained one is exactly the record an "
    "auditor cannot use"
)
ACTOR_REQUIRED = "a reversal needs an actor; an unattributed one is not auditable"
NOT_REVERSIBLE = (
    "this module cannot restore the state that action changed, so it will not "
    "write a row claiming it did"
)
NOTHING_TO_RESTORE = "the state that decision changed is not there to put back"
# The phrase that keeps a withdrawal from reading as a reinstatement. It goes in
# the stored reason and in the value handed back, so neither a log reader nor a
# screen can miss it.
NOT_REINSTATED = "is not reinstated and needs a fresh decision"


@dataclass(frozen=True, slots=True)
class Reversal:
    """What a revert did, in a form a surface can render without deciding anything.

    event is the new row. Its action says which of the two things happened:
    decision.reverted, where state went back, or decision.reversal_withdrawn,
    where an undo was taken back and nothing was reinstated.

    outcome is the same fact in plain words, for the person reading the screen.
    Frozen and slotted, like Coverage and the withheld form: a template is handed
    this object and must not be able to adjust what it is about to display.
    """

    event: AuditEvent
    reverted_event_id: int
    outcome: str


# ---------------------------------------------------------------------------
# What each reversible decision puts back
#
# Keyed on the PAIR (action, subject_type), not on the action alone. The same
# verb means different things about different subjects -- approving an
# obligation action and approving an escalation share a word and share no state
# -- and a registry keyed on the verb would run the wrong restorer on the day a
# second subject type arrives, silently.
#
# A restorer raises rather than returning a flag when the state is not where the
# event left it. Refusing is the whole contract: absence is denial applies to
# undo exactly as it applies to citations.
# ---------------------------------------------------------------------------


def _reopen_escalation(session: Session, company_id: str, event: AuditEvent) -> str:
    """Put an escalation back in the queue: no resolver, no resolution time.

    The two columns are cleared together because they are written together. A
    time with nobody against it describes an event nobody can name.

    WHAT THIS CANNOT PROVE, stated rather than implied. It confirms the
    escalation is resolved right now. It cannot confirm that the open resolution
    is the one this event recorded: the audit row's occurred_at and the row's
    resolved_at are stamped by two different clocks a few microseconds apart, and
    the resolver is a display label that two people can share. No path in the
    product reaches that state today -- resolving refuses an escalation that is
    already resolved, and an event can be reverted once -- so the gap is a
    statement about what the check proves, not a hole anyone can walk through.
    """
    row = (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == event.subject_id)
        .one_or_none()
    )
    if row is None:
        raise ValueError(
            f"{NOTHING_TO_RESTORE}: this company has no escalation "
            f"{event.subject_id!r}"
        )
    if row.resolved_at is None and row.resolved_by is None:
        raise ValueError(
            f"{NOTHING_TO_RESTORE}: {row.id} is already open, so there is nothing "
            "this reversal would put back"
        )

    row.resolved_at = None
    row.resolved_by = None
    session.flush()
    return f"{row.id} is open again and waiting in the review queue"


# Every way an escalation gets closed, mapped to the one way it gets reopened.
#
# ACTION_ESCALATION_RESOLVED has no writer today. It is registered anyway
# because it names the same state change on the same subject, and a registry
# that knew two of three synonymous codes would fail in the least visible way
# there is: a reviewer clicking undo and being told the decision cannot be taken
# back, for no reason a user could see.
RESTORERS = {
    (ACTION_ESCALATION_APPROVED, SUBJECT_ESCALATION): _reopen_escalation,
    (ACTION_ESCALATION_REJECTED, SUBJECT_ESCALATION): _reopen_escalation,
    (ACTION_ESCALATION_RESOLVED, SUBJECT_ESCALATION): _reopen_escalation,
}


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------


def _require_event_id(event_id: int) -> int:
    """An id that is not a number matches nothing and looks like a missing row.

    Refused rather than passed to the database, where a string would compare
    unequal to every integer id and hand back the one answer this module must
    never give by accident: nothing here.
    """
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        raise ValueError(
            f"event_id must be an audit event id; got {event_id!r}. A value the "
            "database cannot match would be reported as a missing row."
        )
    return event_id


def _event_for_company(session: Session, company_id: str, event_id: int) -> AuditEvent:
    """One event from this company's chain, or a refusal that says nothing more.

    Another tenant's id raises the same words as an id nobody has. Telling the
    two apart would confirm that some other company holds that row, which is the
    fact the scope exists to withhold.
    """
    _require_scope(company_id)
    _require_event_id(event_id)
    row = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .where(AuditEvent.id == event_id)
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(NO_SUCH_EVENT)
    return row


def reversal_of(
    session: Session, *, company_id: str, event_id: int
) -> AuditEvent | None:
    """The row that took this event back, or None if nothing has.

    Scoped both ways. The event has to be this company's, and so does the row
    pointing at it: audit ids are unique across tenants, so an unscoped search
    would let one company's chain answer a question about another's.
    """
    event = _event_for_company(session, company_id, event_id)
    return _reversing_row(session, company_id, event.id)


def reverted_event(
    session: Session, *, company_id: str, event_id: int
) -> AuditEvent | None:
    """The event this one takes back, or None where it takes back nothing.

    A pointer that crosses companies raises instead of resolving. Nothing in the
    schema stops one being written -- audit ids are unique across tenants and the
    foreign key does not care which company a row sits in -- so a reader that
    found one and followed it would hand back another tenant's record dressed as
    this tenant's history. Refusing to read it as a reversal is the only safe
    answer, and it is loud because the row is either forged or a defect.
    """
    event = _event_for_company(session, company_id, event_id)
    if event.reverts_event_id is None:
        return None

    target = session.get(AuditEvent, event.reverts_event_id)
    if target is None or target.company_id != company_id:
        raise AuditTamperError(
            f"{company_id}: seq {event.seq} says it reverts event "
            f"{event.reverts_event_id}, which is not in this company's chain. "
            "Refusing to read it as a reversal."
        )
    return target


def event_stands(session: Session, *, company_id: str, event_id: int) -> bool:
    """Does this decision still govern? False once any row takes it back.

    A withdrawn reversal does NOT put the original back. B takes back A, C takes
    back B: A does not stand, B does not stand, C does. That is the read-model
    half of the rule in this file's docstring, and it has to agree with the state
    -- the escalation is open in that sequence, so an answer of True for A would
    say a decision governs that nothing in the product is acting on.

    An event this company cannot read is refused rather than answered. Returning
    True for a row you cannot see is a guess, and it is the reassuring kind.
    """
    event = _event_for_company(session, company_id, event_id)
    return _reversing_row(session, company_id, event.id) is None


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def revert_event(
    session: Session,
    *,
    company_id: str,
    event_id: int,
    actor: str,
    actor_kind: str,
    reason: str,
    actor_user_id: str | None = None,
    session_id: str | None = None,
    ip: str | None = None,
) -> Reversal:
    """Take one decision back. Appends a row, restores state, deletes nothing.

    Every check runs before any state moves, so a refused revert leaves the
    database exactly as it found it.

    actor_kind has no default, unlike record_event's. A machine may take a
    decision back -- a migration correcting a batch is a real case -- but it has
    to say so. A default of "user" would quietly claim a person made a judgement;
    a default of "system" would quietly claim nobody was answerable for one. The
    caller knows which it is and is made to say.

    The new row keeps the subject of the row it takes back, so "everything that
    happened to this escalation" finds both. Which event was undone is the
    pointer, not the subject. It cites nothing: a reversal rests on the
    reverter's judgement, not on a span of a source document.
    """
    # Scope first, ahead of every other check, and called again a few lines
    # down inside _event_for_company. The repetition buys the ordering: an
    # unscoped call must be refused for being unscoped, not for a missing
    # reason, or the caller fixes the wrong thing and the scope bug survives.
    _require_scope(company_id)
    _require_event_id(event_id)

    note = " ".join((reason or "").split())
    if not note:
        raise ValueError(REASON_REQUIRED)
    if not actor:
        raise ValueError(ACTOR_REQUIRED)

    target = _event_for_company(session, company_id, event_id)
    if _reversing_row(session, company_id, target.id) is not None:
        raise ValueError(ALREADY_REVERTED)

    if target.reverts_event_id is not None:
        # The row being taken back is itself a reversal. Nothing is put back --
        # see the docstring at the top of this file for why a reinstatement
        # cannot be written honestly.
        action = ACTION_REVERSAL_WITHDRAWN
        outcome = f"{target.subject_id} {NOT_REINSTATED}"
        summary = (
            f"withdraws the reversal at seq {target.seq}; {target.subject_id} "
            f"{NOT_REINSTATED}: {note}"
        )
    else:
        restore = RESTORERS.get((target.action, target.subject_type))
        if restore is None:
            raise ValueError(
                f"{NOT_REVERSIBLE}: nothing is registered for "
                f"({target.action!r}, {target.subject_type!r})"
            )
        action = ACTION_DECISION_REVERTED
        outcome = restore(session, company_id, target)
        summary = f"reverses seq {target.seq} ({target.action}): {note}"

    entry = record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type=target.subject_type,
        subject_id=target.subject_id,
        reason=summary,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        session_id=session_id,
        ip=ip,
        reverts_event_id=target.id,
    )
    return Reversal(event=entry, reverted_event_id=target.id, outcome=outcome)


__all__ = [
    "ACTOR_REQUIRED",
    "ALREADY_REVERTED",
    "NOTHING_TO_RESTORE",
    "NOT_REINSTATED",
    "NOT_REVERSIBLE",
    "NO_SUCH_EVENT",
    "REASON_REQUIRED",
    "RESTORERS",
    "SUBJECT_ESCALATION",
    "Reversal",
    "event_stands",
    "revert_event",
    "reversal_of",
    "reverted_event",
]
