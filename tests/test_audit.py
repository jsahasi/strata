import ast
from pathlib import Path

import pytest

from app.state import audit
from app.state.audit import (
    ACTION_MATERIALITY_SET,
    ACTOR_MODEL,
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


# ---------------------------------------------------------------------------
# The vocabulary, and the second home it kept growing
# ---------------------------------------------------------------------------


def test_the_materiality_judgement_has_one_spelling_and_it_is_here():
    """A materiality verdict is a decision, so it has an action code like any other.

    It had none until the verdict was persisted, and in the meantime the string
    grew a second home: tests/test_policy.py types `change.materiality_set` as a
    bare literal in two places. That is the exact drift the ACTION_ constants
    were consolidated to stop, and it is the same shape as
    `escalation.approved`, which app/web/views/review.py still restates.

    The code is exported, because a constant nothing may import is a constant
    every caller will retype.
    """
    assert ACTION_MATERIALITY_SET == "change.materiality_set"
    assert "ACTION_MATERIALITY_SET" in audit.__all__


def test_a_materiality_row_names_the_model_as_the_actor():
    """The judgement is a machine's, and the row says so rather than implying it.

    actor_kind is the field a later query filters on -- "what did the model
    decide about this company" -- and it is not recoverable from an actor string
    somebody chose. A model actor carries no user id, which record_event already
    refuses, so this also pins that a materiality row cannot be attributed to a
    person by accident.
    """
    init_db()
    with session_scope() as session:
        event = record_event(
            session,
            company_id="MEP",
            actor="model:claude-opus-5",
            action=ACTION_MATERIALITY_SET,
            subject_type="change",
            subject_id="CHG-v1-v2-003",
            reason="material: the filing deadline moved from 90 days to 60",
            citation="v2:1970:2066",
            actor_kind=ACTOR_MODEL,
        )
        assert event.action == "change.materiality_set"
        assert event.actor_kind == ACTOR_MODEL
        assert event.actor_user_id is None
        assert verify_chain(session, "MEP") is True


#: The one place under app/ that still types an action code this file already
#: names, with the argument for why it is not fixed here.
#:
#: NAMED, NOT COUNTED, and it is a debt rather than a decision.
#: app/web/views/review.py declares its own `escalation.approved` and
#: `escalation.rejected`, and app/web/views/review_centre.py imports them from
#: there -- so the vocabulary has a second home with the same two words in it.
#: tests/test_rollback.py already pins the two copies together, which is what
#: keeps them from disagreeing until review.py imports these instead.
_ACTION_LITERAL_EXEMPT = {"app/web/views/review.py"}


def test_no_module_under_app_retypes_an_action_code_this_file_already_names():
    """Fix the class, not the line.

    Adding ACTION_MATERIALITY_SET removes one bare literal. It does not stop the
    next one, and the failure mode is quiet: a retyped code writes a row that
    hashes perfectly and that no query for that action will ever return. The
    event is in the log and invisible, which is worse than missing.

    Read from the syntax tree rather than grepped. Every ACTION_ value in this
    module is a short noun.verb string that also appears in prose -- the
    docstrings above argue about `escalation.approved` by name -- and a
    substring search would fire on the argument for the rule.
    """
    repo_root = Path(__file__).resolve().parent.parent
    codes = {
        value
        for name, value in vars(audit).items()
        if name.startswith("ACTION_") and isinstance(value, str)
    }

    offenders = []
    home = repo_root / "app" / "state" / "audit.py"
    for path in sorted((repo_root / "app").rglob("*.py")):
        relative = path.relative_to(repo_root).as_posix()
        # The file that defines them is where the literals belong.
        if path == home or relative in _ACTION_LITERAL_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in codes
            ):
                offenders.append(f"{relative}:{node.lineno} {node.value!r}")

    assert not offenders, (
        f"{offenders} type an action code app/state/audit.py already names. "
        "Import the constant. A typo in a retyped code writes a row that "
        "verifies and that nothing can find."
    )
