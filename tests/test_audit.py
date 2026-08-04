import pytest

from app.state.audit import (
    AuditTamperError,
    record_event,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent


def _three_events(session):
    record_event(
        session,
        company_id="MEP",
        actor="analyst@mep.example",
        action="claim.verified",
        subject_type="claim",
        subject_id="CLM-1",
        reason="citation matched source at 1970:2066",
        citation="v1:1970:2066",
    )
    record_event(
        session,
        company_id="MEP",
        actor="analyst@mep.example",
        action="claim.escalated",
        subject_type="claim",
        subject_id="CLM-2",
        reason="quoted text does not match the source at the cited offsets",
        citation="v1:1970:2066",
    )
    record_event(
        session,
        company_id="MEP",
        actor="owner@mep.example",
        action="action.approved",
        subject_type="action",
        subject_id="ACT-1",
        reason="comply by 2026-11-01",
        citation="v3:8418:9294",
    )


def test_an_event_records_actor_timestamp_action_and_citation():
    init_db()
    with session_scope() as session:
        event = record_event(
            session,
            company_id="MEP",
            actor="analyst@mep.example",
            action="claim.verified",
            subject_type="claim",
            subject_id="CLM-1",
            reason="citation matched",
            citation="v1:1970:2066",
        )
        assert event.actor == "analyst@mep.example"
        assert event.action == "claim.verified"
        assert event.citation == "v1:1970:2066"
        assert event.occurred_at is not None
        assert event.occurred_at.tzinfo is not None, "timestamp must carry a timezone"
        assert event.entry_hash


def test_each_event_chains_to_its_predecessor():
    init_db()
    with session_scope() as session:
        _three_events(session)
        events = session.query(AuditEvent).order_by(AuditEvent.seq).all()
        assert len(events) == 3
        assert events[0].prev_hash == ""
        assert events[1].prev_hash == events[0].entry_hash
        assert events[2].prev_hash == events[1].entry_hash


def test_verify_chain_passes_on_an_untampered_log():
    init_db()
    with session_scope() as session:
        _three_events(session)
        assert verify_chain(session, "MEP") is True


def test_editing_a_recorded_reason_is_detected():
    # The whole point. A history that the writing process can silently edit
    # proves nothing when someone asks "what did you know, and when".
    init_db()
    with session_scope() as session:
        _three_events(session)
        session.commit()
        target = session.query(AuditEvent).order_by(AuditEvent.seq).all()[1]
        session.execute(
            AuditEvent.__table__.update()
            .where(AuditEvent.id == target.id)
            .values(reason="citation matched")
        )
        session.commit()
        session.expire_all()
        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "seq 2" in str(caught.value)


def test_removing_a_record_is_detected_as_a_gap():
    init_db()
    with session_scope() as session:
        _three_events(session)
        session.commit()
        target = session.query(AuditEvent).order_by(AuditEvent.seq).all()[1]
        session.execute(
            AuditEvent.__table__.delete().where(AuditEvent.id == target.id)
        )
        session.commit()
        session.expire_all()
        with pytest.raises(AuditTamperError):
            verify_chain(session, "MEP")


def test_the_application_has_no_path_to_update_or_delete_an_event():
    # Tamper evidence catches it afterwards. This refuses it up front, so the
    # application itself cannot rewrite its own record even by mistake.
    init_db()
    with session_scope() as session:
        _three_events(session)
        session.commit()
        event = session.query(AuditEvent).order_by(AuditEvent.seq).first()

        event.reason = "something else"
        with pytest.raises(AuditTamperError):
            session.flush()
        session.rollback()

        event = session.query(AuditEvent).order_by(AuditEvent.seq).first()
        session.delete(event)
        with pytest.raises(AuditTamperError):
            session.flush()
        session.rollback()


def test_one_company_cannot_read_or_disturb_another_companys_chain():
    init_db()
    with session_scope() as session:
        _three_events(session)
        record_event(
            session,
            company_id="RIVAL",
            actor="analyst@rival.example",
            action="claim.verified",
            subject_type="claim",
            subject_id="RIVAL-CLM-1",
            reason="unrelated",
            citation="x:0:1",
        )
        assert verify_chain(session, "MEP") is True
        assert verify_chain(session, "RIVAL") is True
        rival = (
            session.query(AuditEvent).filter_by(company_id="RIVAL").all()
        )
        assert len(rival) == 1
        assert rival[0].prev_hash == "", "each company's chain starts on its own"


def test_an_unscoped_verify_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        _three_events(session)
        for value in ("", None):
            with pytest.raises(ValueError):
                verify_chain(session, value)
