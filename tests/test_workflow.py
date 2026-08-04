"""The approval route: what it refuses to activate, and what its clock does.

Nobody could say what should happen when an approval sat untouched for two days,
because nothing anywhere expressed it. This suite is where that sentence gets
teeth, and it is built around four claims the product makes.

**A draft may be half-finished; a live route may not.** Saving invents nothing --
no default of twenty-four hours, no default of "remind" -- and activation refuses
with one error per offending step rather than filling the gaps in. A refused
activation changes nothing at all: the status is still draft afterwards, which
is the difference between a validation and a half-migration.

**Reminding is not acting.** A step that reminds stays pending and stays on the
same desk. The counter grows and nothing advances, because chasing somebody is
not the same as them answering.

**Escalation failing is not permission to skip.** When escalate_to resolves to
nobody the step stays open, visibly, on the person who already had it. It never
falls through to bypass. This is the branch a tired implementation gets wrong,
and getting it wrong turns a routing bug into an unapproved action.

**Bypassed is not approved, and the product has to be able to say so.** A run
that skipped a step can complete, and approval_summary() must still report it as
not fully approved and hand back the steps that were skipped -- not a boolean a
caller can round off.

Time is a parameter everywhere. No test sleeps, and no engine call reads the
clock for itself, so a deadline two days past is one argument rather than a wait.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.state.audit import verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import ensure_system_roles, set_user_status
from app.state.models import (
    ASSIGNEE_OBLIGATION_OWNER,
    ASSIGNEE_UNASSIGNED,
    OUTCOME_APPROVED,
    OUTCOME_BYPASSED,
    OUTCOME_ESCALATED,
    OUTCOME_REJECTED,
    ROLE_ADMIN,
    ROLE_ANALYST,
    STATUS_SUSPENDED,
    TIMEOUT_BYPASS,
    TIMEOUT_ESCALATE,
    TIMEOUT_REMIND,
    WORKFLOW_ACTIVE,
    WORKFLOW_ARCHIVED,
    WORKFLOW_DRAFT,
    WORKFLOW_RUN_COMPLETED,
    WORKFLOW_RUN_REJECTED,
    WORKFLOW_RUN_RUNNING,
    ApprovalWorkflow,
    AuditEvent,
)
from app.state.workflow import (
    ACTION_RUN_COMPLETED,
    ACTION_RUN_REFUSED,
    ACTION_RUN_STARTED,
    ACTION_STEP_ASSIGNED,
    ACTION_STEP_BYPASSED,
    ACTION_STEP_ESCALATED,
    ACTION_STEP_ESCALATION_FAILED,
    ACTION_STEP_REMINDED,
    ACTION_STEP_UNROUTED,
    ACTION_WORKFLOW_ACTIVATED,
    ACTION_WORKFLOW_ARCHIVED,
    RUN_NO_ACTIVE_WORKFLOW,
    activate_workflow,
    active_workflow,
    advance_overdue,
    approval_summary,
    create_workflow,
    desk_for_user,
    graph_dict,
    open_step_run,
    record_decision,
    run_for_escalation,
    save_graph,
    start_run,
    step_runs_for_run,
    unrouted_step_runs,
    validation_errors,
    workflow_for_company,
    workflows_for_company,
)

from tests.test_routing import (
    ACTOR,
    COMPANY,
    RIVAL,
    _change,
    _escalation,
    _person,
    _world,
)

T0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
WF = "WF-0001"


def _hours(count: int) -> timedelta:
    return timedelta(hours=count)


# ---------------------------------------------------------------------------
# Graph fixtures, written as the wire contract writes them
# ---------------------------------------------------------------------------


def _step(step_id: str, **overrides) -> dict:
    step = {
        "id": step_id,
        "label": f"Step {step_id}",
        "assignee_rule": ASSIGNEE_UNASSIGNED,
        "approval_hours": None,
        "on_timeout": None,
        "escalate_to": None,
        "remind_every_hours": None,
        "x": 0,
        "y": 0,
    }
    step.update(overrides)
    return step


def _graph(steps, edges=(), *, workflow_id=WF, name="Large load tariff review") -> dict:
    return {
        "workflow_id": workflow_id,
        "name": name,
        "status": WORKFLOW_DRAFT,
        "steps": list(steps),
        "edges": [{"from": a, "to": b} for a, b in edges],
    }


def _draft(session, workflow_id=WF, name="Large load tariff review"):
    return create_workflow(
        session, COMPANY, workflow_id=workflow_id, name=name, actor=ACTOR
    )


def _good_steps(owner_rule=ASSIGNEE_OBLIGATION_OWNER) -> list[dict]:
    """A route a regulatory team would recognise, using all three timeout rules."""
    return [
        _step(
            "STP-1",
            label="Obligation owner review",
            assignee_rule=owner_rule,
            approval_hours=24,
            on_timeout=TIMEOUT_REMIND,
            remind_every_hours=4,
            x=120,
            y=80,
        ),
        _step(
            "STP-2",
            label="Analyst check against the filed tariff",
            assignee_rule=f"role:{ROLE_ANALYST}",
            approval_hours=24,
            on_timeout=TIMEOUT_ESCALATE,
            escalate_to=f"role:{ROLE_ADMIN}",
            x=320,
            y=80,
        ),
        _step(
            "STP-3",
            label="Log the outcome on the docket record",
            assignee_rule=f"role:{ROLE_ADMIN}",
            approval_hours=12,
            on_timeout=TIMEOUT_BYPASS,
            x=520,
            y=80,
        ),
    ]


def _people(session):
    """The corpus's shape: one owner, one analyst, one admin."""
    ensure_system_roles(session)
    analyst = _person(session, COMPANY, "denise", role=ROLE_ANALYST)
    admin = _person(session, COMPANY, "sarah", role=ROLE_ADMIN)
    return analyst, admin


def _live_route(session, steps=None, edges=(("STP-1", "STP-2"), ("STP-2", "STP-3"))):
    """A world with people, an escalation, and one active route over it."""
    owner, change, escalation = _world(session)
    analyst, admin = _people(session)
    _draft(session)
    result = save_graph(
        session,
        COMPANY,
        WF,
        _graph(steps or _good_steps(), edges),
        actor=ACTOR,
    )
    assert result.ok, result.as_dict()
    activation = activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0)
    assert activation.ok, activation.as_dict()
    return owner, analyst, admin, escalation


def _actions(session, company_id=COMPANY):
    return [
        row.action
        for row in session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq)
        .all()
    ]


def _rows(session, action, company_id=COMPANY):
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .filter(AuditEvent.action == action)
        .order_by(AuditEvent.seq)
        .all()
    )


# ---------------------------------------------------------------------------
# Saving a draft
# ---------------------------------------------------------------------------


def test_a_half_finished_draft_saves_and_invents_nothing():
    init_db()
    with session_scope() as session:
        _draft(session)
        result = save_graph(
            session, COMPANY, WF, _graph([_step("STP-1", label="Owner review")]),
            actor=ACTOR,
        )
        assert result.as_dict() == {"ok": True}

        stored = graph_dict(session, COMPANY, WF)
        step = stored["steps"][0]
        assert step["approval_hours"] is None
        assert step["on_timeout"] is None
        assert step["remind_every_hours"] is None
        assert step["escalate_to"] is None
        assert step["assignee_rule"] == ASSIGNEE_UNASSIGNED


def test_the_graph_round_trips_through_save_and_reload_unchanged():
    init_db()
    with session_scope() as session:
        _people(session)
        _draft(session)
        posted = _graph(_good_steps(), (("STP-1", "STP-2"), ("STP-2", "STP-3")))
        assert save_graph(session, COMPANY, WF, posted, actor=ACTOR).ok
        assert graph_dict(session, COMPANY, WF) == posted


def test_a_save_refuses_two_steps_carrying_one_id():
    init_db()
    with session_scope() as session:
        _draft(session)
        result = save_graph(
            session, COMPANY, WF, _graph([_step("STP-1"), _step("STP-1")]), actor=ACTOR
        )
        assert not result.ok
        assert any(error.step_id == "STP-1" for error in result.errors)


def test_a_save_refuses_hours_that_are_not_a_number():
    init_db()
    with session_scope() as session:
        _draft(session)
        result = save_graph(
            session,
            COMPANY,
            WF,
            _graph([_step("STP-1", approval_hours="soon")]),
            actor=ACTOR,
        )
        assert not result.ok
        assert result.as_dict()["errors"][0]["step_id"] == "STP-1"


def test_a_save_refuses_a_timeout_word_outside_the_vocabulary():
    init_db()
    with session_scope() as session:
        _draft(session)
        result = save_graph(
            session, COMPANY, WF, _graph([_step("STP-1", on_timeout="wait")]),
            actor=ACTOR,
        )
        assert not result.ok


def test_a_negative_clock_saves_as_a_draft_and_is_refused_at_activation():
    """A draft may be wrong. A live route may not."""
    init_db()
    with session_scope() as session:
        _draft(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule=f"role:{ROLE_ADMIN}",
                approval_hours=-4,
                on_timeout=TIMEOUT_BYPASS,
            )
        ]
        assert save_graph(session, COMPANY, WF, _graph(steps), actor=ACTOR).ok
        _people(session)
        result = activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0)
        assert not result.ok
        assert workflow_for_company(session, COMPANY, WF).status == WORKFLOW_DRAFT


def test_an_active_route_refuses_a_save_and_says_what_to_do_instead():
    init_db()
    with session_scope() as session:
        _live_route(session)
        result = save_graph(session, COMPANY, WF, _graph([_step("STP-9")]), actor=ACTOR)
        assert not result.ok
        assert "draft" in result.errors[0].message
        assert len(graph_dict(session, COMPANY, WF)["steps"]) == 3


def test_a_save_from_another_company_cannot_reach_this_graph():
    init_db()
    with session_scope() as session:
        _draft(session)
        assert workflow_for_company(session, RIVAL, WF) is None
        assert graph_dict(session, RIVAL, WF) is None
        with pytest.raises(ValueError):
            save_graph(session, RIVAL, WF, _graph([_step("STP-1")]), actor=ACTOR)
        assert workflows_for_company(session, RIVAL) == []


# ---------------------------------------------------------------------------
# Validation: one error per offending step, and nothing half-activated
# ---------------------------------------------------------------------------


def _errors_for(session, steps, edges=()):
    _draft(session)
    save_graph(session, COMPANY, WF, _graph(steps, edges), actor=ACTOR)
    return validation_errors(session, COMPANY, WF)


def _messages(errors):
    return " | ".join(error.message for error in errors)


def test_a_route_with_no_steps_cannot_activate():
    init_db()
    with session_scope() as session:
        errors = _errors_for(session, [])
        assert errors
        assert errors[0].step_id is None


def test_a_route_with_no_start_step_cannot_activate():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        errors = _errors_for(
            session, steps, (("STP-1", "STP-2"), ("STP-2", "STP-3"), ("STP-3", "STP-1"))
        )
        assert "start" in _messages(errors)


def test_two_start_steps_are_both_named():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        errors = _errors_for(session, steps, (("STP-1", "STP-3"),))
        named = {error.step_id for error in errors if "start" in error.message}
        assert named == {"STP-1", "STP-2"}


def test_a_cycle_is_refused_and_every_step_on_it_is_named():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        errors = _errors_for(
            session, steps, (("STP-1", "STP-2"), ("STP-2", "STP-3"), ("STP-3", "STP-2"))
        )
        named = {error.step_id for error in errors if "loop" in error.message}
        assert named == {"STP-2", "STP-3"}


def test_a_step_nothing_reaches_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        errors = _errors_for(session, steps, (("STP-1", "STP-2"),))
        unreachable = [e for e in errors if e.step_id == "STP-3" and "reach" in e.message]
        assert unreachable


def test_an_edge_naming_a_step_that_is_not_in_the_graph_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        _draft(session)
        result = save_graph(
            session,
            COMPANY,
            WF,
            _graph(steps, (("STP-1", "STP-2"), ("STP-2", "STP-9"))),
            actor=ACTOR,
        )
        assert not result.ok
        assert any(error.step_id == "STP-9" for error in result.errors)


def test_escalate_with_nobody_to_escalate_to_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule=f"role:{ROLE_ADMIN}",
                approval_hours=24,
                on_timeout=TIMEOUT_ESCALATE,
                escalate_to=None,
            )
        ]
        errors = _errors_for(session, steps)
        assert [e.step_id for e in errors] == ["STP-1"]
        assert "escalate_to" in errors[0].message


def test_remind_with_no_interval_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule=f"role:{ROLE_ADMIN}",
                approval_hours=24,
                on_timeout=TIMEOUT_REMIND,
                remind_every_hours=None,
            )
        ]
        errors = _errors_for(session, steps)
        assert "remind_every_hours" in _messages(errors)


def test_a_clock_that_is_not_a_positive_integer_is_refused():
    # init_db() drops and recreates the tables, so it cannot run inside an open
    # session_scope(): the outer session already holds a RESERVED lock from its
    # own writes and DROP TABLE on a second pooled connection fails with
    # "database is locked". The original loop called it inside the block and
    # failed on its own line, before any code under test ran. One scope per
    # iteration instead -- the reset is the point of the loop, so it stays.
    for hours in (None, 0, -1):
        init_db()
        with session_scope() as session:
            _people(session)
            ensure_system_roles(session)
            steps = [
                _step(
                    "STP-1",
                    assignee_rule=f"role:{ROLE_ADMIN}",
                    approval_hours=hours,
                    on_timeout=TIMEOUT_BYPASS,
                )
            ]
            errors = _errors_for(session, steps)
            assert "approval_hours" in _messages(errors), hours


def test_an_unassigned_step_is_refused_because_it_cannot_route():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule=ASSIGNEE_UNASSIGNED,
                approval_hours=24,
                on_timeout=TIMEOUT_BYPASS,
            )
        ]
        errors = _errors_for(session, steps)
        assert "assignee" in _messages(errors)


def test_a_rule_naming_a_role_this_company_does_not_have_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule="role:auditor",
                approval_hours=24,
                on_timeout=TIMEOUT_BYPASS,
            )
        ]
        errors = _errors_for(session, steps)
        assert "auditor" in _messages(errors)


def test_a_rule_naming_an_account_this_company_does_not_have_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule="user:usr-nobody",
                approval_hours=24,
                on_timeout=TIMEOUT_BYPASS,
            )
        ]
        errors = _errors_for(session, steps)
        assert "usr-nobody" in _messages(errors)


def test_an_escalation_target_nobody_holds_is_refused():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = [
            _step(
                "STP-1",
                assignee_rule=f"role:{ROLE_ADMIN}",
                approval_hours=24,
                on_timeout=TIMEOUT_ESCALATE,
                escalate_to="role:auditor",
            )
        ]
        errors = _errors_for(session, steps)
        assert "auditor" in _messages(errors)


def test_a_fork_is_refused_because_this_engine_walks_one_step_at_a_time():
    init_db()
    with session_scope() as session:
        _people(session)
        steps = _good_steps(owner_rule=f"role:{ROLE_ADMIN}")
        errors = _errors_for(session, steps, (("STP-1", "STP-2"), ("STP-1", "STP-3")))
        assert "one outgoing" in _messages(errors)


def test_a_refused_activation_changes_nothing_at_all():
    init_db()
    with session_scope() as session:
        _people(session)
        _draft(session)
        save_graph(session, COMPANY, WF, _graph([_step("STP-1")]), actor=ACTOR)
        before = _actions(session)

        result = activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0)
        assert not result.ok
        assert result.as_dict()["ok"] is False
        assert set(result.as_dict()["errors"][0]) == {"step_id", "message"}

        stored = workflow_for_company(session, COMPANY, WF)
        assert stored.status == WORKFLOW_DRAFT
        assert stored.activated_at is None
        assert active_workflow(session, COMPANY) is None
        assert _actions(session) == before


def test_activation_returns_the_reasons_rather_than_raising():
    init_db()
    with session_scope() as session:
        _people(session)
        _draft(session)
        save_graph(session, COMPANY, WF, _graph([]), actor=ACTOR)
        result = activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0)
        assert not result.ok
        assert result.errors


def test_a_good_route_activates_and_becomes_the_live_one():
    init_db()
    with session_scope() as session:
        _live_route(session)
        stored = workflow_for_company(session, COMPANY, WF)
        assert stored.status == WORKFLOW_ACTIVE
        assert stored.activated_at == T0
        assert active_workflow(session, COMPANY).id == WF
        assert ACTION_WORKFLOW_ACTIVATED in _actions(session)
        assert verify_chain(session, COMPANY)


def test_activating_a_second_route_archives_the_first():
    init_db()
    with session_scope() as session:
        _live_route(session)
        second = "WF-0002"
        create_workflow(
            session,
            COMPANY,
            workflow_id=second,
            name="Large load tariff review, revised",
            actor=ACTOR,
            supersedes_id=WF,
        )
        save_graph(
            session,
            COMPANY,
            second,
            _graph(
                _good_steps(),
                (("STP-1", "STP-2"), ("STP-2", "STP-3")),
                workflow_id=second,
            ),
            actor=ACTOR,
        )
        later = T0 + _hours(1)
        assert activate_workflow(session, COMPANY, second, actor=ACTOR, now=later).ok

        assert workflow_for_company(session, COMPANY, WF).status == WORKFLOW_ARCHIVED
        assert active_workflow(session, COMPANY).id == second
        assert workflow_for_company(session, COMPANY, second).supersedes_id == WF
        assert ACTION_WORKFLOW_ARCHIVED in _actions(session)
        assert verify_chain(session, COMPANY)


def test_activating_the_route_that_is_already_live_writes_nothing():
    init_db()
    with session_scope() as session:
        _live_route(session)
        before = _actions(session)
        assert activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0).ok
        assert _actions(session) == before


def test_an_archived_route_cannot_be_brought_back():
    init_db()
    with session_scope() as session:
        _live_route(session)
        session.get(ApprovalWorkflow, WF).status = WORKFLOW_ARCHIVED
        session.flush()
        result = activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0)
        assert not result.ok
        assert "archived" in result.errors[0].message


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


def test_a_run_starts_on_the_first_step_assigned_by_its_rule():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        assert start.run is not None
        assert start.run.status == WORKFLOW_RUN_RUNNING
        assert start.run.current_step_id == "STP-1"

        step_run = open_step_run(session, COMPANY, start.run.id)
        assert step_run.assigned_to_user_id == owner.id
        assert step_run.assigned_at == T0
        assert step_run.due_at == T0 + _hours(24)
        assert step_run.outcome is None
        assert ACTION_RUN_STARTED in _actions(session)
        assert ACTION_STEP_ASSIGNED in _actions(session)
        assert verify_chain(session, COMPANY)


def test_starting_a_run_twice_for_one_escalation_returns_the_first():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(session)
        first = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        before = _actions(session)
        second = start_run(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        assert second.run.id == first.run.id
        assert _actions(session) == before


def test_with_no_live_route_a_run_refuses_and_says_so_out_loud():
    init_db()
    with session_scope() as session:
        _owner, _change, escalation = _world(session)
        _people(session)
        start = start_run(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        assert start.run is None
        assert start.reason_code == RUN_NO_ACTIVE_WORKFLOW
        assert start.reason_text
        assert ACTION_RUN_REFUSED in _actions(session)
        assert verify_chain(session, COMPANY)


def test_a_step_that_cannot_be_routed_opens_unassigned_with_no_clock():
    """The step is visible and stalled. It is never given to somebody at random."""
    init_db()
    with session_scope() as session:
        _people(session)
        change = _change(session, COMPANY, "CHG-ORPHAN")
        escalation = _escalation(session, COMPANY, "ESC-ORPHAN", change)
        _draft(session)
        save_graph(
            session,
            COMPANY,
            WF,
            _graph(_good_steps(), (("STP-1", "STP-2"), ("STP-2", "STP-3"))),
            actor=ACTOR,
        )
        assert activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0).ok

        start = start_run(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )
        step_run = open_step_run(session, COMPANY, start.run.id)
        assert step_run.assigned_to_user_id is None
        assert step_run.due_at is None, "an unrouted step must not start a clock"
        assert step_run.outcome is None
        assert [row.id for row in unrouted_step_runs(session, COMPANY)] == [step_run.id]

        reasons = [row.reason for row in _rows(session, ACTION_STEP_UNROUTED)]
        assert reasons and "ROUTE_NO_OBLIGATION" in reasons[0]


def test_an_unrouted_step_is_never_bypassed_however_long_it_waits():
    init_db()
    with session_scope() as session:
        _people(session)
        change = _change(session, COMPANY, "CHG-ORPHAN")
        escalation = _escalation(session, COMPANY, "ESC-ORPHAN", change)
        _draft(session)
        save_graph(
            session,
            COMPANY,
            WF,
            _graph(
                [
                    _step(
                        "STP-1",
                        assignee_rule=ASSIGNEE_OBLIGATION_OWNER,
                        approval_hours=1,
                        on_timeout=TIMEOUT_BYPASS,
                    )
                ]
            ),
            actor=ACTOR,
        )
        assert activate_workflow(session, COMPANY, WF, actor=ACTOR, now=T0).ok
        start = start_run(
            session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0
        )

        report = advance_overdue(session, COMPANY, now=T0 + _hours(500))
        assert report.bypassed_step_run_ids == ()
        assert report.unrouted_step_run_ids
        run = run_for_escalation(session, COMPANY, escalation)
        assert run.status == WORKFLOW_RUN_RUNNING
        assert open_step_run(session, COMPANY, start.run.id).outcome is None


# ---------------------------------------------------------------------------
# remind
# ---------------------------------------------------------------------------


def test_a_reminder_does_not_advance_anything():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)

        report = advance_overdue(session, COMPANY, now=T0 + _hours(25))
        assert report.reminders_sent == 1

        step_run = open_step_run(session, COMPANY, start.run.id)
        assert step_run.step_id == "STP-1"
        assert step_run.outcome is None, "a reminder is not an outcome"
        assert step_run.assigned_to_user_id == owner.id, "a reminder moves nobody"
        assert step_run.reminder_count == 1
        assert start.run.current_step_id == "STP-1"
        assert len(_rows(session, ACTION_STEP_REMINDED)) == 1


def test_running_the_tick_twice_does_not_double_remind():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        when = T0 + _hours(25)
        advance_overdue(session, COMPANY, now=when)
        second = advance_overdue(session, COMPANY, now=when)
        assert second.reminders_sent == 0
        assert open_step_run(session, COMPANY, start.run.id).reminder_count == 1


def test_reminders_catch_up_one_per_interval_that_has_passed():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        # due at +24, remind every 4: at +36 that is the deadline plus three
        # intervals, so four reminders are owed.
        report = advance_overdue(session, COMPANY, now=T0 + _hours(36))
        assert report.reminders_sent == 4
        assert open_step_run(session, COMPANY, start.run.id).reminder_count == 4
        assert len(_rows(session, ACTION_STEP_REMINDED)) == 4


def test_nothing_is_reminded_before_the_clock_runs_out():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        report = advance_overdue(session, COMPANY, now=T0 + _hours(23))
        assert report.reminders_sent == 0
        assert open_step_run(session, COMPANY, start.run.id).reminder_count == 0


# ---------------------------------------------------------------------------
# escalate
# ---------------------------------------------------------------------------


def _at_step_two(session):
    """Walk a run onto STP-2, whose timeout rule is escalate."""
    owner, analyst, admin, escalation = _live_route(session)
    start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
    record_decision(
        session,
        COMPANY,
        run_id=start.run.id,
        outcome=OUTCOME_APPROVED,
        actor="denise",
        acting_user_id=owner.id,
        now=T0 + _hours(1),
    )
    return owner, analyst, admin, escalation, start.run


def test_an_escalation_keeps_both_rows_and_names_both_people():
    init_db()
    with session_scope() as session:
        _owner, analyst, admin, _escalation, run = _at_step_two(session)
        assert open_step_run(session, COMPANY, run.id).assigned_to_user_id == analyst.id

        when = T0 + _hours(30)
        report = advance_overdue(session, COMPANY, now=when)
        assert len(report.escalated_step_run_ids) == 1

        rows = [row for row in step_runs_for_run(session, COMPANY, run.id)
                if row.step_id == "STP-2"]
        assert len(rows) == 2, "the first attempt must stay on the record"
        first, second = rows
        assert first.outcome == OUTCOME_ESCALATED
        assert first.assigned_to_user_id == analyst.id
        assert first.acted_by_user_id is None, "the clock is not a person"
        assert second.outcome is None
        assert second.assigned_to_user_id == admin.id
        assert second.due_at == when + _hours(24)

        reason = _rows(session, ACTION_STEP_ESCALATED)[-1].reason
        assert analyst.display_name in reason and admin.display_name in reason
        assert verify_chain(session, COMPANY)


def test_an_escalation_that_resolves_to_nobody_never_falls_through_to_bypass():
    """Escalation failing is not permission to skip. The branch that must not slip."""
    init_db()
    with session_scope() as session:
        _owner, analyst, admin, _escalation, run = _at_step_two(session)
        set_user_status(session, COMPANY, admin.id, STATUS_SUSPENDED, ACTOR)

        report = advance_overdue(session, COMPANY, now=T0 + _hours(30))
        assert report.escalated_step_run_ids == ()
        assert report.bypassed_step_run_ids == ()
        assert report.stalled_step_run_ids

        step_run = open_step_run(session, COMPANY, run.id)
        assert step_run.step_id == "STP-2"
        assert step_run.outcome is None
        assert step_run.assigned_to_user_id == analyst.id
        assert run.status == WORKFLOW_RUN_RUNNING

        summary = approval_summary(session, COMPANY, run.id)
        assert summary.bypassed_steps == ()
        assert not summary.fully_approved


def test_a_failed_escalation_is_recorded_once_however_often_the_tick_runs():
    init_db()
    with session_scope() as session:
        _owner, _analyst, admin, _escalation, _run = _at_step_two(session)
        set_user_status(session, COMPANY, admin.id, STATUS_SUSPENDED, ACTOR)
        advance_overdue(session, COMPANY, now=T0 + _hours(30))
        advance_overdue(session, COMPANY, now=T0 + _hours(40))
        assert len(_rows(session, ACTION_STEP_ESCALATION_FAILED)) == 1


def test_an_escalated_step_does_not_escalate_again_to_the_same_desk():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, _escalation, run = _at_step_two(session)
        advance_overdue(session, COMPANY, now=T0 + _hours(30))
        advance_overdue(session, COMPANY, now=T0 + _hours(200))
        rows = [row for row in step_runs_for_run(session, COMPANY, run.id)
                if row.step_id == "STP-2"]
        assert len(rows) == 2
        assert rows[-1].outcome is None


# ---------------------------------------------------------------------------
# bypass -- the sanctioned fallback, which has to announce itself
# ---------------------------------------------------------------------------


def _at_step_three(session):
    owner, analyst, admin, escalation, run = _at_step_two(session)
    record_decision(
        session,
        COMPANY,
        run_id=run.id,
        outcome=OUTCOME_APPROVED,
        actor="denise",
        acting_user_id=analyst.id,
        now=T0 + _hours(2),
    )
    return owner, analyst, admin, escalation, run


def test_a_bypass_records_bypassed_and_never_approved():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, _escalation, run = _at_step_three(session)
        report = advance_overdue(session, COMPANY, now=T0 + _hours(20))
        assert len(report.bypassed_step_run_ids) == 1

        rows = {row.step_id: row for row in step_runs_for_run(session, COMPANY, run.id)}
        skipped = rows["STP-3"]
        assert skipped.outcome == OUTCOME_BYPASSED
        assert skipped.outcome != OUTCOME_APPROVED
        assert skipped.acted_by_user_id is None

        reason = _rows(session, ACTION_STEP_BYPASSED)[-1].reason
        assert "nobody" in reason.lower()
        assert "time" in reason.lower()
        assert verify_chain(session, COMPANY)


def test_a_bypassed_run_never_reports_itself_as_fully_approved():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, _escalation, run = _at_step_three(session)
        advance_overdue(session, COMPANY, now=T0 + _hours(20))

        stored = run_for_escalation(session, COMPANY, run.escalation_id)
        assert stored.status == WORKFLOW_RUN_COMPLETED

        summary = approval_summary(session, COMPANY, run.id)
        assert summary.fully_approved is False
        assert summary.bypassed_steps == ("STP-3",)
        assert summary.approved_steps == ("STP-1", "STP-2")
        assert summary.open_steps == ()


def test_the_completion_row_says_a_step_was_skipped():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, _escalation, _run = _at_step_three(session)
        advance_overdue(session, COMPANY, now=T0 + _hours(20))
        reason = _rows(session, ACTION_RUN_COMPLETED)[-1].reason
        assert "STP-3" in reason
        assert "bypass" in reason.lower()


def test_running_the_tick_twice_does_not_double_bypass():
    init_db()
    with session_scope() as session:
        _owner, _analyst, _admin, _escalation, run = _at_step_three(session)
        advance_overdue(session, COMPANY, now=T0 + _hours(20))
        before = _actions(session)
        report = advance_overdue(session, COMPANY, now=T0 + _hours(40))
        assert report.bypassed_step_run_ids == ()
        assert _actions(session) == before
        rows = [r for r in step_runs_for_run(session, COMPANY, run.id)
                if r.step_id == "STP-3"]
        assert len(rows) == 1


def test_a_run_that_was_approved_at_every_step_reports_so():
    init_db()
    with session_scope() as session:
        _owner, _analyst, admin, _escalation, run = _at_step_three(session)
        record_decision(
            session,
            COMPANY,
            run_id=run.id,
            outcome=OUTCOME_APPROVED,
            actor="sarah",
            acting_user_id=admin.id,
            now=T0 + _hours(3),
        )
        summary = approval_summary(session, COMPANY, run.id)
        assert summary.fully_approved is True
        assert summary.bypassed_steps == ()
        assert summary.approved_steps == ("STP-1", "STP-2", "STP-3")


# ---------------------------------------------------------------------------
# A person answering
# ---------------------------------------------------------------------------


def test_only_the_person_the_step_was_given_to_can_answer_it():
    init_db()
    with session_scope() as session:
        owner, analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        with pytest.raises(ValueError):
            record_decision(
                session,
                COMPANY,
                run_id=start.run.id,
                outcome=OUTCOME_APPROVED,
                actor="denise",
                acting_user_id=analyst.id,
                now=T0,
            )
        assert open_step_run(session, COMPANY, start.run.id).outcome is None
        assert owner.id != analyst.id


def test_a_rejection_stops_the_run_where_it_stands():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        record_decision(
            session,
            COMPANY,
            run_id=start.run.id,
            outcome=OUTCOME_REJECTED,
            actor="priya",
            acting_user_id=owner.id,
            now=T0 + _hours(1),
        )
        run = run_for_escalation(session, COMPANY, escalation)
        assert run.status == WORKFLOW_RUN_REJECTED
        assert run.current_step_id is None
        assert open_step_run(session, COMPANY, run.id) is None

        summary = approval_summary(session, COMPANY, run.id)
        assert summary.fully_approved is False
        assert summary.rejected_steps == ("STP-1",)


def test_a_rejected_run_is_left_alone_by_the_clock():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        record_decision(
            session,
            COMPANY,
            run_id=start.run.id,
            outcome=OUTCOME_REJECTED,
            actor="priya",
            acting_user_id=owner.id,
            now=T0 + _hours(1),
        )
        report = advance_overdue(session, COMPANY, now=T0 + _hours(500))
        assert report.checked == 0


def test_an_answer_the_vocabulary_does_not_have_is_refused():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        with pytest.raises(ValueError):
            record_decision(
                session,
                COMPANY,
                run_id=start.run.id,
                outcome="bypassed",
                actor="priya",
                acting_user_id=owner.id,
                now=T0,
            )


# ---------------------------------------------------------------------------
# The desk, which is the pain this feature exists for
# ---------------------------------------------------------------------------


def test_a_desk_holds_only_what_is_waiting_on_that_person_soonest_first():
    init_db()
    with session_scope() as session:
        owner, analyst, _admin, escalation = _live_route(session)
        start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)

        mine = desk_for_user(session, COMPANY, owner.id)
        assert [row.step_id for row in mine] == ["STP-1"]
        assert desk_for_user(session, COMPANY, analyst.id) == []
        assert mine[0].due_at == T0 + _hours(24)


def test_another_company_sees_none_of_this_companys_runs():
    init_db()
    with session_scope() as session:
        owner, _analyst, _admin, escalation = _live_route(session)
        start = start_run(session, COMPANY, escalation_id=escalation, actor=ACTOR, now=T0)
        assert run_for_escalation(session, RIVAL, escalation) is None
        assert step_runs_for_run(session, RIVAL, start.run.id) == []
        assert desk_for_user(session, RIVAL, owner.id) == []
        assert advance_overdue(session, RIVAL, now=T0 + _hours(500)).checked == 0
        assert open_step_run(session, COMPANY, start.run.id).outcome is None


def test_an_unscoped_call_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        _live_route(session)
        for value in ("", None):
            with pytest.raises(ValueError):
                workflows_for_company(session, value)
            with pytest.raises(ValueError):
                advance_overdue(session, value, now=T0)
