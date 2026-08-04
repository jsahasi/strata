"""The schema three features are built on, and the promises its shape makes.

Four agents build against these names and cannot ask what they are, so the
names are pinned here: a rename fails a test rather than a colleague's import.

Beyond the names, four properties are load-bearing and none of them is
enforceable by reading the model file:

  * an obligation whose owner left stays visible and unroutable, so the owner
    reference is nullable and NULL routes nowhere;
  * a draft workflow saves half-finished, so every field the admin has yet to
    decide is NULL rather than a default that reads like a decision;
  * one workflow is active per company, and the database says so rather than
    the write layer remembering to;
  * bypassed is not approved. A step that ran out of time and a step a person
    signed off are different values, and nothing downstream may read them as
    one.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.state.audit import ACTION_PROJECT_CREATED, record_event, verify_chain
from app.state.db import init_db, session_scope
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    ASSIGNEE_ROLE_PREFIX,
    ASSIGNEE_UNASSIGNED,
    ASSIGNEE_USER_PREFIX,
    OUTCOME_APPROVED,
    OUTCOME_BYPASSED,
    PERMISSION_CODES,
    STEP_RUN_OUTCOMES,
    STEP_TIMEOUT_ACTIONS,
    WORKFLOW_ACTIVE,
    WORKFLOW_ARCHIVED,
    WORKFLOW_DRAFT,
    WORKFLOW_RUN_STATUSES,
    WORKFLOW_STATUSES,
    ApprovalWorkflow,
    AuditEvent,
    Base,
    Change,
    ChangeObligation,
    Escalation,
    Obligation,
    WorkflowEdge,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def _columns(model) -> set[str]:
    return {column.key for column in model.__table__.columns}


def _throwaway_session():
    """A private database, for rows that must not join the shared chain."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _mapping(change_id: str, obligation_id: str) -> ChangeObligation:
    return ChangeObligation(
        company_id=COMPANY,
        change_id=change_id,
        obligation_id=obligation_id,
        mapped_by="analyst",
    )


def _workflow(
    workflow_id: str,
    status: str,
    company_id: str = COMPANY,
    name: str = "route",
) -> ApprovalWorkflow:
    return ApprovalWorkflow(
        id=workflow_id,
        company_id=company_id,
        name=name,
        status=status,
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# A. Reviewer routing
# ---------------------------------------------------------------------------


def test_the_routing_columns_four_agents_build_against_are_these():
    assert {"id", "company_id", "title", "owner_user_id"} <= _columns(Obligation)
    assert {"change_id", "obligation_id"} <= _columns(ChangeObligation)
    assert {"assigned_to_user_id", "assigned_at"} <= _columns(Escalation)


def test_an_obligation_whose_owner_left_stays_visible_and_unroutable():
    init_db()
    with session_scope() as session:
        session.add(
            Obligation(
                id="OBL-001",
                company_id=COMPANY,
                title="Post security before construction starts.",
                owner_user_id=None,
            )
        )
        session.flush()

    with session_scope() as session:
        obligation = session.get(Obligation, "OBL-001")
        # Still readable, and it names nobody. A row that vanished when its
        # owner left would take the duty with it.
        assert obligation is not None
        assert obligation.owner_user_id is None


def test_a_change_touches_many_obligations_and_an_obligation_many_changes():
    session = _throwaway_session()
    session.add_all(
        [
            _mapping("CHG-5", "OBL-001"),
            _mapping("CHG-5", "OBL-008"),
            _mapping("CHG-1", "OBL-001"),
        ]
    )
    session.flush()

    both_ways = session.query(ChangeObligation).count()
    assert both_ways == 3


def test_the_same_change_mapped_to_the_same_obligation_twice_is_refused():
    session = _throwaway_session()
    session.add(_mapping("CHG-1", "OBL-005"))
    session.flush()
    session.add(_mapping("CHG-1", "OBL-005"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_escalation_starts_assigned_to_nobody():
    session = _throwaway_session()
    escalation = Escalation(
        id="ESC-1",
        company_id=COMPANY,
        claim_id="CLM-1",
        reason_code="citation.not_found",
        reason_text="the quote is not in the source",
    )
    session.add(escalation)
    session.flush()

    # Nobody is holding it, and nothing pretends otherwise.
    assert escalation.assigned_to_user_id is None
    assert escalation.assigned_at is None


# ---------------------------------------------------------------------------
# B. The approval workflow
# ---------------------------------------------------------------------------


def test_the_step_columns_are_exactly_the_wire_contract():
    contract = {
        "id",
        "label",
        "assignee_rule",
        "approval_hours",
        "on_timeout",
        "escalate_to",
        "remind_every_hours",
        "x",
        "y",
    }
    assert contract <= _columns(WorkflowStep)
    assert "workflow_id" in _columns(WorkflowStep)
    assert {"workflow_id", "from_step_id", "to_step_id"} <= _columns(WorkflowEdge)
    assert {
        "id",
        "company_id",
        "workflow_id",
        "escalation_id",
        "current_step_id",
        "started_at",
        "status",
    } <= _columns(WorkflowRun)
    assert {
        "run_id",
        "step_id",
        "assigned_to_user_id",
        "assigned_at",
        "due_at",
        "outcome",
        "acted_at",
        "acted_by_user_id",
        "reminder_count",
    } <= _columns(WorkflowStepRun)


def test_the_vocabularies_are_closed_and_named():
    assert WORKFLOW_STATUSES == (WORKFLOW_DRAFT, WORKFLOW_ACTIVE, WORKFLOW_ARCHIVED)
    assert STEP_TIMEOUT_ACTIONS == ("remind", "escalate", "bypass")
    assert WORKFLOW_RUN_STATUSES == ("running", "completed", "rejected", "cancelled")
    assert ASSIGNEE_OBLIGATION_OWNER == "obligation_owner"
    assert ASSIGNEE_UNASSIGNED == "unassigned"
    assert (ASSIGNEE_ROLE_PREFIX, ASSIGNEE_USER_PREFIX) == ("role:", "user:")


def test_a_half_finished_draft_step_invents_no_deadline_and_no_policy():
    session = _throwaway_session()
    session.add(_workflow("WF-0001", WORKFLOW_DRAFT, name="Large load tariff review"))
    step = WorkflowStep(workflow_id="WF-0001", id="STP-1")
    session.add(step)
    session.flush()

    # The admin has not said how long the step gets or what happens when it
    # runs out. A default would be an answer nobody gave.
    assert step.approval_hours is None
    assert step.on_timeout is None
    assert step.remind_every_hours is None
    assert step.escalate_to is None
    # One spelling of "nobody is assigned yet", and it is the contract's.
    assert step.assignee_rule == ASSIGNEE_UNASSIGNED
    assert (step.x, step.y) == (0, 0)


def test_one_workflow_is_active_per_company_and_the_database_says_so():
    init_db()
    with session_scope() as session:
        session.add_all(
            [
                _workflow("WF-1", WORKFLOW_ACTIVE),
                _workflow("WF-2", WORKFLOW_DRAFT),
                _workflow("WF-3", WORKFLOW_DRAFT),
                _workflow("WF-4", WORKFLOW_ARCHIVED),
                # Another tenant's live route is not this tenant's business.
                _workflow("WF-5", WORKFLOW_ACTIVE, company_id=RIVAL),
            ]
        )
        session.flush()

    with session_scope() as session:
        session.add(
            _workflow("WF-6", WORKFLOW_ACTIVE, name="second live")
        )
        with pytest.raises(IntegrityError):
            session.flush()
        # The scope commits on the way out, and a failed flush poisons the
        # transaction until it is rolled back.
        session.rollback()


def test_a_step_id_is_local_to_its_workflow():
    session = _throwaway_session()
    session.add_all(
        [
            _workflow("WF-1", WORKFLOW_DRAFT),
            _workflow("WF-2", WORKFLOW_DRAFT),
        ]
    )
    # Both graphs start at STP-1. The canvas numbers its own nodes.
    session.add_all(
        [
            WorkflowStep(workflow_id="WF-1", id="STP-1", label="Owner review"),
            WorkflowStep(workflow_id="WF-2", id="STP-1", label="Owner review"),
        ]
    )
    session.flush()
    assert session.query(WorkflowStep).count() == 2


def test_the_same_edge_drawn_twice_is_refused():
    session = _throwaway_session()
    session.add(
        _workflow("WF-1", WORKFLOW_DRAFT)
    )
    session.add_all(
        [
            WorkflowStep(workflow_id="WF-1", id="STP-1"),
            WorkflowStep(workflow_id="WF-1", id="STP-2"),
        ]
    )
    edge = dict(workflow_id="WF-1", from_step_id="STP-1", to_step_id="STP-2")
    session.add(WorkflowEdge(**edge))
    session.flush()

    session.add(WorkflowEdge(**edge))
    with pytest.raises(IntegrityError):
        session.flush()


def test_bypassed_is_not_approved():
    # The whole point of the outcome column. If these two ever collapse into
    # one value, a step nobody answered reads as a step somebody signed.
    assert OUTCOME_APPROVED != OUTCOME_BYPASSED
    assert OUTCOME_APPROVED in STEP_RUN_OUTCOMES
    assert OUTCOME_BYPASSED in STEP_RUN_OUTCOMES
    # Exactly one value means a person said yes.
    assert OUTCOME_APPROVED == "approved"

    session = _throwaway_session()
    bypassed = WorkflowStepRun(
        company_id=COMPANY,
        run_id="RUN-1",
        step_id="STP-1",
        outcome=OUTCOME_BYPASSED,
        acted_at=NOW,
    )
    session.add(bypassed)
    session.flush()
    # Nobody acted. The clock did.
    assert bypassed.acted_by_user_id is None


def test_a_step_run_with_no_outcome_is_open_rather_than_approved():
    session = _throwaway_session()
    open_run = WorkflowStepRun(
        company_id=COMPANY,
        run_id="RUN-1",
        step_id="STP-1",
        assigned_at=NOW,
        due_at=NOW + timedelta(hours=24),
    )
    session.add(open_run)
    session.flush()

    assert open_run.outcome is None
    assert open_run.acted_at is None
    assert open_run.reminder_count == 0


def test_a_run_pins_the_workflow_it_started_under():
    session = _throwaway_session()
    session.add(
        _workflow("WF-1", WORKFLOW_ACTIVE)
    )
    run = WorkflowRun(
        id="RUN-1",
        company_id=COMPANY,
        workflow_id="WF-1",
        escalation_id="ESC-1",
        started_at=NOW,
        status="running",
    )
    session.add(run)
    session.flush()

    assert run.workflow_id == "WF-1"
    # Not yet at any step, and that is not the same as finished.
    assert run.current_step_id is None


# ---------------------------------------------------------------------------
# C. Rollback
# ---------------------------------------------------------------------------


def test_an_audit_row_can_name_the_row_it_reverses_and_neither_is_touched():
    session = _throwaway_session()
    original = AuditEvent(
        company_id=COMPANY,
        seq=1,
        actor="analyst@mep.example",
        action=ACTION_PROJECT_CREATED,
        subject_type="project",
        subject_id="PRJ-1",
        reason="opened",
        citation="",
        occurred_at=NOW,
        entry_hash="a" * 64,
    )
    session.add(original)
    session.flush()

    reversal = AuditEvent(
        company_id=COMPANY,
        seq=2,
        actor="admin@mep.example",
        action=ACTION_PROJECT_CREATED,
        subject_type="project",
        subject_id="PRJ-1",
        reason="opened in error",
        citation="",
        occurred_at=NOW,
        prev_hash="a" * 64,
        entry_hash="b" * 64,
        reverts_event_id=original.id,
    )
    session.add(reversal)
    session.flush()

    # Two rows, both readable. No tombstone, no edit, no delete.
    assert session.query(AuditEvent).count() == 2
    assert reversal.reverts_event_id == original.id
    assert original.reverts_event_id is None


def test_the_reversal_column_did_not_disturb_the_schemes_that_hash_today():
    init_db()
    with session_scope() as session:
        first = record_event(
            session,
            company_id=COMPANY,
            actor="system:test",
            action=ACTION_PROJECT_CREATED,
            subject_type="project",
            subject_id="PRJ-1",
            reason="opened",
        )
        record_event(
            session,
            company_id=COMPANY,
            actor="system:test",
            action=ACTION_PROJECT_CREATED,
            subject_type="project",
            subject_id="PRJ-2",
            reason="opened",
        )
        assert verify_chain(session, COMPANY) is True
        # Nothing written today claims to reverse anything.
        assert first.reverts_event_id is None


# ---------------------------------------------------------------------------
# D. Permission
# ---------------------------------------------------------------------------


def test_workflow_manage_is_a_permission_code():
    assert "workflow.manage" in PERMISSION_CODES


def test_change_and_obligation_are_both_company_scoped():
    assert "company_id" in _columns(Change)
    assert "company_id" in _columns(Obligation)
    assert "company_id" in _columns(ChangeObligation)
