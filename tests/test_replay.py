"""What the record said on the day, and what putting it back may and may not do.

Two halves under test, and they carry different risk, so they are tested apart.

  READING. state_at() answers "what did this look like at three o'clock on
  Tuesday" from the chain alone. It writes nothing. Its hardest case is the one
  everybody gets wrong: a moment before the first row is not an empty company,
  it is a moment before the record begins, and an escalation the chain has not
  yet named is not an open one. Those are three different answers and this file
  pins all three.

  WRITING. restore_to() takes back the decisions recorded after a moment, one
  reversal row each, through app/state/rollback.py. The chain is never rewound,
  never truncated, never rewritten -- so verify_chain() must pass over the whole
  log INCLUDING the restore, and that is asserted rather than assumed.

  REFUSING. A decision the chain cannot put back stops the whole restore, with
  the list. A partial restore presented as a restore is the worst outcome
  available here, so there is a test that a mixed set writes nothing at all.

  AND THE ONE THAT MATTERS MOST. No restore may make an unverified claim assert
  itself. Putting an escalation back in the queue changes nothing whatever about
  what the source says. There is a test below that reads the module's own source
  to keep a later edit from blurring it.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTION_DECISION_REVERTED,
    ACTION_ESCALATION_APPROVED,
    ACTION_ESCALATION_REJECTED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_REVERSAL_WITHDRAWN,
    ACTOR_SYSTEM,
    ACTOR_USER,
    DIGEST_V3,
    AuditTamperError,
    _digest_v3,
    chain_head,
    event_count,
    record_event,
    verify_chain,
)
from app.state.claims import REASON_CODE_QUOTE_MISMATCH, verified_claims
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent, Change, Claim, Escalation, Proceeding
from app.state.replay import (
    BEFORE_THE_RECORD,
    COVERED,
    NOT_COVERED,
    NOT_RECORDED_IS_NOT_OPEN,
    RECORDS_ONLY,
    STATE_CLOSED,
    STATE_NOT_RECORDED,
    STATE_OPEN,
    WHY_CLOCKS_DISAGREE,
    WHY_FRESH_DECISION,
    WHY_NOTHING_PUTS_IT_BACK,
    UnreplayableError,
    restore_to,
    state_at,
    what_stands_in_the_way,
)
from app.state.rollback import ACTOR_REQUIRED, REASON_REQUIRED, revert_event
from app.ingestion.ingest import ingest_version


# ---------------------------------------------------------------------------
# A day, in hours, so every test can say when it means.
# ---------------------------------------------------------------------------

DAY = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _at(hour: int) -> datetime:
    return DAY + timedelta(hours=hour)


NINE = _at(9)  # before anything happened
TEN = _at(10)
ELEVEN = _at(11)
NOON = _at(12)
ONE = _at(13)
TWO = _at(14)


# ---------------------------------------------------------------------------
# One company with a real graph: a source, a change, a claim whose quote does
# not match it, and the escalation that refusal raised. Mirrored from
# tests/test_rollback.py rather than imported -- one test file reaching into
# another's private helpers ties two suites together for no gain.
# ---------------------------------------------------------------------------

DEFINITION = (
    '"Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW)."
)
SOURCE = "SECTION 2. DEFINITIONS\n\n2.1 " + DEFINITION + "\n"
QUOTE_START = SOURCE.index(DEFINITION)
QUOTE_END = QUOTE_START + len(DEFINITION)
MISQUOTE = DEFINITION.replace("20 megawatts", "10 megawatts")


def _seed(session, company_id: str, *, escalations: int = 1) -> list[str]:
    """A company whose claim fails verification. Returns the escalation ids."""
    prefix = company_id.lower()
    ingest_version(
        session,
        version_id=f"{prefix}-v1",
        company_id=company_id,
        docket=f"{company_id}-2026-0142",
        label="NOPR",
        status="DRAFT",
        source_text=SOURCE,
    )
    session.add(
        Proceeding(
            id=f"{prefix}-proc",
            company_id=company_id,
            docket=f"{company_id}-2026-0142",
            commission="Public Utilities Commission",
            subject=f"{company_id} large load interconnection",
        )
    )
    session.add(
        Change(
            id=f"{prefix}-chg",
            company_id=company_id,
            proceeding_id=f"{prefix}-proc",
            from_version_id=f"{prefix}-v1",
            to_version_id=f"{prefix}-v1",
            change_type="modified",
            before_start=QUOTE_START,
            before_end=QUOTE_END,
            after_start=QUOTE_START,
            after_end=QUOTE_END,
            section="2.1",
            alignment_confidence=0.94,
            materiality=None,
            status="DRAFT",
        )
    )
    session.add(
        Claim(
            id=f"{prefix}-claim",
            company_id=company_id,
            change_id=f"{prefix}-chg",
            statement=f"{company_id} threshold sits at 20 MW.",
            citation_version_id=f"{prefix}-v1",
            citation_start=QUOTE_START,
            citation_end=QUOTE_END,
            citation_quote=MISQUOTE,
            cited_occurrence=None,
            confidence_bp=9200,
        )
    )
    ids = []
    for number in range(1, escalations + 1):
        escalation_id = f"{prefix}-esc-{number}"
        session.add(
            Escalation(
                id=escalation_id,
                company_id=company_id,
                claim_id=f"{prefix}-claim",
                reason_code=REASON_CODE_QUOTE_MISMATCH,
                reason_text="quoted text does not match the source",
                detail=f"{company_id} confidential detail",
            )
        )
        ids.append(escalation_id)
    session.flush()
    return ids


def _escalation(session, company_id: str, escalation_id: str) -> Escalation:
    return (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one()
    )


def _close(
    session,
    company_id: str,
    escalation_id: str,
    *,
    at: datetime,
    action: str = ACTION_ESCALATION_APPROVED,
    actor: str = "ann@mep.example",
) -> AuditEvent:
    """Close an escalation the way the review queue closes one, at a stated time."""
    row = _escalation(session, company_id, escalation_id)
    row.resolved_at = at
    row.resolved_by = actor
    session.flush()
    return record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type="escalation",
        subject_id=escalation_id,
        reason=f"refusal upheld for {row.claim_id}: {row.reason_code}",
        occurred_at=at,
        actor_kind=ACTOR_USER,
        actor_user_id="USR-ANN",
    )


def _reopen(session, company_id: str, closing: AuditEvent, *, at: datetime) -> AuditEvent:
    """Reopen an escalation at a stated hour, the way revert_event reopens one.

    Mirrored rather than called because revert_event stamps its row "now", and
    the reader under test here has to be asked about an afternoon. The row is
    the same in every other respect -- same action, same pointer, scheme 3 --
    and it is written through record_event, so the chain verifies over it. The
    restore tests below use the real revert_event.
    """
    row = _escalation(session, company_id, closing.subject_id)
    row.resolved_at = None
    row.resolved_by = None
    session.flush()
    return record_event(
        session,
        company_id=company_id,
        actor="bob@mep.example",
        action=ACTION_DECISION_REVERTED,
        subject_type=closing.subject_type,
        subject_id=closing.subject_id,
        reason=f"reverses seq {closing.seq} ({closing.action}): approved the wrong one",
        occurred_at=at,
        actor_kind=ACTOR_USER,
        actor_user_id="USR-BOB",
        reverts_event_id=closing.id,
    )


def _withdraw(
    session, company_id: str, reversal: AuditEvent, *, at: datetime
) -> AuditEvent:
    """Take back a reversal at a stated hour. Reinstates nothing, by contract."""
    return record_event(
        session,
        company_id=company_id,
        actor="bob@mep.example",
        action=ACTION_REVERSAL_WITHDRAWN,
        subject_type=reversal.subject_type,
        subject_id=reversal.subject_id,
        reason=f"withdraws the reversal at seq {reversal.seq}; nothing is reinstated",
        occurred_at=at,
        actor_kind=ACTOR_USER,
        actor_user_id="USR-BOB",
        reverts_event_id=reversal.id,
    )


def _login(session, company_id: str, *, at: datetime) -> AuditEvent:
    return record_event(
        session,
        company_id=company_id,
        actor="ann@mep.example",
        action=ACTION_LOGIN_SUCCEEDED,
        subject_type="user",
        subject_id="USR-ANN",
        reason="password accepted",
        occurred_at=at,
        actor_kind=ACTOR_USER,
        actor_user_id="USR-ANN",
    )


def _registered_a_source(session, company_id: str, *, at: datetime) -> AuditEvent:
    """A state change in a table this module does not cover. Blocks a restore."""
    return record_event(
        session,
        company_id=company_id,
        actor="ann@mep.example",
        action="source.registered",
        subject_type="source",
        subject_id="SRC-1",
        reason="added the commission's docket feed",
        occurred_at=at,
        actor_kind=ACTOR_USER,
        actor_user_id="USR-ANN",
    )


def _revert(session, company_id, event_id, *, reason="approved the wrong one"):
    return revert_event(
        session,
        company_id=company_id,
        event_id=event_id,
        actor="bob@mep.example",
        actor_kind=ACTOR_USER,
        reason=reason,
        actor_user_id="USR-BOB",
    )


def _restore(session, company_id, moment, *, reason="restoring the wrong afternoon"):
    return restore_to(
        session,
        company_id=company_id,
        moment=moment,
        actor="bob@mep.example",
        actor_kind=ACTOR_USER,
        reason=reason,
        actor_user_id="USR-BOB",
    )


def _rows(session, company_id="MEP") -> list[AuditEvent]:
    return (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
        )
        .scalars()
        .all()
    )


def _overwrite(session, entry_id: int, **values) -> None:
    session.execute(
        AuditEvent.__table__.update().where(AuditEvent.id == entry_id).values(**values)
    )
    session.commit()
    session.expire_all()


def _state_of(snapshot, subject_id: str):
    for subject in snapshot.subjects:
        if subject.subject_id == subject_id:
            return subject
    raise AssertionError(f"{subject_id} is not in the snapshot at all")


# ---------------------------------------------------------------------------
# 1. Reading: what the chain says was true then
# ---------------------------------------------------------------------------


def test_the_snapshot_says_what_the_chain_said_at_that_moment():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN, actor="ann@mep.example")
        session.commit()

        before = state_at(session, company_id="MEP", moment=TEN)
        after = state_at(session, company_id="MEP", moment=NOON)

        assert _state_of(before, escalation_id).state == STATE_NOT_RECORDED
        closed = _state_of(after, escalation_id)
        assert closed.state == STATE_CLOSED
        assert closed.actor == "ann@mep.example"
        assert closed.as_of == ELEVEN
        assert ACTION_ESCALATION_APPROVED in closed.detail


def test_a_reopening_is_visible_from_the_moment_it_was_recorded_and_not_before():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        _reopen(session, "MEP", closing, at=ONE)
        session.commit()
        assert verify_chain(session, "MEP") is True

        assert _state_of(
            state_at(session, company_id="MEP", moment=NOON), escalation_id
        ).state == STATE_CLOSED
        reopened = _state_of(
            state_at(session, company_id="MEP", moment=TWO), escalation_id
        )
        assert reopened.state == STATE_OPEN
        assert str(closing.seq) in reopened.detail


def test_withdrawing_a_reversal_moves_no_state_in_the_snapshot_either():
    # app/state/rollback.py reinstates nothing when a reversal is taken back.
    # A snapshot that folded the withdrawal back into "closed" would put a
    # decision in force that nobody re-made.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=TEN)
        reversal = _reopen(session, "MEP", closing, at=ELEVEN)
        _withdraw(session, "MEP", reversal, at=NOON)
        session.commit()
        assert verify_chain(session, "MEP") is True

        snapshot = state_at(session, company_id="MEP", moment=TWO)
        assert _state_of(snapshot, escalation_id).state == STATE_OPEN


def test_a_moment_before_the_first_row_is_before_the_record_and_says_so():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        early = state_at(session, company_id="MEP", moment=NINE)
        assert early.before_the_record is True
        assert early.last_seq is None
        assert early.chain_head_at == ""
        assert early.events_counted == 0
        assert early.first_event_at is not None and early.first_event_at > NINE


def test_the_snapshot_carries_the_words_for_its_own_standing():
    # The distinction has to survive the trip to a screen. A template handed a
    # bare False for before_the_record will render "no events" and the auditor
    # will read "nothing happened", which is a different claim entirely.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        assert state_at(session, company_id="MEP", moment=NINE).note == BEFORE_THE_RECORD
        inside = state_at(session, company_id="MEP", moment=NOON)
        assert inside.note != BEFORE_THE_RECORD
        assert str(inside.events_counted) in inside.note


def test_before_the_record_and_not_recorded_are_different_answers():
    # The distinction this module exists to keep. At ten the record has begun
    # and says nothing about this escalation; at nine the record has not begun.
    # Neither answer is "it was open".
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _login(session, "MEP", at=TEN)
        _close(session, "MEP", escalation_id, at=NOON)
        session.commit()

        inside = state_at(session, company_id="MEP", moment=ELEVEN)
        outside = state_at(session, company_id="MEP", moment=NINE)

        assert inside.before_the_record is False
        assert outside.before_the_record is True
        for snapshot in (inside, outside):
            subject = _state_of(snapshot, escalation_id)
            assert subject.state == STATE_NOT_RECORDED
            assert subject.state != STATE_OPEN
            assert NOT_RECORDED_IS_NOT_OPEN in subject.detail


def test_the_snapshot_names_every_escalation_the_chain_ever_mentions():
    # Including ones it only mentions later. An auditor asking about three
    # o'clock has to be told that the chain said nothing about ESC-2 then,
    # rather than not being shown ESC-2 at all.
    init_db()
    with session_scope() as session:
        first, second = _seed(session, "MEP", escalations=2)
        _close(session, "MEP", first, at=TEN)
        _close(session, "MEP", second, at=ONE)
        session.commit()

        snapshot = state_at(session, company_id="MEP", moment=ELEVEN)
        assert {s.subject_id for s in snapshot.subjects} == {first, second}
        assert _state_of(snapshot, first).state == STATE_CLOSED
        assert _state_of(snapshot, second).state == STATE_NOT_RECORDED


def test_the_snapshot_pins_itself_to_the_chain_head_of_that_moment():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()
        head_then = chain_head(session, "MEP")

        _login(session, "MEP", at=ONE)
        session.commit()

        snapshot = state_at(session, company_id="MEP", moment=NOON)
        assert snapshot.chain_head_at == head_then
        assert snapshot.last_seq == closing.seq
        assert snapshot.chain_head_at != chain_head(session, "MEP")


def test_the_snapshot_carries_the_list_of_what_it_cannot_speak_for():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=TEN)
        _registered_a_source(session, "MEP", at=ELEVEN)
        session.commit()

        snapshot = state_at(session, company_id="MEP", moment=NOON)
        assert "source" in snapshot.cannot_speak_for
        assert "escalation" not in snapshot.cannot_speak_for
        assert snapshot.covers == COVERED
        assert snapshot.does_not_cover == NOT_COVERED
        # The two lists are not decoration: escalations is in both, because two
        # of its columns can be put back and the rest of the row cannot.
        assert any("escalations.resolved_at" in line for line in COVERED)
        assert "escalations" in NOT_COVERED and "claims" in NOT_COVERED
        assert "audit_events" not in NOT_COVERED


def test_reading_the_past_writes_nothing():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()
        before = (event_count(session, "MEP"), chain_head(session, "MEP"))

        state_at(session, company_id="MEP", moment=NOON)
        what_stands_in_the_way(session, company_id="MEP", moment=TEN)

        assert not session.new and not session.dirty and not session.deleted
        assert (event_count(session, "MEP"), chain_head(session, "MEP")) == before
        assert _escalation(session, "MEP", escalation_id).resolved_at == ELEVEN


def test_one_tenants_snapshot_says_nothing_about_another():
    init_db()
    with session_scope() as session:
        ours = _seed(session, "MEP")[0]
        theirs = _seed(session, "RIVAL")[0]
        _close(session, "MEP", ours, at=TEN)
        _close(session, "RIVAL", theirs, at=TEN)
        session.commit()

        snapshot = state_at(session, company_id="MEP", moment=NOON)
        assert {s.subject_id for s in snapshot.subjects} == {ours}


def test_a_snapshot_of_a_chain_that_does_not_verify_is_refused():
    # A snapshot of a record that was altered is a guess wearing the clothes of
    # evidence. It is the one answer this module must never give.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        _overwrite(session, closing.id, reason="something else entirely")
        with pytest.raises(AuditTamperError):
            state_at(session, company_id="MEP", moment=NOON)
        with pytest.raises(AuditTamperError):
            _restore(session, "MEP", TEN)


def test_a_pointer_into_another_tenants_chain_is_refused_though_it_verifies():
    # The gap the hash does not close. audit.py says it plainly: anyone who can
    # write the database can recompute every hash from the tampered record
    # forward. So a row that reverts ANOTHER company's event can be written to
    # hash perfectly, and verify_chain will pass over it. Reading that pointer
    # would let one tenant's log decide what another is put back to, so the
    # reader refuses it rather than resolving it.
    init_db()
    with session_scope() as session:
        ours = _seed(session, "MEP")[0]
        theirs = _seed(session, "RIVAL")[0]
        _close(session, "MEP", ours, at=TEN)
        stolen = _close(session, "RIVAL", theirs, at=TEN)
        session.commit()

        tail = _rows(session, "MEP")[-1]
        fields = {
            "prev_hash": tail.entry_hash,
            "seq": tail.seq + 1,
            "company_id": "MEP",
            "actor": "bob@mep.example",
            "action": ACTION_DECISION_REVERTED,
            "subject_type": "escalation",
            "subject_id": ours,
            "reason": "reverses a row that is not ours",
            "citation": "",
            "occurred_at": ELEVEN,
            "actor_user_id": "USR-BOB",
            "actor_kind": ACTOR_USER,
            "session_id": None,
            "ip": None,
        }
        session.add(
            AuditEvent(
                **fields,
                digest_version=DIGEST_V3,
                entry_hash=_digest_v3(**fields, reverts_event_id=stolen.id),
                reverts_event_id=stolen.id,
            )
        )
        session.commit()

        # The chain is sound by every measure it has. That is the point.
        assert verify_chain(session, "MEP") is True
        for call in (state_at, what_stands_in_the_way):
            with pytest.raises(AuditTamperError) as caught:
                call(session, company_id="MEP", moment=NOON)
            assert str(stolen.id) in str(caught.value)


@pytest.mark.parametrize("scope", ("", None))
def test_an_unscoped_read_or_restore_is_refused_rather_than_answered(scope):
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        session.commit()

        for call in (state_at, what_stands_in_the_way):
            with pytest.raises(ValueError) as caught:
                call(session, company_id=scope, moment=NOON)
            assert "company_id" in str(caught.value)

        with pytest.raises(ValueError) as caught:
            restore_to(
                session,
                company_id=scope,
                moment=NOON,
                actor="bob@mep.example",
                actor_kind=ACTOR_USER,
                reason="no scope",
            )
        assert "company_id" in str(caught.value)


@pytest.mark.parametrize(
    "moment", (datetime(2026, 8, 3, 12, 0, 0), "2026-08-03T12:00:00+00:00", None)
)
def test_a_moment_with_no_timezone_is_refused_rather_than_assumed(moment):
    # "Three o'clock" where? A naive stamp compared against an aware log either
    # raises or silently means a different hour depending on the host.
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        session.commit()

        with pytest.raises(ValueError) as caught:
            state_at(session, company_id="MEP", moment=moment)
        assert "timezone" in str(caught.value) or "moment" in str(caught.value)


# ---------------------------------------------------------------------------
# 2. Writing: putting it back, without rewinding anything
# ---------------------------------------------------------------------------


def test_restoring_takes_the_decision_back_and_the_chain_still_verifies():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()
        count = event_count(session, "MEP")

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert len(result.reversals) == 1
        written = result.reversals[0].event
        assert written.action == ACTION_DECISION_REVERTED
        assert written.reverts_event_id == closing.id
        assert event_count(session, "MEP") == count + 1
        assert _escalation(session, "MEP", escalation_id).resolved_at is None
        # The whole log, including the restore itself.
        assert verify_chain(session, "MEP") is True


def test_the_chain_is_never_rewound_truncated_or_rewritten():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()
        before = [
            (row.seq, row.action, row.reason, row.entry_hash) for row in _rows(session)
        ]

        _restore(session, "MEP", TEN)
        session.commit()

        after = [
            (row.seq, row.action, row.reason, row.entry_hash) for row in _rows(session)
        ]
        # Every row that was there is there, unchanged, in the same order.
        assert after[: len(before)] == before
        assert len(after) == len(before) + 1
        assert session.get(AuditEvent, closing.id).entry_hash == closing.entry_hash
        assert verify_chain(session, "MEP") is True


def test_the_rows_a_restore_writes_name_the_moment_they_were_restoring_to():
    # A restore writes no summary row of its own -- there is no action code for
    # one -- so the only way back to "these four reversals were one restore" is
    # the reason on each of them.
    init_db()
    with session_scope() as session:
        first, second = _seed(session, "MEP", escalations=2)
        _close(session, "MEP", first, at=ELEVEN)
        _close(session, "MEP", second, at=NOON, action=ACTION_ESCALATION_REJECTED)
        session.commit()

        result = _restore(session, "MEP", TEN, reason="wrong docket approved")
        session.commit()

        assert len(result.reversals) == 2
        for reversal in result.reversals:
            assert TEN.isoformat() in reversal.event.reason
            assert "wrong docket approved" in reversal.event.reason
        assert verify_chain(session, "MEP") is True


def test_decisions_are_taken_back_newest_first():
    init_db()
    with session_scope() as session:
        first, second = _seed(session, "MEP", escalations=2)
        early = _close(session, "MEP", first, at=ELEVEN)
        late = _close(session, "MEP", second, at=NOON)
        session.commit()

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert [r.reverted_event_id for r in result.reversals] == [late.id, early.id]


def test_restoring_to_a_moment_with_nothing_after_it_writes_nothing():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()
        count = event_count(session, "MEP")

        result = _restore(session, "MEP", TWO)
        session.commit()

        assert result.reversals == ()
        assert event_count(session, "MEP") == count
        assert _escalation(session, "MEP", escalation_id).resolved_at == ELEVEN
        assert "nothing" in result.outcome.lower()


def test_restoring_twice_to_the_same_moment_is_the_second_time_a_no_op():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        _restore(session, "MEP", TEN)
        session.commit()
        count = event_count(session, "MEP")

        again = _restore(session, "MEP", TEN)
        session.commit()

        assert again.reversals == ()
        assert event_count(session, "MEP") == count
        assert verify_chain(session, "MEP") is True


def test_a_decision_already_taken_back_is_left_alone():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        _revert(session, "MEP", closing.id)
        session.commit()
        count = event_count(session, "MEP")

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert result.reversals == ()
        assert event_count(session, "MEP") == count


def test_a_restore_leaves_the_other_tenant_untouched():
    init_db()
    with session_scope() as session:
        ours = _seed(session, "MEP")[0]
        theirs = _seed(session, "RIVAL")[0]
        _close(session, "MEP", ours, at=ELEVEN)
        _close(session, "RIVAL", theirs, at=ELEVEN)
        session.commit()

        _restore(session, "MEP", TEN)
        session.commit()

        assert _escalation(session, "RIVAL", theirs).resolved_at == ELEVEN
        assert event_count(session, "RIVAL") == 1
        assert verify_chain(session, "RIVAL") is True


def test_a_restore_needs_an_actor_a_reason_and_a_stated_actor_kind():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        with pytest.raises(ValueError) as no_reason:
            restore_to(
                session,
                company_id="MEP",
                moment=TEN,
                actor="bob@mep.example",
                actor_kind=ACTOR_USER,
                reason="   ",
            )
        assert REASON_REQUIRED in str(no_reason.value)

        with pytest.raises(ValueError) as no_actor:
            restore_to(
                session,
                company_id="MEP",
                moment=TEN,
                actor="",
                actor_kind=ACTOR_USER,
                reason="a reason",
            )
        assert ACTOR_REQUIRED in str(no_actor.value)

        # No default for actor_kind, exactly as revert_event has none: a default
        # of "user" claims a person, a default of "system" claims nobody.
        with pytest.raises(TypeError):
            restore_to(
                session,
                company_id="MEP",
                moment=TEN,
                actor="batch-fixer",
                reason="corrected in bulk",
            )

        assert _escalation(session, "MEP", escalation_id).resolved_at == ELEVEN


def test_a_machine_may_restore_but_has_to_say_it_is_one():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        session.commit()

        result = restore_to(
            session,
            company_id="MEP",
            moment=TEN,
            actor="batch-fixer",
            actor_kind=ACTOR_SYSTEM,
            reason="corrected in bulk",
        )
        session.commit()

        assert result.reversals[0].event.actor_kind == ACTOR_SYSTEM
        assert result.reversals[0].event.actor_user_id is None
        assert verify_chain(session, "MEP") is True


# ---------------------------------------------------------------------------
# 3. Refusing: the half it does not know is never restored quietly
# ---------------------------------------------------------------------------


def test_a_decision_the_chain_cannot_put_back_refuses_the_whole_restore():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        registered = _registered_a_source(session, "MEP", at=ELEVEN)
        session.commit()
        count = event_count(session, "MEP")

        with pytest.raises(UnreplayableError) as caught:
            _restore(session, "MEP", TEN)

        blockers = caught.value.blockers
        assert [b.event_id for b in blockers] == [registered.id]
        assert blockers[0].why == WHY_NOTHING_PUTS_IT_BACK
        assert "source.registered" in str(caught.value)
        session.rollback()
        assert event_count(session, "MEP") == count


def test_a_partial_restore_is_never_written():
    # The worst outcome available here: half a restore, reported as a restore.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        _registered_a_source(session, "MEP", at=NOON)
        session.commit()
        count = event_count(session, "MEP")

        with pytest.raises(UnreplayableError):
            _restore(session, "MEP", TEN)
        session.rollback()

        # The half it did know how to undo was not undone either.
        assert _escalation(session, "MEP", escalation_id).resolved_at == ELEVEN
        assert event_count(session, "MEP") == count
        assert verify_chain(session, "MEP") is True


def test_the_refusal_names_the_tables_it_covers_and_the_ones_it_does_not():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        _registered_a_source(session, "MEP", at=ELEVEN)
        session.commit()

        with pytest.raises(UnreplayableError) as caught:
            _restore(session, "MEP", TEN)

        message = str(caught.value)
        assert "escalations.resolved_at" in message
        assert "sources" in message


def test_it_will_not_re_make_a_decision_somebody_took_back():
    # At eleven the escalation was closed. Somebody reopened it at one. Putting
    # it back closed would mean writing a resolver's name onto a judgement
    # nobody made -- the rule app/state/rollback.py already refuses to break.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=TEN)
        _reopen(session, "MEP", closing, at=ONE)
        session.commit()
        count = event_count(session, "MEP")

        with pytest.raises(UnreplayableError) as caught:
            _restore(session, "MEP", NOON)

        assert [b.why for b in caught.value.blockers] == [WHY_FRESH_DECISION]
        session.rollback()
        assert event_count(session, "MEP") == count
        assert _escalation(session, "MEP", escalation_id).resolved_at is None


def test_a_decision_and_its_undo_after_the_moment_cancel_each_other():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        closing = _close(session, "MEP", escalation_id, at=ELEVEN)
        _revert(session, "MEP", closing.id)
        session.commit()
        count = event_count(session, "MEP")

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert result.reversals == ()
        assert what_stands_in_the_way(session, company_id="MEP", moment=TEN) == ()
        assert event_count(session, "MEP") == count


def test_a_log_whose_clocks_and_order_disagree_refuses_a_cut_by_time():
    # The rows say one thing about when, the chain says another about what
    # order. "Everything after three o'clock" is then not a clean cut of the
    # chain, and taking back a chunk out of the middle is not a restore.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=TWO)
        _login(session, "MEP", at=TEN)
        session.commit()

        blockers = what_stands_in_the_way(session, company_id="MEP", moment=NOON)
        assert [b.why for b in blockers] == [WHY_CLOCKS_DISAGREE]
        with pytest.raises(UnreplayableError):
            _restore(session, "MEP", NOON)

        # The read still answers, and says so on the snapshot rather than
        # refusing: a warning a caller can see beats a wall.
        snapshot = state_at(session, company_id="MEP", moment=NOON)
        assert snapshot.clocks_out_of_order is True


def test_what_stands_in_the_way_lists_what_restore_would_refuse_on():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        registered = _registered_a_source(session, "MEP", at=ELEVEN)
        session.commit()

        listed = what_stands_in_the_way(session, company_id="MEP", moment=TEN)
        with pytest.raises(UnreplayableError) as caught:
            _restore(session, "MEP", TEN)

        assert [b.event_id for b in listed] == [registered.id]
        assert [b.event_id for b in caught.value.blockers] == [b.event_id for b in listed]
        assert what_stands_in_the_way(session, company_id="MEP", moment=TWO) == ()


# ---------------------------------------------------------------------------
# 4. What it deliberately leaves alone, said out loud
# ---------------------------------------------------------------------------


def test_a_sign_in_after_the_moment_does_not_block_and_is_named_as_left_alone():
    # Sessions are not restored on purpose: handing back a session somebody
    # signed out of would hand back access. A restore that quietly skipped them
    # would be a fallback that did not announce itself.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        _login(session, "MEP", at=NOON)
        session.commit()

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert len(result.reversals) == 1
        assert result.left_alone, "a restore that skipped a row said nothing about it"
        assert any(ACTION_LOGIN_SUCCEEDED in line for line in result.left_alone)
        assert _escalation(session, "MEP", escalation_id).resolved_at is None


def test_every_action_left_alone_carries_the_reason_it_is_left_alone():
    for action, why in RECORDS_ONLY.items():
        assert isinstance(action, str) and action
        assert isinstance(why, str) and len(why.split()) > 3, (
            f"{action} is skipped by every restore with no reason written down"
        )
    for action in (ACTION_LOGIN_SUCCEEDED, ACTION_ACCESS_DENIED, ACTION_REVERSAL_WITHDRAWN):
        assert action in RECORDS_ONLY


def test_the_dry_run_and_the_restore_agree_about_what_is_left_alone():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        _close(session, "MEP", escalation_id, at=ELEVEN)
        _login(session, "MEP", at=NOON)
        session.commit()

        result = _restore(session, "MEP", TEN)
        session.commit()

        assert result.outcome
        assert str(len(result.reversals)) in result.outcome


# ---------------------------------------------------------------------------
# 5. The one that matters most
# ---------------------------------------------------------------------------

_THE_SENTENCE = (
    "NOTHING here may turn a withheld claim into an assertion -- only the "
    "source agreeing with the quote does that."
)


def test_the_module_says_in_its_own_docstring_what_it_cannot_do():
    from app.state import replay

    assert _THE_SENTENCE in " ".join(replay.__doc__.split())


def test_restoring_leaves_every_withheld_claim_exactly_as_withheld():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")[0]
        before_verified, before_withheld = verified_claims(session, "MEP", "mep-chg")
        assert (len(before_verified), len(before_withheld)) == (0, 1)

        _close(session, "MEP", escalation_id, at=ELEVEN)
        _restore(session, "MEP", TEN)
        session.commit()

        after_verified, after_withheld = verified_claims(session, "MEP", "mep-chg")
        assert (len(after_verified), len(after_withheld)) == (0, 1)
        assert after_withheld[0].reason_code == before_withheld[0].reason_code
        assert not hasattr(after_withheld[0], "statement")


def test_the_module_never_touches_a_claim_or_a_source(repo_root):
    # Structural, because the failure it prevents is a helpful edit six months
    # from now rather than a bug today.
    source = (repo_root / "app" / "state" / "replay.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    body = body.split('"""', 2)[-1]
    for forbidden in ("Claim", "DocumentVersion", "Passage", "verified_claims"):
        assert forbidden not in body, (
            f"replay.py names {forbidden}. Putting state back is not re-opening "
            "the question of what the source says."
        )


def test_the_two_halves_stay_apart():
    # The read is the valuable half and it is safe. If state_at ever grows a
    # write, an auditor's question starts changing the record it asks about.
    import inspect

    from app.state import replay

    for name in ("state_at", "what_stands_in_the_way"):
        body = inspect.getsource(getattr(replay, name))
        for forbidden in ("record_event", "revert_event", "session.add", "session.flush"):
            assert forbidden not in body, f"{name} writes: it names {forbidden}"
