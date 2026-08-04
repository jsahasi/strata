"""The approval route: the admin's canvas, and the screen everybody else reads.

Five questions this file asks, in the order they matter.

1. **Does the gate refuse?** workflow.manage is an admin permission. An analyst
   asking for the list, the editor, the graph, a save or an activation gets a
   refusal with a reason, and the refusal lands in the audit chain. A hidden
   link is not a control; a blank canvas is not a control.

2. **Does the graph survive a round trip?** The wire contract is the whole
   agreement between the editor and whatever runs the route. A save followed by
   a reload must return the same JSON, field for field, including the canvas
   coordinates. Anything that quietly normalises a value here is a defect the
   canvas would show as a node that moved on its own.

3. **Does activation refuse, and does it say which node?** A draft may be saved
   half-finished. Activation is where the missing answers bite, and every error
   names the step it belongs to so the editor can put the message on the node
   rather than in a list at the bottom of the page.

4. **Does the read-only route tell the truth?** It is not the editor with the
   buttons greyed out. Any signed-in user reaches it, it writes the timeout rule
   as a sentence a person can act on, and it says plainly that a bypassed step
   was skipped rather than approved. A route that renders a bypass to look like
   an approval is the same defect as a claim asserted on a citation that did not
   verify.

5. **Is it one tenant?** Another company's workflow id is a 404 on every one of
   the six routes, read and write alike.

WHAT IS NOT TESTED HERE. app/web/static/workflow.js is not exercised: there is
no browser in this suite and no build step to add one. The tests below pin the
JSON the script is handed and the JSON it posts back, which is the seam a
regression would cross, and the script's own drawing is checked by eye. Said
plainly rather than implied by omission.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http drops it in silence.
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.audit import verify_chain
from app.state.db import get_engine, init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role
from app.state.models import (
    OUTCOME_APPROVED,
    OUTCOME_BYPASSED,
    ROLE_ADMIN,
    ROLE_ANALYST,
    WORKFLOW_ACTIVE,
    WORKFLOW_ARCHIVED,
    WORKFLOW_DRAFT,
    WORKFLOW_RUN_RUNNING,
    ApprovalWorkflow,
    AuditEvent,
    RolePermission,
    WorkflowEdge,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)
from app.web import deps
from app.web.views import admin as admin_view

COMPANY = "MEP"
RIVAL = "RIVAL"

ESCALATION = "ESC-CLM-MISQUOTE"

RIVAL_EMAIL = "admin@rival.example"
RIVAL_PASSWORD = "rival-admin-password"

LIST_URL = "/admin/workflows"
ROUTE_URL = "/workflow"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


def _email(role: str) -> str:
    """The seeded account holding a role, read from the corpus rather than typed."""
    return next(
        account.email for account in demo_account_list() if account.role == role
    )


def _sign_in(client: TestClient, email: str, password: str):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


@pytest.fixture
def anonymous() -> TestClient:
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin(anonymous: TestClient) -> TestClient:
    """Signed in as the seeded account holding workflow.manage."""
    assert _sign_in(anonymous, _email(ROLE_ADMIN), DEMO_PASSWORD).status_code == 303
    return anonymous


@pytest.fixture
def analyst(anonymous: TestClient) -> TestClient:
    """Signed in as somebody who does not hold workflow.manage."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST), DEMO_PASSWORD).status_code == 303
    return anonymous


def _make_workflow(company_id: str = COMPANY, workflow_id: str = "WF-0001") -> str:
    """A draft with two steps and one edge, written straight to the rows."""
    with session_scope() as session:
        session.add(
            ApprovalWorkflow(
                id=workflow_id,
                company_id=company_id,
                name="Large load tariff review",
                status=WORKFLOW_DRAFT,
            )
        )
        session.flush()
        session.add(
            WorkflowStep(
                workflow_id=workflow_id,
                id="STP-1",
                label="Obligation owner review",
                assignee_rule="obligation_owner",
                approval_hours=24,
                on_timeout="escalate",
                escalate_to=f"role:{ROLE_ADMIN}",
                remind_every_hours=None,
                x=120,
                y=80,
            )
        )
        session.add(
            WorkflowStep(
                workflow_id=workflow_id,
                id="STP-2",
                label="Regulatory sign-off",
                assignee_rule=f"role:{ROLE_ADMIN}",
                approval_hours=48,
                on_timeout="bypass",
                escalate_to=None,
                remind_every_hours=None,
                x=460,
                y=80,
            )
        )
        session.flush()
        session.add(
            WorkflowEdge(
                workflow_id=workflow_id, from_step_id="STP-1", to_step_id="STP-2"
            )
        )
    return workflow_id


def _graph(client: TestClient, workflow_id: str) -> dict:
    response = client.get(f"{LIST_URL}/{workflow_id}/graph")
    assert response.status_code == 200, response.text
    return response.json()


def _activate(client: TestClient, workflow_id: str):
    return client.post(f"{LIST_URL}/{workflow_id}/activate")


def _messages(payload: dict) -> dict[str, list[str]]:
    """Errors grouped by the step they name. "" is the graph itself."""
    grouped: dict[str, list[str]] = {}
    for error in payload["errors"]:
        grouped.setdefault(error["step_id"] or "", []).append(error["message"])
    return grouped


# ------------------------------------------------------------- the gate


def test_the_five_admin_routes_refuse_somebody_without_workflow_manage(analyst):
    """A hidden link is not a control. Every one of them answers 403.

    The two GET screens answer with a page a person can read; the three JSON
    routes answer with JSON, because a canvas cannot render an HTML refusal
    into a fetch handler.
    """
    workflow_id = _make_workflow()

    page = analyst.get(LIST_URL)
    assert page.status_code == 403
    assert "workflow.manage" in page.text

    editor = analyst.get(f"{LIST_URL}/{workflow_id}")
    assert editor.status_code == 403
    assert "workflow.manage" in editor.text

    for response in (
        analyst.get(f"{LIST_URL}/{workflow_id}/graph"),
        analyst.post(f"{LIST_URL}/{workflow_id}/graph", json={}),
        analyst.post(f"{LIST_URL}/{workflow_id}/activate"),
    ):
        assert response.status_code == 403
        body = response.json()
        assert body["ok"] is False
        assert "workflow.manage" in body["errors"][0]["message"]


def test_a_refusal_is_written_into_the_audit_chain(analyst):
    """require() audits its refusals, and the write must survive the response."""
    _make_workflow()
    assert analyst.get(LIST_URL).status_code == 403

    with session_scope() as session:
        denied = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.subject_id == admin_view.MANAGE)
            .all()
        )
        assert denied, "the refusal was rolled back with the response"
        assert verify_chain(session, COMPANY)


def test_a_refused_activation_does_not_change_the_status(analyst):
    """A gate that refuses after the write is not a gate."""
    workflow_id = _make_workflow()
    assert _activate(analyst, workflow_id).status_code == 403

    with session_scope() as session:
        assert session.get(ApprovalWorkflow, workflow_id).status == WORKFLOW_DRAFT


def test_a_check_that_could_not_run_refuses(analyst):
    """A gate that fails open on a broken database is not a gate.

    The grant tables are dropped and the session tables are left alone, so the
    request is still signed in and the permission read is the thing that
    cannot run. Before this, that answered 200 with an empty screen: nothing
    leaked, but the check had not run and the page behaved as though it had
    passed. On a clone nobody had seeded, the admin screen opened for anybody.
    """
    engine = get_engine()
    RolePermission.__table__.drop(engine)
    try:
        response = analyst.get(LIST_URL, follow_redirects=False)
        assert response.status_code == 403
        assert admin_view.MANAGE in response.text
    finally:
        RolePermission.__table__.create(engine)


# ------------------------------------------------------------- the screens


def test_an_admin_reaches_the_list_and_the_editor(admin):
    workflow_id = _make_workflow()

    listing = admin.get(LIST_URL)
    assert listing.status_code == 200
    assert workflow_id in listing.text
    assert "Large load tariff review" in listing.text

    editor = admin.get(f"{LIST_URL}/{workflow_id}")
    assert editor.status_code == 200
    assert "/static/workflow.js" in editor.text
    assert "/static/workflow.css" in editor.text


def test_the_editor_page_carries_the_graph_so_the_canvas_needs_no_fetch(admin):
    """Same argument as citation.js: the evidence is in the page already."""
    workflow_id = _make_workflow()
    body = admin.get(f"{LIST_URL}/{workflow_id}").text

    marker = '<script id="wf-graph" type="application/json">'
    assert marker in body
    embedded = json.loads(body.split(marker)[1].split("</script>")[0])
    assert embedded == _graph(admin, workflow_id)


def test_the_editor_says_plainly_that_it_needs_script(admin):
    """No build step means no framework, and no framework means saying so."""
    workflow_id = _make_workflow()
    body = admin.get(f"{LIST_URL}/{workflow_id}").text
    assert "<noscript>" in body
    assert ROUTE_URL in body


def test_an_admin_can_open_a_new_draft(admin):
    created = admin.post(
        LIST_URL, data={"name": "Emergency tariff review"}, follow_redirects=False
    )
    assert created.status_code == 303
    assert created.headers["location"].startswith(LIST_URL + "/")

    with session_scope() as session:
        rows = (
            session.query(ApprovalWorkflow)
            .filter(ApprovalWorkflow.company_id == COMPANY)
            .all()
        )
        assert [row.status for row in rows] == [WORKFLOW_DRAFT]
        assert rows[0].name == "Emergency tariff review"


# ------------------------------------------------------- the round trip


def test_the_graph_round_trips_through_save_and_reload_unchanged(admin):
    """The wire contract, asserted whole. A field that normalises is a defect."""
    workflow_id = _make_workflow()
    before = _graph(admin, workflow_id)

    assert before["workflow_id"] == workflow_id
    assert before["status"] == WORKFLOW_DRAFT
    assert [step["id"] for step in before["steps"]] == ["STP-1", "STP-2"]
    assert before["edges"] == [{"from": "STP-1", "to": "STP-2"}]
    assert before["steps"][0]["x"] == 120 and before["steps"][0]["y"] == 80

    saved = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=before)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"ok": True}

    assert _graph(admin, workflow_id) == before


def test_a_moved_node_keeps_its_new_position(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][0]["x"] = 300
    graph["steps"][0]["y"] = 210

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json() == {
        "ok": True
    }
    reloaded = _graph(admin, workflow_id)
    assert reloaded["steps"][0]["x"] == 300
    assert reloaded["steps"][0]["y"] == 210


def test_a_deleted_step_takes_its_edges_with_it(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"] = [step for step in graph["steps"] if step["id"] != "STP-2"]
    graph["edges"] = []

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json() == {
        "ok": True
    }
    with session_scope() as session:
        assert (
            session.query(WorkflowEdge)
            .filter(WorkflowEdge.workflow_id == workflow_id)
            .count()
            == 0
        )
        assert (
            session.query(WorkflowStep)
            .filter(WorkflowStep.workflow_id == workflow_id)
            .count()
            == 1
        )


def test_a_half_finished_draft_saves_and_invents_nothing(admin):
    """The contract allows it, so the columns stay NULL rather than gaining a default."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"] = [
        {
            "id": "STP-1",
            "label": "",
            "assignee_rule": "unassigned",
            "approval_hours": None,
            "on_timeout": None,
            "escalate_to": None,
            "remind_every_hours": None,
            "x": 40,
            "y": 40,
        }
    ]
    graph["edges"] = []

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json() == {
        "ok": True
    }
    reloaded = _graph(admin, workflow_id)["steps"][0]
    assert reloaded["approval_hours"] is None
    assert reloaded["on_timeout"] is None
    assert reloaded["remind_every_hours"] is None


def test_a_save_refuses_a_word_outside_the_vocabulary(admin):
    """Half-finished is allowed. Wrong is not: nothing here has a CHECK constraint."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][0]["on_timeout"] = "ignore"

    response = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)
    assert response.json()["ok"] is False
    assert "STP-1" in _messages(response.json())

    # And nothing was written.
    assert _graph(admin, workflow_id)["steps"][0]["on_timeout"] == "escalate"


def test_a_save_refuses_an_edge_that_names_no_step(admin):
    """SQLite would take it and PostgreSQL would not. One answer, not two."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["edges"].append({"from": "STP-2", "to": "STP-9"})

    response = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)
    assert response.json()["ok"] is False
    assert _graph(admin, workflow_id)["edges"] == [{"from": "STP-1", "to": "STP-2"}]


def test_a_save_refuses_two_steps_with_one_id(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][1]["id"] = "STP-1"
    graph["edges"] = []

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json()["ok"] is False


def test_a_save_that_does_not_say_what_the_steps_are_deletes_nothing(admin):
    """Absence is denial, applied to a write.

    An empty body is a client that lost its request, not an admin who deleted
    every node. Reading the two alike would wipe a route and answer ok.
    """
    workflow_id = _make_workflow()
    before = _graph(admin, workflow_id)

    for body in ({}, {"name": "Renamed"}, {"steps": []}, {"edges": []}):
        response = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=body)
        assert response.json()["ok"] is False, body

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph").json()["ok"] is False
    assert _graph(admin, workflow_id) == before


def test_a_save_refuses_a_graph_addressed_to_another_route(admin):
    """Two editor tabs. Writing it would overwrite one route with another's."""
    first = _make_workflow(workflow_id="WF-0001")
    second = _make_workflow(workflow_id="WF-0002")
    graph = _graph(admin, second)

    response = admin.post(f"{LIST_URL}/{first}/graph", json=graph)
    assert response.json()["ok"] is False
    assert "WF-0002" in response.json()["errors"][0]["message"]


def test_an_explicitly_emptied_graph_still_saves(admin):
    """Not vacuous: deleting every step is a real thing an admin does."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"], graph["edges"] = [], []

    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json() == {
        "ok": True
    }
    assert _graph(admin, workflow_id)["steps"] == []


def test_a_save_refuses_hours_that_are_not_a_positive_integer(admin):
    workflow_id = _make_workflow()
    for bad in ("soon", 0, -3, 1.5):
        graph = _graph(admin, workflow_id)
        graph["steps"][0]["approval_hours"] = bad
        response = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)
        assert response.json()["ok"] is False, bad


# -------------------------------------------------------------- activation


def test_activation_names_the_step_behind_every_refusal(admin):
    """An error list at the bottom of a canvas is a list nobody reads."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][0]["approval_hours"] = None
    graph["steps"][0]["on_timeout"] = None
    graph["steps"][1]["assignee_rule"] = "unassigned"
    assert admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph).json() == {
        "ok": True
    }

    response = _activate(admin, workflow_id)
    payload = response.json()
    assert payload["ok"] is False
    grouped = _messages(payload)
    assert "STP-1" in grouped and "STP-2" in grouped
    assert any("hours" in message for message in grouped["STP-1"])
    assert any("nobody" in message.lower() for message in grouped["STP-2"])

    with session_scope() as session:
        assert session.get(ApprovalWorkflow, workflow_id).status == WORKFLOW_DRAFT


def test_activation_refuses_a_remind_step_with_no_interval(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][0]["on_timeout"] = "remind"
    graph["steps"][0]["escalate_to"] = None
    graph["steps"][0]["remind_every_hours"] = None
    admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)

    assert "STP-1" in _messages(_activate(admin, workflow_id).json())


def test_activation_refuses_an_escalation_target_nobody_holds(admin):
    """Absence is denial: a target that resolves to nobody routes nowhere."""
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"][0]["escalate_to"] = "role:nobody_at_all"
    admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)

    grouped = _messages(_activate(admin, workflow_id).json())
    assert "STP-1" in grouped
    assert any("nobody_at_all" in message for message in grouped["STP-1"])


def test_activation_refuses_a_graph_with_two_starting_points(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["edges"] = []
    admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)

    payload = _activate(admin, workflow_id).json()
    assert payload["ok"] is False
    assert any("start" in error["message"].lower() for error in payload["errors"])


def test_activation_refuses_a_loop(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["edges"].append({"from": "STP-2", "to": "STP-1"})
    admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)

    payload = _activate(admin, workflow_id).json()
    assert payload["ok"] is False
    assert any("loop" in error["message"].lower() for error in payload["errors"])


def test_activation_refuses_an_empty_graph(admin):
    workflow_id = _make_workflow()
    graph = _graph(admin, workflow_id)
    graph["steps"], graph["edges"] = [], []
    admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)

    assert _activate(admin, workflow_id).json()["ok"] is False


def test_a_good_graph_activates_and_is_audited(admin):
    workflow_id = _make_workflow()
    assert _activate(admin, workflow_id).json() == {"ok": True}

    with session_scope() as session:
        row = session.get(ApprovalWorkflow, workflow_id)
        assert row.status == WORKFLOW_ACTIVE
        assert row.activated_at is not None
        actions = {
            event.action
            for event in session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .all()
        }
        assert admin_view.ACTION_WORKFLOW_ACTIVATED in actions
        assert verify_chain(session, COMPANY)


def test_activating_a_second_route_archives_the_first_and_names_it(admin):
    """One live route per company, and the chain back to the one it replaced."""
    first = _make_workflow(workflow_id="WF-0001")
    assert _activate(admin, first).json() == {"ok": True}

    second = _make_workflow(workflow_id="WF-0002")
    assert _activate(admin, second).json() == {"ok": True}

    with session_scope() as session:
        assert session.get(ApprovalWorkflow, first).status == WORKFLOW_ARCHIVED
        replacement = session.get(ApprovalWorkflow, second)
        assert replacement.status == WORKFLOW_ACTIVE
        assert replacement.supersedes_id == first


def test_an_active_route_refuses_a_save(admin):
    """Activation freezes. A run in flight must not change under it."""
    workflow_id = _make_workflow()
    assert _activate(admin, workflow_id).json() == {"ok": True}

    graph = _graph(admin, workflow_id)
    graph["steps"][0]["label"] = "Rewritten after the fact"
    response = admin.post(f"{LIST_URL}/{workflow_id}/graph", json=graph)
    assert response.json()["ok"] is False
    assert _graph(admin, workflow_id)["steps"][0]["label"] == "Obligation owner review"


def test_an_active_route_refuses_a_second_activation(admin):
    workflow_id = _make_workflow()
    assert _activate(admin, workflow_id).json() == {"ok": True}
    assert _activate(admin, workflow_id).json()["ok"] is False


# ------------------------------------------------------------- one tenant


def test_another_company_reaches_none_of_it(anonymous, monkeypatch):
    workflow_id = _make_workflow()
    with session_scope() as session:
        ensure_system_roles(session)
        user = create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival admin",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        grant_role(
            session,
            RIVAL,
            user_id=user.id,
            role_name=ROLE_ADMIN,
            actor="system:test",
        )

    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    assert _sign_in(anonymous, RIVAL_EMAIL, RIVAL_PASSWORD).status_code == 303
    monkeypatch.setenv(deps.COMPANY_ENV, COMPANY)

    listing = anonymous.get(LIST_URL)
    assert listing.status_code == 200
    assert workflow_id not in listing.text

    assert anonymous.get(f"{LIST_URL}/{workflow_id}").status_code == 404
    assert anonymous.get(f"{LIST_URL}/{workflow_id}/graph").status_code == 404
    assert anonymous.post(f"{LIST_URL}/{workflow_id}/graph", json={}).status_code == 404
    assert _activate(anonymous, workflow_id).status_code == 404

    with session_scope() as session:
        assert session.get(ApprovalWorkflow, workflow_id).status == WORKFLOW_DRAFT


# -------------------------------------------------------- the read-only route


def test_the_route_screen_is_open_to_any_signed_in_user(analyst):
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    page = analyst.get(ROUTE_URL)
    assert page.status_code == 200
    assert "Obligation owner review" in page.text


def test_the_route_screen_refuses_an_anonymous_request(anonymous):
    assert anonymous.get(ROUTE_URL, follow_redirects=False).status_code == 303


def test_the_route_screen_says_so_when_no_route_is_live(analyst):
    """A blank screen would read as "there is no approval step". Not the same fact."""
    _make_workflow()
    page = analyst.get(ROUTE_URL)
    assert page.status_code == 200
    assert "no active approval route" in page.text.lower()


def test_the_route_screen_writes_the_timeout_rule_as_a_sentence(analyst):
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    body = analyst.get(ROUTE_URL).text
    assert "on_timeout" not in body
    assert "obligation_owner" not in body
    assert "After 24 hours" in body


def test_the_route_screen_does_not_soften_a_bypass(analyst):
    """Somebody reading this is deciding whether to trust the process."""
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    body = analyst.get(ROUTE_URL).text
    assert "WITHOUT approval" in body
    assert "After 48 hours" in body


def test_the_route_screen_names_who_can_change_it(analyst):
    """A user who thinks a rule is hard-coded stops asking for the rule they need."""
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    body = analyst.get(ROUTE_URL).text
    assert "workflow.manage" in body
    # No link, because following it would be refused.
    assert f'href="{LIST_URL}"' not in body


def test_the_same_line_becomes_a_link_for_somebody_who_may_edit(admin):
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    body = admin.get(ROUTE_URL).text
    assert f'href="{LIST_URL}"' in body


def _start_run(workflow_id: str, run_id: str = "WFR-1") -> str:
    """One escalation walked to step two, with step one already bypassed."""
    with session_scope() as session:
        session.add(
            WorkflowRun(
                id=run_id,
                company_id=COMPANY,
                workflow_id=workflow_id,
                escalation_id=ESCALATION,
                current_step_id="STP-2",
                status=WORKFLOW_RUN_RUNNING,
            )
        )
        session.flush()
        session.add(
            WorkflowStepRun(
                company_id=COMPANY,
                run_id=run_id,
                step_id="STP-1",
                outcome=OUTCOME_BYPASSED,
            )
        )
        session.add(
            WorkflowStepRun(
                company_id=COMPANY, run_id=run_id, step_id="STP-2", outcome=None
            )
        )
    return run_id


def test_the_route_screen_shows_where_an_item_has_reached(analyst):
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE
    _start_run(workflow_id)

    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert ESCALATION in body
    assert "Waiting here now" in body


def test_a_bypassed_step_never_renders_as_an_approved_one(analyst):
    """The same defect as a claim asserted on a citation that did not verify."""
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE
    _start_run(workflow_id)

    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "Skipped without approval" in body
    assert "Approved" not in body

    # And the other way round: an approved step says approved.
    with session_scope() as session:
        row = (
            session.query(WorkflowStepRun)
            .filter(WorkflowStepRun.step_id == "STP-1")
            .one()
        )
        row.outcome = OUTCOME_APPROVED
    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "Approved" in body
    assert "Skipped without approval" not in body


def test_a_timestamp_says_which_moment_it_is(analyst):
    """The regression guard on a bare coordinate reading as a claim.

    A stamp printed beside an open step reads as "answered then"; the same stamp
    beside a bypassed one reads as "signed then". Both are false, and the fix is
    that the word travels with the number.
    """
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE
    _start_run(workflow_id)
    with session_scope() as session:
        for row in session.query(WorkflowStepRun).all():
            if row.step_id == "STP-1":
                row.acted_at = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
            else:
                row.due_at = datetime(2026, 8, 9, 17, 0, tzinfo=timezone.utc)

    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "recorded 2026-08-01 09:00 UTC" in body
    assert "due 2026-08-09 17:00 UTC" in body


def test_a_bypassed_step_names_no_actor(analyst):
    """The clock is not a person, so nothing here may print one as though it were."""
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE
    _start_run(workflow_id)

    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "Nobody acted. The clock did." in body


def test_an_item_with_no_run_says_so_rather_than_showing_nothing(analyst):
    """A fallback announces itself. Silence would read as "it has not started"."""
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE

    body = analyst.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "has not been started" in body


def test_another_company_s_item_is_not_reported_as_this_company_s(anonymous, monkeypatch):
    workflow_id = _make_workflow()
    with session_scope() as session:
        session.get(ApprovalWorkflow, workflow_id).status = WORKFLOW_ACTIVE
    _start_run(workflow_id)

    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival admin",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    assert _sign_in(anonymous, RIVAL_EMAIL, RIVAL_PASSWORD).status_code == 303
    monkeypatch.setenv(deps.COMPANY_ENV, COMPANY)

    body = anonymous.get(f"{ROUTE_URL}?escalation={ESCALATION}").text
    assert "Obligation owner review" not in body
    assert "Waiting here now" not in body


# --------------------------------------------------------------- the assets


# The one absolute URL either asset may contain. It is the SVG namespace, which
# createElementNS takes as an identifier and never fetches -- browsers resolve it
# internally and a machine with no network draws the same paths. Named here so
# the check below can be exact rather than approximate.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def test_the_editor_assets_are_served_and_fetch_nothing(admin):
    """No CDN, no font, no build step. It has to work with the network off."""
    for path in ("/static/workflow.js", "/static/workflow.css"):
        response = admin.get(path)
        assert response.status_code == 200, path
        body = response.text.replace(SVG_NAMESPACE, "")

        assert "http://" not in body, path
        assert "https://" not in body, path
        # Protocol-relative, @import and a remote url() are the three other
        # shapes a stylesheet or a script uses to reach off the host.
        assert "//www." not in body, path
        assert "@import" not in body, path
        assert "url(http" not in body, path
