"""What the record said at three o'clock on Tuesday, and putting it back.

WHAT THIS FIXES. app/state/rollback.py takes ONE decision back, with a new row
naming the row it reverses. It answers "undo that". It does not answer "what
could this person see on the day they signed it, and put us back there" -- the
question an auditor asks first and the only one a regulator's letter ever asks.
This module answers that, in two halves that carry completely different risk and
are therefore kept apart.

  state_at() READS. It reconstructs what the chain says was true at a moment.
  It writes nothing, ever, and there is a test that reads this file to keep it
  that way. This is the valuable half and it is the safe one.

  restore_to() WRITES. It takes back, newest first, every decision the chain
  records after a moment -- one reversal row each, through revert_event(), so
  every rule that module enforces still holds.

THE CHAIN IS THE ONLY SOURCE. Both halves read audit_events and nothing else.
Every other table is current state that has been overwritten, and asking it what
it looked like last Tuesday gets today's answer with a straight face. That is
also this module's limit: a decision the chain does not record cannot be
replayed, and pretending otherwise is the failure the whole design exists to
prevent.

THE CHAIN IS NEVER REWOUND. Not truncated, not rewritten, not resequenced. A
restore only ever appends. After one, verify_chain() passes over the whole log
including the restore itself, and tests/test_replay.py asserts exactly that
rather than assuming it.

WHAT A RESTORE CANNOT DO, AND THIS IS THE SENTENCE TO READ TWICE. Putting state
back does not make a citation verify. NOTHING here may turn a withheld claim
into an assertion -- only the source agreeing with the quote does that. Reopening
an escalation puts a refusal back in the queue and changes nothing whatsoever
about what the source says.

IT WILL NOT RE-MAKE A DECISION. Taking a decision back is a thing the record can
support. Putting a decision back into force is not: it would need a name and a
time against a judgement nobody made, which is the reasoning rollback.py already
uses to refuse reinstating a withdrawn reversal. So a moment whose restoration
would require re-deciding something is refused, with the reason, rather than
approximated.

NO PARTIAL RESTORE, EVER. If anything recorded after the moment cannot be put
back, the whole restore is refused and the refusal names each blocker and lists
the tables this module covers and the ones it does not. Half a restore reported
as a restore is the worst outcome available here, worse than refusing and worse
than doing nothing.

WHAT IT LEAVES ALONE ON PURPOSE, SAID OUT LOUD. Sign-ins, sign-outs and refusals
are recorded and never restored -- handing back a session somebody signed out of
would hand back access. Those rows do not block a restore, and every restore
reports them in left_alone rather than skipping them quietly. A fallback must
announce itself.

NO SUMMARY ROW. A restore writes no row of its own saying "a restore happened".
There is no action code for one in app/state/audit.py, and inventing a second
spelling of the vocabulary here is the drift that file exists to stop. Instead
every reversal a restore writes names the moment it was restoring to, in its own
reason, so the set is recoverable by reading the log.

TRANSACTIONS. restore_to() writes several rows and moves state between them. It
must be called inside a transaction the caller aborts on failure --
app.state.db.session_scope does exactly that. Called outside one, a refusal
halfway through would leave precisely the partial restore this module refuses to
write.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

# Imported, never restated. A second spelling of an action code is a row no
# query for that code will find, and here it would be a decision a restore
# silently walked past.
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTION_DECISION_REVERTED,
    ACTION_LOGIN_FAILED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_LOGOUT,
    ACTION_REVERSAL_WITHDRAWN,
    ACTION_SESSION_EXPIRED,
    AuditTamperError,
    verify_chain,
)
from app.state.models import AuditEvent, Base

# One guard, one place to audit, same as every other tenant-scoped read.
from app.state.queries import _require_scope

# The registry of what can be put back lives in rollback.py and stays there.
# A second list here would answer differently the day somebody registers a new
# restorer, and the two would disagree in the least visible way available: a
# restore that reported success over state nothing moved.
from app.state.rollback import (
    ACTOR_REQUIRED,
    REASON_REQUIRED,
    RESTORERS,
    SUBJECT_ESCALATION,
    Reversal,
    revert_event,
)

# ---------------------------------------------------------------------------
# What the chain can and cannot speak for
#
# Computed from the schema rather than typed out, so a table added next month
# appears in the "not covered" list without anyone remembering to add it. A hand
# list of 40 tables is a hand list that goes stale, and here going stale means
# claiming coverage the code does not have.
#
# escalations is in BOTH lists on purpose. Two of its columns can be put back
# and the rest of the row cannot: the chain records that an escalation was
# routed to somebody, but names the person only inside the reason, in prose.
# This module does not parse prose, so it cannot say who held an escalation at
# a moment, and it does not guess.
# ---------------------------------------------------------------------------

COVERED = (
    "escalations.resolved_at -- whether a refusal was closed, and when",
    "escalations.resolved_by -- and by whom",
)

# audit_events is absent from both lists because it is not state to be put
# back. It is the record, and the record is never rewound.
NOT_COVERED = tuple(sorted(set(Base.metadata.tables) - {AuditEvent.__tablename__}))


# ---------------------------------------------------------------------------
# The three answers a subject can have, and why the third is not the second
# ---------------------------------------------------------------------------

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_NOT_RECORDED = "not recorded"

NOT_RECORDED_IS_NOT_OPEN = (
    "the chain records no decision on this at or before that moment. An "
    "escalation is raised without an audit row naming it, so this is not the "
    "same as saying it was open, and it is not said as though it were"
)

BEFORE_THE_RECORD = (
    "this moment is before the first row in this company's chain. The record "
    "does not begin until later, which is a different answer from an empty one"
)


# ---------------------------------------------------------------------------
# Actions a restore steps over, each with the reason it steps over it
#
# Keyed on the action alone, unlike RESTORERS, and the difference is deliberate.
# RESTORERS runs a function that writes a named table, so it must know which
# subject it is looking at. This table asserts a negative -- "a restore will not
# put this back, and here is why" -- and each reason below holds whatever the
# row was about.
#
# THE DEFAULT FOR ANYTHING ABSENT IS REFUSAL. An action nobody has classified
# blocks the restore rather than being waved through, so the failure mode of
# forgetting is a loud refusal and never a quiet gap. That is why this table is
# short: every line was written after reading the code that writes that row.
# ---------------------------------------------------------------------------

RECORDS_ONLY = {
    ACTION_LOGIN_SUCCEEDED: (
        "a sign-in opened a session; putting a session back would hand somebody "
        "access they no longer have"
    ),
    ACTION_LOGIN_FAILED: (
        "a failed sign-in changed nothing, and un-recording an attempt would "
        "erase the trace an investigation reads"
    ),
    ACTION_LOGOUT: (
        "a sign-out closed a session; re-opening it would hand back access a "
        "person deliberately gave up"
    ),
    ACTION_SESSION_EXPIRED: (
        "a session ran out; a clock is not a decision anybody can take back"
    ),
    ACTION_ACCESS_DENIED: (
        "a refusal records that something did not happen, so there is no state "
        "behind it to put back"
    ),
    ACTION_REVERSAL_WITHDRAWN: (
        "withdrawing a reversal reinstates nothing (app/state/rollback.py), so "
        "it left no state behind to put back"
    ),
}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

CANNOT_RESTORE = (
    "this company cannot be restored to that moment. A restore that put back "
    "only the half the chain covers, and reported success, would be worse than "
    "this refusal"
)
WHY_NOTHING_PUTS_IT_BACK = (
    "nothing is registered that can put this state back, so the restore will "
    "not write a row claiming it did"
)
WHY_FRESH_DECISION = (
    "this took back a decision made at or before the moment; putting it back "
    "would mean re-making a judgement nobody has made"
)
WHY_CLOCKS_DISAGREE = (
    "this row is stamped at or before the moment but was recorded after rows "
    "stamped later, so 'everything after that moment' is not a clean cut of "
    "the chain and taking back a chunk of the middle is not a restore"
)
NAIVE_MOMENT = (
    "moment must carry a timezone; three o'clock where? A naive stamp compared "
    "against an aware log means a different hour on every host"
)


class UnreplayableError(RuntimeError):
    """The record cannot answer for that moment, and here is the list of why.

    A RuntimeError rather than a ValueError because nothing the caller passed
    was wrong. The arguments are fine; the log is what cannot be replayed, and
    a caller that fixes its arguments will get the same answer.

    blockers carries one Blocker per row standing in the way, so a screen can
    show the auditor exactly which decisions have to be dealt with by hand
    instead of a sentence saying it did not work.
    """

    def __init__(self, message: str, blockers: "tuple[Blocker, ...]") -> None:
        super().__init__(message)
        self.blockers = tuple(blockers)


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Blocker:
    """One row a restore will not step over, and the reason in plain words."""

    seq: int
    event_id: int
    action: str
    subject_type: str
    subject_id: str
    occurred_at: datetime
    why: str


@dataclass(frozen=True, slots=True)
class SubjectState:
    """What the chain said about one subject at the moment asked about.

    state is one of the three constants above, and the third is not the second.
    detail is the same fact in words a person can read on a screen without
    working it out again.
    """

    subject_type: str
    subject_id: str
    state: str
    detail: str
    as_of_seq: int | None
    as_of: datetime | None
    actor: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the chain says was true then, carrying its own limits.

    covers and does_not_cover travel with the answer rather than sitting in a
    doc somebody may not have read. A snapshot handed to a template without them
    would look like a statement about the company; it is a statement about two
    columns of one table.

    note says the snapshot's standing in words a person can read. A template
    handed a bare False for before_the_record renders "no events", and an
    auditor reads that as "nothing happened" -- a different claim, and a wrong
    one. The words travel with the flag so the screen cannot lose them.

    clocks_out_of_order warns rather than refuses. The read is more useful with
    a flag on it than replaced by a wall, and the write refuses on the same
    condition -- see restore_to.

    Frozen and slotted, like Coverage and Reversal: a surface is handed this and
    must not be able to adjust what it is about to display.
    """

    company_id: str
    moment: datetime
    before_the_record: bool
    note: str
    first_event_at: datetime | None
    events_counted: int
    last_seq: int | None
    chain_head_at: str
    subjects: tuple[SubjectState, ...]
    cannot_speak_for: tuple[str, ...]
    clocks_out_of_order: bool
    covers: tuple[str, ...] = COVERED
    does_not_cover: tuple[str, ...] = NOT_COVERED


@dataclass(frozen=True, slots=True)
class Restoration:
    """What a restore did, and what it deliberately did not do.

    left_alone is not decoration. It is the announcement a fallback owes: rows
    the restore stepped over, with the reason it stepped over each. A caller
    that renders reversals and drops left_alone is reporting a restore that did
    more than it did.
    """

    company_id: str
    moment: datetime
    reversals: tuple[Reversal, ...]
    left_alone: tuple[str, ...]
    outcome: str


# ---------------------------------------------------------------------------
# Reading the chain
# ---------------------------------------------------------------------------


def _require_moment(moment) -> datetime:
    """A moment has to be an aware datetime. Anything else is refused, not fixed.

    A naive stamp is the quiet failure: it compares against an aware log by
    raising on some paths and by meaning a different hour on others, depending
    on where the process runs. Neither is an answer an auditor can use.
    """
    if not isinstance(moment, datetime):
        raise ValueError(
            f"moment must be a datetime; got {moment!r}. There is no parsing "
            "here, because a string this could not parse would have to be "
            "guessed at."
        )
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(NAIVE_MOMENT)
    return moment.astimezone(timezone.utc)


def _chain(session: Session, company_id: str) -> list[AuditEvent]:
    """This company's whole chain, in the order it was recorded."""
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
        )
        .scalars()
        .all()
    )


def _read(session: Session, company_id: str, moment) -> tuple[datetime, list[AuditEvent]]:
    """Scope, moment, and a verified chain. Every entry point starts here.

    THE CHAIN IS VERIFIED BEFORE IT IS READ, and that is not caution. A snapshot
    taken from a record somebody altered is a guess wearing the clothes of
    evidence, and it is the one answer this module must never hand back. It
    raises AuditTamperError rather than returning a flag: a caller that forgets
    to check a flag publishes the guess.

    The cost is a walk of the whole chain per call. That is the right cost for
    an operation somebody runs when a decision is disputed, and the wrong one
    for a request path -- so this belongs on an audit screen, not in a loop.
    """
    _require_scope(company_id)
    stamp = _require_moment(moment)
    verify_chain(session, company_id)
    return stamp, _chain(session, company_id)


def _pointer_targets(company_id: str, events: list[AuditEvent]) -> dict[int, AuditEvent]:
    """Map every reversal pointer in this chain to the row it names.

    A pointer that names a row outside this company's chain refuses to resolve,
    exactly as rollback.reverted_event refuses. Nothing in the schema stops one
    being written -- audit ids are unique across tenants and the foreign key
    does not care which company a row sits in -- and such a row hashes perfectly
    well, so verify_chain will not catch it. Reading it as a reversal would let
    another tenant's record decide what this one is put back to.
    """
    by_id = {event.id: event for event in events}
    targets: dict[int, AuditEvent] = {}
    for event in events:
        if event.reverts_event_id is None:
            continue
        target = by_id.get(event.reverts_event_id)
        if target is None:
            raise AuditTamperError(
                f"{company_id}: seq {event.seq} says it reverts event "
                f"{event.reverts_event_id}, which is not in this company's "
                "chain. Refusing to read it as a reversal."
            )
        targets[event.id] = target
    return targets


def _subject(
    subject_id: str, state: str, detail: str, event: AuditEvent | None = None
) -> SubjectState:
    """One subject's answer. No event means the chain has not spoken about it.

    Where there is no event there is no time, no seq and no actor, and each is
    left empty rather than filled with the nearest plausible value. A subject
    the chain did not name has nobody to name against it.
    """
    return SubjectState(
        subject_type=SUBJECT_ESCALATION,
        subject_id=subject_id,
        state=state,
        detail=detail,
        as_of_seq=None if event is None else event.seq,
        as_of=None if event is None else event.occurred_at,
        actor="" if event is None else event.actor,
    )


def state_at(session: Session, *, company_id: str, moment: datetime) -> Snapshot:
    """What the chain says was true at a moment. Reads only, and reads only the chain.

    This is the half an auditor asks for: what could the person who approved
    this see on the day they approved it. It answers from audit_events alone,
    because every other table holds today's values and would answer today's
    question in a confident voice.

    THREE ANSWERS, AND THE THIRD IS NOT THE SECOND. A subject is closed, or open
    because the chain records a decision being taken back, or NOT RECORDED --
    the chain says nothing about it at or before that moment. Not recorded is
    not open. An escalation is raised by the pipeline without an audit row
    naming it, so a subject the chain has not yet mentioned may have existed and
    been open, or may not have existed at all, and this module cannot tell them
    apart. It says so instead of choosing.

    A moment before the first row is BEFORE THE RECORD, which is again not the
    same as an empty company: before_the_record says the record had not begun,
    and first_event_at says when it did.

    Every escalation the chain ever names appears, including ones it only names
    later. An auditor asking about three o'clock has to be told the chain said
    nothing about that one then, rather than not being shown it.
    """
    stamp, events = _read(session, company_id, moment)
    targets = _pointer_targets(company_id, events)

    at_or_before = [event for event in events if event.occurred_at <= stamp]
    after = [event for event in events if event.occurred_at > stamp]
    tail = at_or_before[-1] if at_or_before else None

    states: dict[str, SubjectState] = {}
    for event in events:
        if event.subject_type != SUBJECT_ESCALATION:
            continue
        # Named first, answered second. Every escalation the chain mentions gets
        # an entry, and until a row at or before the moment moves it, that entry
        # says the chain did not speak about it -- which is not "open".
        states.setdefault(
            event.subject_id,
            _subject(event.subject_id, STATE_NOT_RECORDED, NOT_RECORDED_IS_NOT_OPEN),
        )
        if event.occurred_at > stamp:
            continue
        if (event.action, event.subject_type) in RESTORERS:
            states[event.subject_id] = _subject(
                event.subject_id,
                STATE_CLOSED,
                f"{event.action} at seq {event.seq}, and the chain records no "
                "row taking it back at or before this moment",
                event,
            )
        elif event.action == ACTION_DECISION_REVERTED:
            # There is no branch for a withdrawn reversal, and the omission is
            # the rule: rollback.py reinstates nothing when an undo is taken
            # back, so folding one into "closed" here would put a decision in
            # force that nobody re-made.
            target = targets.get(event.id)
            undone = f"seq {target.seq}" if target is not None else "an earlier decision"
            states[event.subject_id] = _subject(
                event.subject_id,
                STATE_OPEN,
                f"the closing decision at {undone} was taken back at seq "
                f"{event.seq}, so it was back in the queue",
                event,
            )

    return Snapshot(
        company_id=company_id,
        moment=stamp,
        before_the_record=not at_or_before,
        note=(
            BEFORE_THE_RECORD
            if not at_or_before
            else (
                f"read from {len(at_or_before)} row(s) recorded at or before "
                f"{stamp.isoformat()}"
            )
        ),
        first_event_at=min((e.occurred_at for e in events), default=None),
        events_counted=len(at_or_before),
        last_seq=None if tail is None else tail.seq,
        chain_head_at="" if tail is None else tail.entry_hash,
        subjects=tuple(states[key] for key in sorted(states)),
        cannot_speak_for=tuple(
            sorted({e.subject_type for e in events} - {SUBJECT_ESCALATION})
        ),
        clocks_out_of_order=bool(_out_of_order(events, after)),
    )


# ---------------------------------------------------------------------------
# Planning a restore
# ---------------------------------------------------------------------------


def _out_of_order(events: list[AuditEvent], after: list[AuditEvent]) -> list[AuditEvent]:
    """Rows stamped at or before the moment that were recorded after later ones.

    seq is the order the rows were recorded and the chain guarantees it.
    occurred_at is what the writer stamped, and callers may supply it. Where the
    two disagree across the moment, "everything after three o'clock" is not a
    suffix of the chain, and taking back a set out of the middle of a log is not
    a restore to a point in time. The read reports this; the write refuses on it.
    """
    if not after:
        return []
    later = {event.seq for event in after}
    cut = min(later)
    return [e for e in events if e.seq > cut and e.seq not in later]


def _plan(
    session: Session, company_id: str, moment
) -> tuple[list[AuditEvent], tuple[Blocker, ...], tuple[str, ...]]:
    """Work out what a restore would take back, step over, or refuse on.

    Read only, and shared by the dry run and the write so the two can never
    disagree about what would happen.

    The order of the checks below is the argument, so it is written out:

      1. A row in RECORDS_ONLY is stepped over with its reason. Nothing it did
         is state this module would put back.
      2. A row that reverts something is never taken back for its own sake. If
         it undid something that also happened after the moment, the pair
         cancels and both are left alone -- the state is already where the
         moment had it. If it undid something from at or before the moment,
         putting that back means re-deciding it, and that is refused.
      3. A row already taken back by some later row is left alone. Undoing it
         twice is what rollback.py refuses anyway, and the state is already back.
      4. A row RESTORERS knows how to reverse goes in the plan.
      5. ANYTHING ELSE BLOCKS. That is the safe default and the reason this
         function is trustworthy: forgetting to classify an action produces a
         loud refusal, never a quiet half-restore.
    """
    stamp, events = _read(session, company_id, moment)
    targets = _pointer_targets(company_id, events)

    after = [event for event in events if event.occurred_at > stamp]
    after_ids = {event.id for event in after}
    taken_back = {
        event.reverts_event_id for event in events if event.reverts_event_id is not None
    }

    plan: list[AuditEvent] = []
    blockers: list[Blocker] = []
    stepped_over: dict[str, int] = {}

    def block(event: AuditEvent, why: str) -> None:
        blockers.append(
            Blocker(
                seq=event.seq,
                event_id=event.id,
                action=event.action,
                subject_type=event.subject_type,
                subject_id=event.subject_id,
                occurred_at=event.occurred_at,
                why=why,
            )
        )

    for event in _out_of_order(events, after):
        block(event, WHY_CLOCKS_DISAGREE)

    for event in after:
        if event.action in RECORDS_ONLY:
            stepped_over[event.action] = stepped_over.get(event.action, 0) + 1
            continue
        if event.reverts_event_id is not None:
            target = targets.get(event.id)
            if target is not None and target.id in after_ids:
                continue
            block(event, WHY_FRESH_DECISION)
            continue
        if event.id in taken_back:
            continue
        if (event.action, event.subject_type) in RESTORERS:
            plan.append(event)
            continue
        block(event, WHY_NOTHING_PUTS_IT_BACK)

    left_alone = tuple(
        f"{count} {action} row(s) left as they are: {RECORDS_ONLY[action]}"
        for action, count in sorted(stepped_over.items())
    )
    # In the order they were recorded, so the refusal reads as the log reads.
    return plan, tuple(sorted(blockers, key=lambda blocker: blocker.seq)), left_alone


def what_stands_in_the_way(
    session: Session, *, company_id: str, moment: datetime
) -> tuple[Blocker, ...]:
    """The dry run: what restore_to would refuse on, without writing anything.

    Worth having on its own. A person deciding whether to restore needs to see
    the list before they commit to it, and the answer to "why can I not do this"
    is a list of rows rather than a sentence.
    """
    _, blockers, _ = _plan(session, company_id, moment)
    return blockers


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def _refusal(blockers: tuple[Blocker, ...]) -> str:
    lines = [CANNOT_RESTORE + "."]
    for blocker in blockers:
        lines.append(
            f"  seq {blocker.seq}: {blocker.action} on "
            f"{blocker.subject_type} {blocker.subject_id} at "
            f"{blocker.occurred_at.isoformat()} -- {blocker.why}"
        )
    lines.append("Put back from the chain: " + "; ".join(COVERED) + ".")
    lines.append("Not put back, in any table: " + ", ".join(NOT_COVERED) + ".")
    return "\n".join(lines)


def restore_to(
    session: Session,
    *,
    company_id: str,
    moment: datetime,
    actor: str,
    actor_kind: str,
    reason: str,
    actor_user_id: str | None = None,
    session_id: str | None = None,
    ip: str | None = None,
) -> Restoration:
    """Take back every decision the chain records after a moment. Appends only.

    Newest first, one reversal row each, through revert_event -- so a restore
    cannot do anything a single undo could not, and every guard in rollback.py
    holds over every step of it. Nothing is deleted, nothing is resequenced, and
    verify_chain passes over the whole log afterwards including the rows this
    wrote.

    IT REFUSES WHOLE OR IT WRITES WHOLE. Every check runs before the first row
    is written. If anything after the moment cannot be put back, this raises
    UnreplayableError with the list and writes nothing at all -- not even the
    part it did understand.

    actor_kind has no default, matching revert_event, and for its reason: a
    default of "user" quietly claims a person made this judgement, a default of
    "system" quietly claims nobody was answerable for it. A machine may restore
    -- a migration correcting a batch is a real case -- but it has to say so.

    NOTHING TO DO WRITES NOTHING. A moment with no decisions after it leaves the
    log alone rather than appending a row saying a restore changed nothing. The
    log records decisions and the state they moved; a row recording neither is
    noise in the one table that must stay readable.

    Call it inside session_scope. It moves state and appends rows several times
    over, and a failure between two of those has to abort all of them.
    """
    _require_scope(company_id)
    stamp = _require_moment(moment)

    note = " ".join((reason or "").split())
    if not note:
        raise ValueError(REASON_REQUIRED)
    if not actor:
        raise ValueError(ACTOR_REQUIRED)

    plan, blockers, left_alone = _plan(session, company_id, stamp)
    if blockers:
        raise UnreplayableError(_refusal(blockers), blockers)

    reversals = []
    for event in reversed(plan):
        reversals.append(
            revert_event(
                session,
                company_id=company_id,
                event_id=event.id,
                actor=actor,
                actor_kind=actor_kind,
                # The moment goes in the row's own reason. It is the only thing
                # tying these rows together afterwards, because a restore writes
                # no summary row -- see this module's docstring.
                reason=f"restoring to {stamp.isoformat()}: {note}",
                actor_user_id=actor_user_id,
                session_id=session_id,
                ip=ip,
            )
        )

    if reversals:
        outcome = (
            f"took back {len(reversals)} decision(s) recorded after "
            f"{stamp.isoformat()}"
        )
    else:
        outcome = (
            f"nothing recorded after {stamp.isoformat()} could be taken back, so "
            "no row was written"
        )

    return Restoration(
        company_id=company_id,
        moment=stamp,
        reversals=tuple(reversals),
        left_alone=left_alone,
        outcome=outcome,
    )


__all__ = [
    "BEFORE_THE_RECORD",
    "CANNOT_RESTORE",
    "COVERED",
    "NAIVE_MOMENT",
    "NOT_COVERED",
    "NOT_RECORDED_IS_NOT_OPEN",
    "RECORDS_ONLY",
    "STATE_CLOSED",
    "STATE_NOT_RECORDED",
    "STATE_OPEN",
    "WHY_CLOCKS_DISAGREE",
    "WHY_FRESH_DECISION",
    "WHY_NOTHING_PUTS_IT_BACK",
    "Blocker",
    "Restoration",
    "Snapshot",
    "SubjectState",
    "UnreplayableError",
    "restore_to",
    "state_at",
    "what_stands_in_the_way",
]
