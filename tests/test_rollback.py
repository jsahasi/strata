"""Undo, as a regulated product has to spell it: a new row, never an erasure.

Four things are under test here, and the first one is the one that matters most.

  1. A reversal cannot make a citation verify. Reverting the approval of a
     refusal reopens the escalation and leaves the claim exactly as withheld as
     it was. Nothing in app/state/rollback.py may turn a withheld claim into an
     assertion, and there is a test below that reads the module's own source to
     keep it that way.
  2. The reversal is a row. The original is not edited, not deleted, not
     flagged. The chain verifies over both.
  3. The pointer is inside the digest. Re-pointing a reversal at a different
     event turns the chain red instead of quietly rewriting who took what back.
     The scheme-2 rows that predate the pointer are refused if one appears on
     them, because scheme 2 does not cover it and reading it there would be
     trusting a field nothing hashed.
  4. Taking back a reversal does NOT reinstate the decision it reverted. It
     writes a third row, leaves the escalation open, and says so in plain words.
     Nothing here manufactures a judgement nobody made.

The v1 and v2 golden hashes at the top are the guard on all of it. If adding
scheme 3 moved either of them by a byte, every audit row already written would
stop verifying, and this file fails before anything else does.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.state.audit import (
    ACTION_ESCALATION_APPROVED,
    ACTION_ESCALATION_REJECTED,
    ACTION_ESCALATION_RESOLVED,
    ACTION_DECISION_REVERTED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_REVERSAL_WITHDRAWN,
    ACTOR_SYSTEM,
    ACTOR_USER,
    CURRENT_DIGEST_VERSION,
    DIGEST_V1,
    DIGEST_V2,
    DIGEST_V3,
    AuditTamperError,
    _digest,
    _digest_v2,
    event_count,
    record_event,
    verify_chain,
)
from app.state.claims import REASON_CODE_QUOTE_MISMATCH, verified_claims
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent, Change, Claim, Escalation, Proceeding
from app.state.rollback import (
    ACTOR_REQUIRED,
    ALREADY_REVERTED,
    NOTHING_TO_RESTORE,
    NOT_REINSTATED,
    NOT_REVERSIBLE,
    NO_SUCH_EVENT,
    REASON_REQUIRED,
    event_stands,
    reversal_of,
    reverted_event,
    revert_event,
)
from app.ingestion.ingest import ingest_version

# ---------------------------------------------------------------------------
# Frozen hashes. Both were computed by the code as it stood BEFORE scheme 3
# existed, over the fixed inputs below, and pasted here rather than recomputed.
# That is the only way this file can tell "the old schemes still work" from
# "the old schemes still agree with themselves".
# ---------------------------------------------------------------------------

_T1 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)

GOLDEN_V1 = "c19805f05ca233abca53728eb80b48f520d65b040ca81f2012cf96dfae980a79"
_V1_INPUT = {
    "company_id": "MEP",
    "actor": "analyst@mep.example",
    "action": "claim.verified",
    "subject_type": "claim",
    "subject_id": "CLM-1",
    "reason": "citation matched source at 1970:2066",
    "citation": "v1:1970:2066",
    "occurred_at": _T1,
}

GOLDEN_V2 = "de6a8346ac488d24ad662fa022244bd159ce8ecfb56b1abc8472309fd27b4554"
_V2_INPUT = {
    **_V1_INPUT,
    "actor": "ann@mep.example",
    "action": "action.approved",
    "subject_type": "escalation",
    "subject_id": "ESC-1",
    "reason": "refusal upheld for CLM-1: CITATION_QUOTE_MISMATCH",
    "citation": "v1:10:20",
    "actor_user_id": "USR-ANN",
    "actor_kind": "user",
    "session_id": "SES-ANN",
    "ip": "198.51.100.20",
}


def test_scheme_one_and_two_are_unchanged_by_the_arrival_of_scheme_three():
    # If either of these moves, every audit row already written stops
    # verifying, and a real alarm becomes indistinguishable from a false one.
    assert _digest(prev_hash="", seq=1, **_V1_INPUT) == GOLDEN_V1
    assert _digest_v2(prev_hash="", seq=1, **_V2_INPUT) == GOLDEN_V2
    # And nothing started writing scheme 3 by default.
    assert CURRENT_DIGEST_VERSION == DIGEST_V2


# ---------------------------------------------------------------------------
# One company with a real graph: a source, a change, a claim whose quote does
# not match the source, and the escalation that refusal raised.
# ---------------------------------------------------------------------------

DEFINITION = (
    '"Large Load Customer" means a Customer whose Requested Load equals or '
    "exceeds 20 megawatts (MW)."
)
SOURCE = "SECTION 2. DEFINITIONS\n\n2.1 " + DEFINITION + "\n"
QUOTE_START = SOURCE.index(DEFINITION)
QUOTE_END = QUOTE_START + len(DEFINITION)
# Same length as the truth, so the offsets still land inside the source. The
# claim misquotes; it does not point off the end.
MISQUOTE = DEFINITION.replace("20 megawatts", "10 megawatts")


def _seed(session, company_id: str) -> str:
    """A company whose one claim fails verification. Returns the escalation id."""
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
    session.add(
        Escalation(
            id=f"{prefix}-esc",
            company_id=company_id,
            claim_id=f"{prefix}-claim",
            reason_code=REASON_CODE_QUOTE_MISMATCH,
            reason_text="quoted text does not match the source",
            detail=f"{company_id} confidential detail",
        )
    )
    session.flush()
    return f"{prefix}-esc"


def _escalation(session, company_id: str, escalation_id: str) -> Escalation:
    return (
        session.query(Escalation)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.id == escalation_id)
        .one()
    )


def _resolve(
    session,
    company_id: str,
    escalation_id: str,
    *,
    action: str = ACTION_ESCALATION_APPROVED,
    actor: str = "ann@mep.example",
) -> AuditEvent:
    """Close an escalation the way app/web/views/review.py closes one.

    Mirrored rather than imported: the view drags in FastAPI and belongs to
    another agent's file. What is NOT mirrored is the action string -- that
    comes from app/state/audit.py, and a test below pins the view's copy to it.
    """
    row = _escalation(session, company_id, escalation_id)
    row.resolved_at = datetime.now(timezone.utc)
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
        citation="",
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


def _overwrite(session, entry_id: int, **values) -> None:
    """Rewrite a stored audit row behind the ORM's back, the way an attacker would.

    The before_flush guard refuses this through the ORM; a core UPDATE goes
    round it. That is exactly why the hash chain exists as a second mechanism.
    """
    session.execute(
        AuditEvent.__table__.update().where(AuditEvent.id == entry_id).values(**values)
    )
    session.commit()
    session.expire_all()


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


# ---------------------------------------------------------------------------
# 1. A reversal cannot make a citation verify
# ---------------------------------------------------------------------------

_THE_SENTENCE = (
    "NOTHING in this module may turn a withheld claim into an assertion -- "
    "only the source agreeing with the quote does that."
)


def test_the_module_says_in_its_own_docstring_what_it_cannot_do():
    from app.state import rollback

    assert _THE_SENTENCE in " ".join(rollback.__doc__.split())


def test_reverting_an_approval_leaves_the_claim_exactly_as_withheld():
    # The confusion this whole module invites: undoing the decision that
    # accepted a refusal must not read as accepting the claim.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        before_verified, before_withheld = verified_claims(session, "MEP", "mep-chg")
        assert (len(before_verified), len(before_withheld)) == (0, 1)

        resolution = _resolve(session, "MEP", escalation_id)
        _revert(session, "MEP", resolution.id)
        session.commit()

        after_verified, after_withheld = verified_claims(session, "MEP", "mep-chg")
        assert (len(after_verified), len(after_withheld)) == (0, 1)
        assert after_withheld[0].reason_code == before_withheld[0].reason_code
        # And the withheld form still cannot carry what the claim would say.
        assert not hasattr(after_withheld[0], "statement")


def test_the_module_never_touches_a_claim_or_a_source(repo_root):
    # A structural guard, because the failure this prevents is a helpful edit
    # six months from now rather than a bug today. If rollback.py ever needs to
    # name one of these, that is the moment to argue about it in review.
    source = (repo_root / "app" / "state" / "rollback.py").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    # Skip the docstring, which names them on purpose to say they are untouched.
    body = body.split('"""', 2)[-1]
    for forbidden in ("Claim", "DocumentVersion", "Passage", "verified_claims"):
        assert forbidden not in body, (
            f"rollback.py names {forbidden}. Reverting a decision restores state; "
            "it does not re-open the question of what the source says."
        )


# ---------------------------------------------------------------------------
# 2. The reversal is a row
# ---------------------------------------------------------------------------


def test_reverting_writes_a_new_row_and_leaves_the_original_untouched():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        before = (
            resolution.entry_hash,
            resolution.action,
            resolution.reason,
            resolution.reverts_event_id,
        )
        count_before = event_count(session, "MEP")

        result = _revert(session, "MEP", resolution.id)
        session.commit()

        assert event_count(session, "MEP") == count_before + 1
        assert result.event.reverts_event_id == resolution.id
        assert result.event.action == ACTION_DECISION_REVERTED
        assert result.reverted_event_id == resolution.id

        # Nothing was written on the row being taken back. It does not know.
        original = session.get(AuditEvent, resolution.id)
        assert (
            original.entry_hash,
            original.action,
            original.reason,
            original.reverts_event_id,
        ) == before


def test_the_chain_verifies_across_a_reversal():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        _revert(session, "MEP", resolution.id)
        session.commit()

        rows = _rows(session)
        assert [r.digest_version for r in rows] == [DIGEST_V2, DIGEST_V3]
        assert verify_chain(session, "MEP") is True


def test_the_reversal_row_names_the_same_subject_as_the_row_it_takes_back():
    # So that "everything that happened to this escalation" finds both. Which
    # event was undone is the reverts_event_id column, not the subject.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        result = _revert(session, "MEP", resolution.id)
        session.commit()

        assert result.event.subject_type == resolution.subject_type == "escalation"
        assert result.event.subject_id == resolution.subject_id == escalation_id
        # A reversal rests on the reverter's judgement, not on a span of source.
        assert result.event.citation == ""


@pytest.mark.parametrize(
    "action",
    (ACTION_ESCALATION_APPROVED, ACTION_ESCALATION_REJECTED, ACTION_ESCALATION_RESOLVED),
)
def test_reverting_any_resolution_reopens_the_escalation(action):
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id, action=action)
        assert _escalation(session, "MEP", escalation_id).resolved_at is not None

        result = _revert(session, "MEP", resolution.id)
        session.commit()

        row = _escalation(session, "MEP", escalation_id)
        assert row.resolved_at is None
        assert row.resolved_by is None
        assert escalation_id in result.outcome
        assert verify_chain(session, "MEP") is True


def test_the_reversal_reason_carries_both_the_note_and_what_it_undid():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        result = _revert(session, "MEP", resolution.id, reason="wrong escalation")
        session.commit()

        assert "wrong escalation" in result.event.reason
        assert str(resolution.seq) in result.event.reason
        assert ACTION_ESCALATION_APPROVED in result.event.reason


# ---------------------------------------------------------------------------
# 3. The pointer is inside the digest
# ---------------------------------------------------------------------------


def test_repointing_a_reversal_at_another_event_turns_the_chain_red():
    # The whole reason scheme 3 exists. Left outside the hash, anyone with write
    # access could make the log say a different decision had been taken back,
    # and the chain would keep verifying.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        first = _resolve(session, "MEP", escalation_id)
        decoy = record_event(
            session,
            company_id="MEP",
            actor="ann@mep.example",
            action=ACTION_LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id="USR-ANN",
            reason="password accepted",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-ANN",
        )
        reversal = _revert(session, "MEP", first.id).event
        session.commit()
        assert verify_chain(session, "MEP") is True

        _overwrite(session, reversal.id, reverts_event_id=decoy.id)
        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert f"seq {reversal.seq}" in str(caught.value)


def test_erasing_a_reversal_pointer_turns_the_chain_red():
    # The quieter attack: not "it reversed something else" but "it reversed
    # nothing", which would leave the decision standing again.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        session.commit()

        _overwrite(session, reversal.id, reverts_event_id=None)
        with pytest.raises(AuditTamperError):
            verify_chain(session, "MEP")


def test_downgrading_a_reversal_to_scheme_two_is_detected():
    # The obvious way out of a versioned digest: claim the row was hashed by the
    # scheme that does not cover the field you want to move.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        session.commit()

        _overwrite(session, reversal.id, digest_version=DIGEST_V2)
        with pytest.raises(AuditTamperError):
            verify_chain(session, "MEP")


@pytest.mark.parametrize("version", (DIGEST_V1, DIGEST_V2))
def test_a_row_hashed_before_scheme_three_may_not_carry_a_reversal_pointer(version):
    # Scheme 1 and scheme 2 do not cover this column. A row under either that
    # carries a pointer is asserting something nothing hashed, so it is refused
    # rather than read. Absence is denial, applied to a field the chain does not
    # defend.
    init_db()
    with session_scope() as session:
        target = record_event(
            session,
            company_id="MEP",
            actor="ann@mep.example",
            action=ACTION_LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id="USR-ANN",
            reason="password accepted",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-ANN",
        )
        later = record_event(
            session,
            company_id="MEP",
            actor="pipeline",
            action="ingest_version",
            subject_type="version",
            subject_id="mep-v1",
            reason="loaded the corpus",
        )
        session.commit()
        assert verify_chain(session, "MEP") is True

        _overwrite(
            session, later.id, digest_version=version, reverts_event_id=target.id
        )
        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "revers" in str(caught.value).lower()


def test_a_reversal_row_states_scheme_three_and_ordinary_rows_do_not():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        assert resolution.digest_version == DIGEST_V2
        reversal = _revert(session, "MEP", resolution.id).event
        assert reversal.digest_version == DIGEST_V3
        session.commit()
        assert verify_chain(session, "MEP") is True


def test_record_event_refuses_a_pointer_into_another_company():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        rival_escalation = _seed(session, "RIVAL")
        theirs = _resolve(session, "RIVAL", rival_escalation)
        session.commit()

        with pytest.raises(ValueError) as caught:
            record_event(
                session,
                company_id="MEP",
                actor="bob@mep.example",
                action=ACTION_DECISION_REVERTED,
                subject_type="escalation",
                subject_id=rival_escalation,
                reason="not ours to take back",
                actor_kind=ACTOR_USER,
                reverts_event_id=theirs.id,
            )
        assert NO_SUCH_EVENT in str(caught.value)


def test_record_event_refuses_to_stack_a_second_reversal_on_one_event():
    # The backstop under revert_event's own check, at the chokepoint every write
    # goes through, so a future caller cannot get round it by not using rollback.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        _revert(session, "MEP", resolution.id)
        session.commit()

        with pytest.raises(ValueError) as caught:
            record_event(
                session,
                company_id="MEP",
                actor="cat@mep.example",
                action=ACTION_DECISION_REVERTED,
                subject_type="escalation",
                subject_id=escalation_id,
                reason="again",
                actor_kind=ACTOR_USER,
                reverts_event_id=resolution.id,
            )
        assert ALREADY_REVERTED in str(caught.value)


# ---------------------------------------------------------------------------
# 4. Refusals
# ---------------------------------------------------------------------------


def test_reverting_an_already_reverted_event_refuses_rather_than_stacking():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        _revert(session, "MEP", resolution.id)
        session.commit()
        count = event_count(session, "MEP")

        with pytest.raises(ValueError) as caught:
            _revert(session, "MEP", resolution.id, reason="second thoughts")
        assert ALREADY_REVERTED in str(caught.value)
        session.rollback()
        assert event_count(session, "MEP") == count


def test_reverting_another_tenants_event_is_indistinguishable_from_a_missing_one():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        rival_escalation = _seed(session, "RIVAL")
        theirs = _resolve(session, "RIVAL", rival_escalation)
        session.commit()

        with pytest.raises(ValueError) as cross:
            _revert(session, "MEP", theirs.id)
        with pytest.raises(ValueError) as missing:
            _revert(session, "MEP", 999_999)
        assert str(cross.value) == str(missing.value) == NO_SUCH_EVENT

        session.rollback()
        # And nothing over there moved.
        assert _escalation(session, "RIVAL", rival_escalation).resolved_at is not None
        assert verify_chain(session, "RIVAL") is True


def test_an_unscoped_revert_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        session.commit()

        for scope in ("", None):
            with pytest.raises(ValueError) as caught:
                revert_event(
                    session,
                    company_id=scope,
                    event_id=resolution.id,
                    actor="bob@mep.example",
                    actor_kind=ACTOR_USER,
                    reason="no scope",
                )
            assert "company_id" in str(caught.value)


def test_reverting_an_action_this_module_cannot_undo_is_refused():
    # A login is not a decision with state behind it. Writing a row that claimed
    # to have taken one back would be a fallback that did not announce itself.
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        login = record_event(
            session,
            company_id="MEP",
            actor="ann@mep.example",
            action=ACTION_LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id="USR-ANN",
            reason="password accepted",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-ANN",
        )
        session.commit()
        count = event_count(session, "MEP")

        with pytest.raises(ValueError) as caught:
            _revert(session, "MEP", login.id)
        message = str(caught.value)
        assert NOT_REVERSIBLE in message
        # It says which pair it does not know, so the fix is obvious.
        assert ACTION_LOGIN_SUCCEEDED in message and "user" in message

        session.rollback()
        assert event_count(session, "MEP") == count
        assert reversal_of(session, company_id="MEP", event_id=login.id) is None


def test_reverting_an_escalation_that_is_already_open_is_refused():
    # The state moved out from under the event. Undoing it blind would write a
    # row claiming a change that did not happen.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        session.commit()
        # Somebody reopened it by another path.
        row = _escalation(session, "MEP", escalation_id)
        row.resolved_at = None
        row.resolved_by = None
        session.flush()
        count = event_count(session, "MEP")

        with pytest.raises(ValueError) as caught:
            _revert(session, "MEP", resolution.id)
        assert NOTHING_TO_RESTORE in str(caught.value)
        assert event_count(session, "MEP") == count
        assert reversal_of(session, company_id="MEP", event_id=resolution.id) is None


def test_a_reversal_needs_a_reason_and_an_actor():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        session.commit()

        with pytest.raises(ValueError) as no_reason:
            revert_event(
                session,
                company_id="MEP",
                event_id=resolution.id,
                actor="bob@mep.example",
                actor_kind=ACTOR_USER,
                reason="   ",
            )
        assert REASON_REQUIRED in str(no_reason.value)

        with pytest.raises(ValueError) as no_actor:
            revert_event(
                session,
                company_id="MEP",
                event_id=resolution.id,
                actor="",
                actor_kind=ACTOR_USER,
                reason="approved the wrong one",
            )
        assert ACTOR_REQUIRED in str(no_actor.value)

        # Neither wrote anything, and the escalation is still resolved.
        assert _escalation(session, "MEP", escalation_id).resolved_at is not None


def test_the_actor_kind_has_to_be_stated_rather_than_assumed():
    # A machine may take a decision back -- a migration correcting a batch is a
    # real case -- but it has to say so. There is no default, because a default
    # of "user" would quietly claim a person and a default of "system" would
    # quietly claim nobody was responsible.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        session.commit()

        with pytest.raises(TypeError):
            revert_event(
                session,
                company_id="MEP",
                event_id=resolution.id,
                actor="batch-fixer",
                reason="corrected in bulk",
            )

        result = revert_event(
            session,
            company_id="MEP",
            event_id=resolution.id,
            actor="batch-fixer",
            actor_kind=ACTOR_SYSTEM,
            reason="corrected in bulk",
        )
        session.commit()
        assert result.event.actor_kind == ACTOR_SYSTEM
        assert result.event.actor_user_id is None
        assert verify_chain(session, "MEP") is True


def test_an_event_id_that_is_not_a_number_is_refused_rather_than_matched():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        session.commit()

        with pytest.raises(ValueError):
            _revert(session, "MEP", str(resolution.id))


# ---------------------------------------------------------------------------
# 5. Taking back a reversal
# ---------------------------------------------------------------------------


def test_a_reversal_can_itself_be_taken_back():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        withdrawal = _revert(session, "MEP", reversal.id, reason="the undo was hasty")
        session.commit()

        assert withdrawal.event.action == ACTION_REVERSAL_WITHDRAWN
        assert withdrawal.event.reverts_event_id == reversal.id
        assert withdrawal.event.digest_version == DIGEST_V3
        assert verify_chain(session, "MEP") is True


def test_taking_back_a_reversal_does_not_reinstate_the_decision():
    # The rule, stated: a rollback cannot manufacture a judgement nobody made.
    # Putting "resolved" back would have to name a resolver -- either the person
    # who never re-made the decision, or the original one at a time when the
    # escalation was demonstrably open. Both are false statements, so neither is
    # written. The escalation stays open and the row says so.
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        withdrawal = _revert(session, "MEP", reversal.id, reason="the undo was hasty")
        session.commit()

        row = _escalation(session, "MEP", escalation_id)
        assert row.resolved_at is None
        assert row.resolved_by is None
        assert NOT_REINSTATED in withdrawal.outcome
        assert NOT_REINSTATED in withdrawal.event.reason


def test_a_withdrawn_reversal_does_not_make_the_original_decision_stand_again():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        withdrawal = _revert(session, "MEP", reversal.id, reason="the undo was hasty")
        session.commit()

        assert event_stands(session, company_id="MEP", event_id=resolution.id) is False
        assert event_stands(session, company_id="MEP", event_id=reversal.id) is False
        assert (
            event_stands(session, company_id="MEP", event_id=withdrawal.event.id) is True
        )


def test_taking_back_a_withdrawal_is_itself_a_withdrawal_and_still_reinstates_nothing():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        reversal = _revert(session, "MEP", resolution.id).event
        withdrawal = _revert(session, "MEP", reversal.id, reason="hasty").event
        fourth = _revert(session, "MEP", withdrawal.id, reason="hasty again")
        session.commit()

        assert fourth.event.action == ACTION_REVERSAL_WITHDRAWN
        assert _escalation(session, "MEP", escalation_id).resolved_at is None
        assert verify_chain(session, "MEP") is True
        assert len(_rows(session)) == 4


# ---------------------------------------------------------------------------
# 6. Reading the record afterwards
# ---------------------------------------------------------------------------


def test_reversal_of_finds_the_row_that_took_a_decision_back():
    init_db()
    with session_scope() as session:
        escalation_id = _seed(session, "MEP")
        resolution = _resolve(session, "MEP", escalation_id)
        assert reversal_of(session, company_id="MEP", event_id=resolution.id) is None
        assert event_stands(session, company_id="MEP", event_id=resolution.id) is True

        reversal = _revert(session, "MEP", resolution.id).event
        session.commit()

        found = reversal_of(session, company_id="MEP", event_id=resolution.id)
        assert found is not None and found.id == reversal.id
        assert (
            reverted_event(session, company_id="MEP", event_id=reversal.id).id
            == resolution.id
        )
        assert reverted_event(session, company_id="MEP", event_id=resolution.id) is None


def test_a_reversal_pointer_that_crosses_companies_refuses_to_resolve():
    # models.py says it plainly: audit ids are unique across tenants, so nothing
    # in the schema stops this. A reader that found one must refuse to read it
    # as a reversal rather than hand back another tenant's row.
    init_db()
    with session_scope() as session:
        mep_escalation = _seed(session, "MEP")
        rival_escalation = _seed(session, "RIVAL")
        theirs = _resolve(session, "RIVAL", rival_escalation)
        ours = _resolve(session, "MEP", mep_escalation)
        reversal = _revert(session, "MEP", ours.id).event
        session.commit()

        _overwrite(session, reversal.id, reverts_event_id=theirs.id)
        with pytest.raises(AuditTamperError):
            reverted_event(session, company_id="MEP", event_id=reversal.id)
        # And the other tenant does not see it as a reversal of their row.
        assert reversal_of(session, company_id="RIVAL", event_id=theirs.id) is None


def test_asking_whether_a_missing_event_stands_is_refused_rather_than_guessed():
    init_db()
    with session_scope() as session:
        _seed(session, "MEP")
        session.commit()
        for call in (event_stands, reversal_of, reverted_event):
            with pytest.raises(ValueError) as caught:
                call(session, company_id="MEP", event_id=999_999)
            assert NO_SUCH_EVENT in str(caught.value)


def test_one_tenants_rollback_leaves_the_other_chain_alone():
    init_db()
    with session_scope() as session:
        mep_escalation = _seed(session, "MEP")
        rival_escalation = _seed(session, "RIVAL")
        _resolve(session, "RIVAL", rival_escalation)
        ours = _resolve(session, "MEP", mep_escalation)
        _revert(session, "MEP", ours.id)
        session.commit()

        assert event_count(session, "MEP") == 2
        assert event_count(session, "RIVAL") == 1
        assert verify_chain(session, "MEP") is True
        assert verify_chain(session, "RIVAL") is True
        assert _escalation(session, "RIVAL", rival_escalation).resolved_at is not None


# ---------------------------------------------------------------------------
# 7. One vocabulary, one scheme number
# ---------------------------------------------------------------------------


def test_the_digest_scheme_number_has_one_value_wherever_it_is_spelt():
    # DIGEST_V3 belongs in app/state/audit.py beside V1 and V2. It was declared
    # in models.py first because the column landed there first. While both
    # copies exist they must agree; the models.py line is meant to be deleted.
    from app.state import models

    assert DIGEST_V3 == 3
    if hasattr(models, "DIGEST_V3"):
        assert models.DIGEST_V3 == DIGEST_V3, (
            "two spellings of one digest scheme disagree about which fields a "
            "hash covers. Delete the copy in models.py."
        )


def test_the_view_that_resolves_escalations_writes_the_codes_rollback_knows():
    # The failure this catches is silent: a resolution written under a code the
    # registry does not hold is simply not reversible, and nothing says so until
    # a reviewer tries to undo one.
    from app.state import rollback
    from app.web.views import review

    for code in (review.ACTION_APPROVED, review.ACTION_REJECTED):
        assert (code, "escalation") in rollback.RESTORERS, (
            f"{code!r} closes an escalation but rollback cannot reopen one. "
            "Either register it or stop writing it."
        )


def test_the_two_new_action_codes_are_distinct_and_exported():
    from app.state import audit

    codes = {
        name: getattr(audit, name) for name in dir(audit) if name.startswith("ACTION_")
    }
    assert len(set(codes.values())) == len(codes)
    for name in (
        "ACTION_DECISION_REVERTED",
        "ACTION_REVERSAL_WITHDRAWN",
        "ACTION_ESCALATION_APPROVED",
        "ACTION_ESCALATION_REJECTED",
    ):
        assert name in audit.__all__, f"{name} is not exported; a caller cannot use it"
