"""The action approval screen, and the one control the product rests on.

WHAT THIS FILE IS FOR. app/auth/policy.py::can_approve was written, unit-tested,
exported -- and called by nothing. docs/prd.html said the gap out loud: "the
monitor, comment and comply vocabulary exists in code and no screen renders it".
An action that nobody can propose is an action nobody can approve, so the
segregation-of-duties gate had no decision to gate. These tests hold the screen
that gives it one.

THE TEST THIS FILE EXISTS FOR is test_the_person_who_proposed_it_is_refused. In
SEGREGATED mode -- which is what an unset STRATA_APPROVAL_MODE means, and what
production runs -- somebody who holds action.approve and who already acted on
the claim is refused, and the refusal names the separation and cites the audit
row that proves it. Every other test here is scaffolding around that one.

WHY THE REFUSED PERSON HOLDS TWO ROLES. Gate 4 can only be reached by somebody
who got past gate 2, so the person must hold action.approve AND have touched the
claim. In the seeded grid nobody does: the analyst proposes and cannot approve,
the obligation owner approves and cannot propose. The arrangement is reached
here the same way a real deployment reaches it -- an admin grants a second role
through app/state/identity.py::grant_role, which has no ceiling and says so in
its own docstring. app/state/models.py::SEGREGATION_CONFLICTS already declares
that pair, and the permissions register already prints it. What was missing was
anything that actually stopped the act.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http drops it in silence.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import policy
from app.interpretation.action import ACTION_COMPLY, ACTION_MONITOR
from app.seed import (
    DEMO_PASSWORD,
    demo_account_list,
    ensure_accounts,
    ensure_demo_actions,
    load,
)
from app.state import actions as store
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTION_ACTION_APPROVED,
    ACTION_ACTION_PROPOSED,
    ACTION_ACTION_REJECTED,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role, user_by_email
from app.state.models import (
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    AuditEvent,
    ProposedAction,
)
from app.web import deps
from app.web.deps import install_auth
from app.web.views import actions as screen
from app.web.views import auth as auth_view

COMPANY = "MEP"
RIVAL = "RIVAL"

RIVAL_EMAIL = "owner@rival.example"
RIVAL_PASSWORD = "rival-owner-password"

# The corpus rows the seeded demonstration hangs off. Named rather than
# discovered, so a change to the data file fails a test instead of quietly
# moving what the demonstration shows.
FINAL_CLAIM = "CLM-CHG-5"
FINAL_CHANGE = "CHG-v2-v3-004"
DRAFT_CLAIM = "CLM-CHG-1"
DRAFT_CHANGE = "CHG-v1-v2-004"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def default_tenant(monkeypatch):
    """The default company, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture(autouse=True)
def segregated():
    """Every test here assumes the safe mode, so it says so rather than hoping.

    STRATA_APPROVAL_MODE is read once at import (app/auth/policy.py explains
    why), so this asserts rather than sets. A suite run with the demo downgrade
    exported would otherwise report the control passing while it was switched
    off, which is the one result this file must never produce.
    """
    assert policy.APPROVAL_MODE == policy.SEGREGATED, (
        "these tests describe SEGREGATED behaviour and the process is running "
        f"under {policy.APPROVAL_MODE}. Unset {policy.ENV_APPROVAL_MODE}."
    )


@pytest.fixture
def anonymous() -> TestClient:
    """The corpus, the accounts, the seeded proposals, behind the real guard."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        ensure_demo_actions(session)

    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(screen.router)
    install_auth(app)
    return TestClient(app, base_url="https://testserver")


def _email(role: str) -> str:
    return next(a.email for a in demo_account_list() if a.role == role)


def _sign_in(client: TestClient, email: str, password: str = DEMO_PASSWORD):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


@pytest.fixture
def owner(anonymous: TestClient) -> TestClient:
    """Signed in as a seeded obligation owner: holds action.approve, touched nothing."""
    assert _sign_in(anonymous, _email(ROLE_OBLIGATION_OWNER)).status_code == 303
    return anonymous


@pytest.fixture
def analyst(anonymous: TestClient) -> TestClient:
    """Signed in as the seeded analyst: proposes, and may never approve."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
    return anonymous


def _waiting() -> list[ProposedAction]:
    with session_scope() as session:
        return list(store.proposed_actions_for_company(session, COMPANY))


def _one_waiting() -> ProposedAction:
    rows = [row for row in _waiting() if row.state == store.STATE_PROPOSED]
    assert rows, "the seed left nothing waiting for a decision"
    return rows[0]


def _events() -> list[AuditEvent]:
    with session_scope() as session:
        return (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq)
            .all()
        )


def _both_roles(email: str) -> str:
    """Give a seeded person the second role, and return their user id.

    This is the hole app/state/identity.py concedes in its own docstring:
    grant_role takes a display string for its actor and so has no ceiling to
    compute, which means an admin can hand out any system role, including one
    that puts both halves of a declared separation on one person. Nothing here
    invents a capability -- it uses the one the product already has, which is
    the only way to reach gate 4 at all.
    """
    with session_scope() as session:
        person = user_by_email(session, COMPANY, email)
        grant_role(
            session,
            COMPANY,
            user_id=person.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )
        return person.id


# ------------------------------------------------------- the vocabulary exists


def test_the_seed_leaves_a_proposed_action_on_a_final_change():
    """comply on a FINAL order. The action half of the product, on a row.

    Until this landed, ACTION_COMPLY was a constant in
    app/interpretation/action.py with nothing to hang it on: no table, no
    screen, and therefore no decision for the approval gate to gate.
    """
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        ensure_demo_actions(session)

        rows = store.proposed_actions_for_company(session, COMPANY)
        kinds = {row.kind for row in rows}
        assert ACTION_COMPLY in kinds
        assert ACTION_MONITOR in kinds

        final = next(row for row in rows if row.claim_id == FINAL_CLAIM)
        assert final.change_id == FINAL_CHANGE
        assert final.kind == ACTION_COMPLY
        assert final.state == store.STATE_PROPOSED
        # Proposed by the analyst, so an obligation owner can approve it. A seed
        # whose only proposer also holds action.approve would leave the screen
        # refusing everybody on the live demonstration.
        assert final.proposed_by_user_id is not None


def test_the_seed_is_idempotent():
    """Run it twice and the numbers do not move, like every other loader here."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        first = ensure_demo_actions(session)
        second = ensure_demo_actions(session)
        assert first == second
        assert len(store.proposed_actions_for_company(session, COMPANY)) == len(first)


# ------------------------------------------------------------- the happy path


def test_the_holder_of_action_approve_approves_work_somebody_else_did(owner):
    """The screen exists so that this can happen, and so that the next one cannot."""
    row = _one_waiting()
    response = owner.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with session_scope() as session:
        after = store.proposed_action_for_company(session, COMPANY, row.id)
        assert after.state == store.STATE_APPROVED
        assert after.decided_by_user_id is not None
        assert after.decided_by_user_id != after.proposed_by_user_id
        assert _email(ROLE_OBLIGATION_OWNER) in after.decided_by
        # The policy's own sentence, kept rather than paraphrased.
        assert policy.APPROVE in after.decision_reason


def test_approving_appends_exactly_one_audit_row_and_the_chain_still_verifies(owner):
    """One decision, one row, naming the approver. And the chain still holds.

    Exactly one, because a clean approval writes nothing from the policy itself
    -- can_approve audits refusals and waivers, never a plain yes -- so a second
    row here would mean the decision was recorded twice under two spellings.
    """
    row = _one_waiting()
    before = len(_events())

    assert (
        owner.post(
            f"{screen.ACTIONS_URL}/{row.id}/decide",
            data={"decision": screen.DECISION_APPROVE},
            follow_redirects=False,
        ).status_code
        == 303
    )

    after = _events()
    assert len(after) == before + 1

    entry = after[-1]
    assert entry.action == ACTION_ACTION_APPROVED
    assert entry.subject_id == row.id
    assert _email(ROLE_OBLIGATION_OWNER) in entry.actor
    assert entry.actor_user_id is not None

    with session_scope() as session:
        assert verify_chain(session, COMPANY) is True


def test_rejecting_records_the_reason_and_leaves_the_record(owner):
    """A rejection with no reason is not auditable, so it is refused."""
    row = _one_waiting()

    blank = owner.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_REJECT, "reason": "   "},
        follow_redirects=False,
    )
    assert blank.status_code == 400
    assert screen.REASON_REQUIRED.capitalize() in blank.text

    with session_scope() as session:
        assert (
            store.proposed_action_for_company(session, COMPANY, row.id).state
            == store.STATE_PROPOSED
        )

    given = owner.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_REJECT, "reason": "the deadline is not ours"},
        follow_redirects=False,
    )
    assert given.status_code == 303

    with session_scope() as session:
        after = store.proposed_action_for_company(session, COMPANY, row.id)
        assert after.state == store.STATE_REJECTED
        assert "the deadline is not ours" in after.decision_reason

    assert _events()[-1].action == ACTION_ACTION_REJECTED


# ------------------------------------------------------------ THE CONTROL


def test_the_person_who_proposed_it_is_refused(anonymous):
    """THE TEST THIS FILE EXISTS FOR.

    Somebody holding action.approve, who already acted on the claim, is refused
    in SEGREGATED mode -- and the refusal names the separation and cites the
    audit row that proves it. A permission is necessary and never sufficient.
    """
    who = _email(ROLE_OBLIGATION_OWNER)
    _both_roles(who)
    assert _sign_in(anonymous, who).status_code == 303

    proposed = anonymous.post(
        f"{screen.ACTIONS_URL}/propose",
        data={
            "claim_id": DRAFT_CLAIM,
            "kind": ACTION_MONITOR,
            "rationale": "watch the revised threshold through to the final order",
        },
        follow_redirects=False,
    )
    assert proposed.status_code == 303, proposed.text

    with session_scope() as session:
        mine = [
            row
            for row in store.proposed_actions_for_company(session, COMPANY)
            if row.claim_id == DRAFT_CLAIM and row.state == store.STATE_PROPOSED
        ]
        assert len(mine) == 1
        action_id = mine[0].id

    refused = anonymous.post(
        f"{screen.ACTIONS_URL}/{action_id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert refused.status_code == 403

    body = refused.text
    assert "The person who interprets a change does not approve" in body
    assert DRAFT_CLAIM in body
    # The evidence, cited. "you worked on this" is not checkable; an audit
    # sequence number is.
    assert "audit seq" in body

    with session_scope() as session:
        assert (
            store.proposed_action_for_company(session, COMPANY, action_id).state
            == store.STATE_PROPOSED
        )


def test_the_refusal_is_in_the_chain_and_survives_the_request(anonymous):
    """A refusal nobody recorded is a control nobody can audit afterwards.

    The write goes through the request's own session, so a handler that raised
    out of the transaction would roll the refusal back along with everything
    else -- the person is stopped and the record of stopping them is gone.
    """
    who = _email(ROLE_OBLIGATION_OWNER)
    _both_roles(who)
    assert _sign_in(anonymous, who).status_code == 303

    anonymous.post(
        f"{screen.ACTIONS_URL}/propose",
        data={
            "claim_id": DRAFT_CLAIM,
            "kind": ACTION_MONITOR,
            "rationale": "watch the revised threshold",
        },
        follow_redirects=False,
    )
    with session_scope() as session:
        action_id = next(
            row.id
            for row in store.proposed_actions_for_company(session, COMPANY)
            if row.claim_id == DRAFT_CLAIM
        )

    assert (
        anonymous.post(
            f"{screen.ACTIONS_URL}/{action_id}/decide",
            data={"decision": screen.DECISION_APPROVE},
            follow_redirects=False,
        ).status_code
        == 403
    )

    denials = [row for row in _events() if row.action == ACTION_ACCESS_DENIED]
    assert denials, "the refusal never reached the chain"
    assert denials[-1].subject_id == DRAFT_CLAIM
    assert who in denials[-1].actor

    with session_scope() as session:
        assert verify_chain(session, COMPANY) is True


def test_without_action_approve_the_refusal_is_a_different_one(analyst):
    """Gate 2 and gate 4 refuse for different reasons and must read differently.

    The analyst is stopped because they do not hold the permission at all. That
    is a fact about their grid row. Gate 4 stops somebody who does hold it,
    because of what they did -- and a screen that printed one sentence for both
    would teach an operator to read "you may not" where the product meant "you,
    specifically, may not, and here is the row".
    """
    row = _one_waiting()
    refused = analyst.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert f"does not hold {policy.APPROVE}" in refused.text
    # Gate 4's sentence cites the audit row that proves authorship. Gate 2 has
    # no row to cite, and must not borrow one. The standing explanation at the
    # top of the page carries the general rule, so the fragment tested for here
    # is the evidence rather than the argument.
    assert "audit seq" not in refused.text


def test_the_demo_downgrade_does_not_rescue_a_missing_permission():
    """DEMO_SELF_APPROVAL waives gate 4 alone, and policy.py says so.

    Written as a unit call rather than through the screen because the mode is
    read once at import. The point is the sentence in policy.py's docstring:
    the downgrade waives who may approve their own work, never whether the
    account holds the permission.
    """
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        person = user_by_email(session, COMPANY, _email(ROLE_ANALYST))
        allowed, reason = policy.can_approve(session, COMPANY, person.id, FINAL_CLAIM)
        assert allowed is False
        assert policy.APPROVE in reason


# ------------------------------------------------------------- proposing


def test_proposing_needs_action_propose(owner):
    """The obligation owner approves and does not shape what reaches them."""
    refused = owner.post(
        f"{screen.ACTIONS_URL}/propose",
        data={
            "claim_id": DRAFT_CLAIM,
            "kind": ACTION_MONITOR,
            "rationale": "watch it",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert store.PROPOSE in refused.text


def test_a_draft_cannot_be_complied_with(analyst):
    """ADR-005's vocabulary, enforced where an action is written rather than read.

    A draft order binds nobody. Offering comply on one is the most expensive
    error available in this domain, so the write refuses it and says which
    actions the status does allow.
    """
    refused = analyst.post(
        f"{screen.ACTIONS_URL}/propose",
        data={
            "claim_id": DRAFT_CLAIM,
            "kind": ACTION_COMPLY,
            "rationale": "file the compliance report",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert ACTION_MONITOR in refused.text

    with session_scope() as session:
        assert not [
            row
            for row in store.proposed_actions_for_company(session, COMPANY)
            if row.claim_id == DRAFT_CLAIM
        ]


def test_a_proposal_names_its_author_in_the_chain(analyst):
    """Authorship is read from the audit chain, so the proposal has to be in it."""
    before = len(_events())
    assert (
        analyst.post(
            f"{screen.ACTIONS_URL}/propose",
            data={
                "claim_id": DRAFT_CLAIM,
                "kind": ACTION_MONITOR,
                "rationale": "watch the revised threshold",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    after = _events()
    assert len(after) == before + 1
    assert after[-1].action == ACTION_ACTION_PROPOSED
    assert after[-1].subject_type == "claim"
    assert after[-1].subject_id == DRAFT_CLAIM
    assert after[-1].actor_user_id is not None


# --------------------------------------------------------------- tenancy


def test_another_company_gets_a_404_that_reveals_nothing(anonymous, monkeypatch):
    """Another tenant's id is indistinguishable from one that was never issued."""
    row = _one_waiting()

    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival owner",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        person = user_by_email(session, RIVAL, RIVAL_EMAIL)
        grant_role(
            session,
            RIVAL,
            user_id=person.id,
            role_name=ROLE_OBLIGATION_OWNER,
            actor="system:test",
        )

    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    assert _sign_in(anonymous, RIVAL_EMAIL, RIVAL_PASSWORD).status_code == 303

    answer = anonymous.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert answer.status_code == 404
    body = answer.text
    assert row.claim_id not in body
    assert row.change_id not in body
    assert row.kind not in body
    assert row.rationale not in body

    with session_scope() as session:
        assert (
            store.proposed_action_for_company(session, COMPANY, row.id).state
            == store.STATE_PROPOSED
        )


def test_the_queue_shows_no_other_tenants_work(anonymous, monkeypatch):
    """The list is scoped like every other read in this codebase."""
    row = _one_waiting()

    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival owner",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )

    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    assert _sign_in(anonymous, RIVAL_EMAIL, RIVAL_PASSWORD).status_code == 303

    body = anonymous.get(screen.ACTIONS_URL).text
    assert row.claim_id not in body
    assert row.rationale not in body


# ----------------------------------------------------------------- the screen


def test_the_screen_shows_what_is_waiting_and_who_proposed_it(owner):
    body = owner.get(screen.ACTIONS_URL).text
    row = _one_waiting()
    assert row.claim_id in body
    assert row.kind in body
    assert row.rationale in body
    assert row.proposed_by in body


def test_the_screen_says_the_permission_is_not_the_whole_answer(owner):
    """The control has to be legible before somebody meets it, not only after."""
    body = owner.get(screen.ACTIONS_URL).text
    assert screen.SEGREGATION_NOTE in body


def test_an_already_decided_action_cannot_be_decided_again(owner):
    row = _one_waiting()
    assert (
        owner.post(
            f"{screen.ACTIONS_URL}/{row.id}/decide",
            data={"decision": screen.DECISION_APPROVE},
            follow_redirects=False,
        ).status_code
        == 303
    )
    again = owner.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert again.status_code == 409


def test_an_anonymous_request_never_reaches_the_screen(anonymous):
    for method, path in (
        ("get", screen.ACTIONS_URL),
        ("post", f"{screen.ACTIONS_URL}/propose"),
    ):
        response = getattr(anonymous, method)(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


def test_the_admin_who_manages_people_does_not_approve_the_work(anonymous):
    """Administering accounts is not authority over regulatory work.

    The grid keeps action.approve off ROLE_ADMIN deliberately, and this is the
    screen where that omission finally means something.
    """
    assert _sign_in(anonymous, _email(ROLE_ADMIN)).status_code == 303
    row = _one_waiting()
    refused = anonymous.post(
        f"{screen.ACTIONS_URL}/{row.id}/decide",
        data={"decision": screen.DECISION_APPROVE},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert f"does not hold {policy.APPROVE}" in refused.text
