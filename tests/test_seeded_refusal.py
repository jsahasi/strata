"""The documented way to watch the approval gate refuse, executed.

WHY THIS FILE EXISTS. README step 6, ADR-91, the comment block in app/seed.py
and docs/.ai/briefing.html all told a reviewer the same thing: sign in as the
admin, open /users, give the analyst the obligation owner role, then watch
Strata refuse her own approval. No screen does that. /users provisions a NEW
login and nothing anywhere adds a role to an account that already exists, so a
reviewer following the walkthrough reached a dead end at the exact moment the
control was supposed to appear. The code was right; the sentence telling anyone
how to watch it work was wrong, in four places.

WHAT REPLACED IT. app/seed.py::ensure_refusal_arrangement, off unless
STRATA_DEMO_REFUSAL is set, puts both halves of the declared
action.propose/action.approve pair on one seeded person -- as the system actor,
which is what no human in the role grid can do. The tests below hold three
facts: the default seed still gives everybody one role, the switch produces the
refusal end to end through the real POST, and no product route can produce the
same arrangement. The last one is the guard that was missing. It states in a
test what the docs now state in prose, so the two cannot drift apart again
without something going red.
"""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import seed
from app.auth import policy
from app.seed import (
    DEMO_PASSWORD,
    REFUSAL_ENV,
    ensure_accounts,
    ensure_demo_actions,
    ensure_refusal_arrangement,
    load,
)
from app.state import actions as store
from app.state.audit import verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    permissions_for_user,
    role_grants_for_user,
    roles_for_company,
    user_by_email,
)
from app.state.models import ROLE_ANALYST, ROLE_OBLIGATION_OWNER
from app.web import deps
from app.web.deps import install_auth
from app.web.views import actions as screen
from app.web.views import auth as auth_view
from app.web.views import permissions as permissions_screen
from app.web.views import users_admin

COMPANY = "MEP"

ANALYST_EMAIL = "denise.okoro@mep.example"
ADMIN_EMAIL = "sarah.lindqvist@mep.example"
OWNER_EMAIL = "priya.nandakumar@mep.example"


@pytest.fixture(autouse=True)
def default_tenant(monkeypatch):
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture(autouse=True)
def segregated():
    """SEGREGATED or nothing here. The mode is read once, at import."""
    assert policy.APPROVAL_MODE == policy.SEGREGATED, (
        "these tests describe SEGREGATED behaviour and the process is running "
        f"under {policy.APPROVAL_MODE}. Unset {policy.ENV_APPROVAL_MODE}."
    )


@pytest.fixture
def seeded():
    """The corpus, the accounts, the proposals. No refusal arrangement."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        ensure_demo_actions(session)


@pytest.fixture
def client(seeded) -> TestClient:
    """Every screen a reviewer is told to open, behind the real guard."""
    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(screen.router)
    app.include_router(users_admin.router)
    app.include_router(permissions_screen.router)
    install_auth(app)
    return TestClient(app, base_url="https://testserver")


def _sign_in(client: TestClient, email: str) -> None:
    answer = client.post(
        "/login",
        data={"email": email, "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert answer.status_code == 303, answer.text


def _text(body: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def _roles_held(session, email: str) -> set[str]:
    """The names of the roles this person holds now. Revoked grants left out."""
    person = user_by_email(session, COMPANY, email)
    names = {role.id: role.name for role in roles_for_company(session, COMPANY)}
    return {
        names[grant.role_id]
        for grant in role_grants_for_user(
            session, COMPANY, person.id, include_revoked=False
        )
    }


# ------------------------------------------------ the default is one role each


def test_the_default_seed_gives_the_analyst_one_role(seeded):
    """Off unless asked for. The clean grid is what a reviewer meets first."""
    with session_scope() as session:
        person = user_by_email(session, COMPANY, ANALYST_EMAIL)
        assert _roles_held(session, ANALYST_EMAIL) == {ROLE_ANALYST}
        assert policy.APPROVE not in permissions_for_user(session, COMPANY, person.id)


# ------------------------------------- the switch, and the refusal it produces


def test_the_switch_puts_both_halves_on_the_analyst(seeded):
    with session_scope() as session:
        assert ensure_refusal_arrangement(session) == ANALYST_EMAIL
        person = user_by_email(session, COMPANY, ANALYST_EMAIL)
        assert _roles_held(session, ANALYST_EMAIL) == {ROLE_ANALYST, ROLE_OBLIGATION_OWNER}
        assert policy.APPROVE in permissions_for_user(session, COMPANY, person.id)


def test_the_arrangement_is_idempotent(seeded):
    with session_scope() as session:
        assert ensure_refusal_arrangement(session) == ANALYST_EMAIL
        assert ensure_refusal_arrangement(session) == ANALYST_EMAIL
        assert _roles_held(session, ANALYST_EMAIL) == {ROLE_ANALYST, ROLE_OBLIGATION_OWNER}
        assert (
            len(
                role_grants_for_user(
                    session,
                    COMPANY,
                    user_by_email(session, COMPANY, ANALYST_EMAIL).id,
                    include_revoked=False,
                )
            )
            == 2
        )


def test_the_documented_walkthrough_ends_in_the_segregation_refusal(client):
    """README step 6, executed: the switch, then her own proposal, then 403.

    This is the test the whole file is for. It drives the real POST through the
    real guard, so a change that leaves the walkthrough pointing at something
    the product cannot do fails here rather than in front of a reviewer.
    """
    with session_scope() as session:
        ensure_refusal_arrangement(session)
        mine = [
            row
            for row in store.proposed_actions_for_company(session, COMPANY)
            if row.state == store.STATE_PROPOSED
        ]
        assert mine, "the seed proposed nothing for the analyst to be refused over"
        target, claim_id = mine[0].id, mine[0].claim_id

    _sign_in(client, ANALYST_EMAIL)
    answer = client.post(
        f"{screen.ACTIONS_URL}/{target}/decide",
        data={"decision": "approve", "reason": "it is my own proposal"},
        follow_redirects=False,
    )
    assert answer.status_code == 403, answer.text

    said = _text(answer.text)
    assert ANALYST_EMAIL in said
    assert f"acted on claim {claim_id} already" in said
    assert "does not approve the action that follows from it" in said

    with session_scope() as session:
        assert verify_chain(session, COMPANY) is True
        assert (
            store.proposed_action_for_company(session, COMPANY, target).state
            == store.STATE_PROPOSED
        )


def test_an_untouched_owner_still_approves_after_the_switch(client):
    """The switch refuses one person. It does not switch the screen off."""
    with session_scope() as session:
        ensure_refusal_arrangement(session)
        target = [
            row
            for row in store.proposed_actions_for_company(session, COMPANY)
            if row.state == store.STATE_PROPOSED
        ][0].id

    _sign_in(client, OWNER_EMAIL)
    answer = client.post(
        f"{screen.ACTIONS_URL}/{target}/decide",
        data={"decision": "approve", "reason": "ours, and I wrote none of it"},
        follow_redirects=False,
    )
    assert answer.status_code == 303, answer.text


# ---------------------------------- why the walkthrough cannot be a screen


def test_no_product_route_puts_action_approve_on_an_existing_account(client):
    """The fact the docs now state, stated as a test.

    The admin holds user.manage and does not hold action.approve, and every
    grant path in the product applies a ceiling to that: the role picker on
    /users offers only roles inside it, /permissions/grant refuses a code the
    granter does not hold, and provisioning an address that already has a login
    is refused before any role is considered. So the arrangement above cannot
    be made from a screen by anybody the seeded grid contains.

    If somebody later builds a route that CAN, this test goes red -- which is
    the moment to rewrite the walkthrough to use it, rather than discovering
    the mismatch from a reviewer.
    """
    _sign_in(client, ADMIN_EMAIL)

    page = client.get(users_admin.USERS_URL)
    assert page.status_code == 200
    offered = set()
    for block in re.finditer(
        r'<select[^>]*name="role"[^>]*>(.*?)</select>', page.text, re.S
    ):
        offered.update(re.findall(r'value="([^"]*)"', block.group(1)))
    assert ROLE_OBLIGATION_OWNER not in offered

    with session_scope() as session:
        analyst_id = user_by_email(session, COMPANY, ANALYST_EMAIL).id

    refused = client.post(
        "/permissions/grant",
        data={
            "user_id": analyst_id,
            "code": policy.APPROVE,
            "reason": "to watch the refusal",
        },
    )
    assert "That grant was refused." in _text(refused.text)

    tampered = client.post(
        users_admin.PROVISION_URL,
        data={
            "email": ANALYST_EMAIL,
            "display_name": "Denise Okoro",
            "role": ROLE_OBLIGATION_OWNER,
        },
    )
    assert tampered.status_code == 200

    with session_scope() as session:
        person = user_by_email(session, COMPANY, ANALYST_EMAIL)
        assert _roles_held(session, ANALYST_EMAIL) == {ROLE_ANALYST}
        assert policy.APPROVE not in permissions_for_user(session, COMPANY, person.id)


# ------------------------------------------------- the docs name what runs


def test_the_readme_names_the_switch_the_seed_reads(repo_root):
    """One env name, in the walkthrough and in the code that reads it.

    The bug this file exists for was a walkthrough describing an act the
    product does not perform. A doc naming an environment variable the seed
    does not read is the same bug in a smaller form.
    """
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    assert REFUSAL_ENV in readme
    assert REFUSAL_ENV == "STRATA_DEMO_REFUSAL"


#: The four files that carried the false instruction, and the shape of it. The
#: verb matters: prose ABOUT the refusal is fine and there is plenty of it, but
#: an instruction to hand somebody the obligation owner role is a claim that a
#: route exists to do that, and none does.
CARRIED_THE_CLAIM = (
    "README.md",
    "docs/.ai/decisions.html",
    "docs/.ai/briefing.html",
    "app/seed.py",
)

#: What makes a sentence false is naming the SCREEN. "The seed grants the
#: analyst the obligation owner role" is true and has to stay sayable; "give the
#: analyst the obligation owner role at /users" is the claim that a route exists
#: to do it. So every pattern below requires /users inside the same sentence.
#: "Within the same sentence." A full stop followed by whitespace ends one; a
#: full stop inside denise.okoro@mep.example or permissions.py does not.
_SAME_SENTENCE = r"(?:(?!\.\s)[\s\S]){0,160}"

INSTRUCTIONS = (
    re.compile(rf"/users{_SAME_SENTENCE}obligation[ -]owner role", re.IGNORECASE),
    re.compile(rf"obligation[ -]owner role{_SAME_SENTENCE}/users", re.IGNORECASE),
    re.compile(rf"\brole grant\b{_SAME_SENTENCE}/users", re.IGNORECASE),
)


def _plain(body: str) -> str:
    """Tags out, whitespace collapsed. The docs are HTML and the prose is not."""
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


@pytest.mark.parametrize("relative", CARRIED_THE_CLAIM)
def test_no_document_tells_a_reviewer_to_hand_out_the_owner_role(repo_root, relative):
    """The four places that carried the false sentence, held to the true one.

    Matches the instruction rather than one wording. Nothing in the product
    puts the obligation owner role on an account that already exists, so a doc
    written in the imperative about doing so is describing a screen that is not
    there -- which is exactly the defect this file was opened for.
    """
    body = _plain((repo_root / relative).read_text(encoding="utf-8"))
    for pattern in INSTRUCTIONS:
        found = pattern.search(body)
        assert found is None, (
            f"{relative} still instructs somebody to hand out the obligation "
            f"owner role ({found.group(0)!r}). No route in the product can, and "
            f"{REFUSAL_ENV} is how the demonstration is reached instead."
        )


@pytest.mark.parametrize("relative", CARRIED_THE_CLAIM)
def test_each_of_them_names_the_switch_instead(repo_root, relative):
    """Having removed the false path, each file names the one that works."""
    body = (repo_root / relative).read_text(encoding="utf-8")
    assert REFUSAL_ENV in body


def test_the_seed_reads_the_switch_and_defaults_to_off(monkeypatch):
    monkeypatch.delenv(REFUSAL_ENV, raising=False)
    assert seed.refusal_requested() is False
    for on in ("1", "true", "yes", "ON"):
        monkeypatch.setenv(REFUSAL_ENV, on)
        assert seed.refusal_requested() is True
    for off in ("0", "false", "no", ""):
        monkeypatch.setenv(REFUSAL_ENV, off)
        assert seed.refusal_requested() is False
