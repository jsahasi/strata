"""Whether the assistant is on, said on a screen instead of guessed at.

WHY THIS EXISTS. The product has two modules that call a model, and both
announce themselves as off when no key reaches the process. That announcement is
honest and it is also the only way anybody could find out -- an administrator had
no screen anywhere that answered "is the assistant configured", so the way to
learn it was to ask Clarke a question and read the refusal. A state a person can
only discover by tripping over it is not a state the product has told them about.

WHAT IT MUST NEVER DO, and this is the half worth testing hardest. It reports
that a variable is SET. Never its value, never its length, never a prefix, never
a masked form with the last four characters showing -- app/state/sources.py made
that argument for source credentials first and this follows it exactly, because
two different answers to "how much of a secret may a screen show" is how the
looser one ends up in front of somebody.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.chat.agent import ENV_API_KEY, MODEL_ID
from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.db import init_db, session_scope
from app.state.models import ROLE_ADMIN, ROLE_ANALYST
from app.web import deps

SCREEN = "/admin/sources"

# Distinctive, and shaped like the real thing, so a test that passes because the
# key was empty could not also pass here.
FAKE_KEY = "sk-ant-api03-TESTVALUEmustNEVERreachTheScreen-000000000000"


def _account(role: str) -> str:
    return next(a.email for a in demo_account_list() if a.role == role)


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture
def client() -> TestClient:
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    c = TestClient(app, base_url="https://testserver")
    assert c.post(
        "/login",
        data={"email": _account(ROLE_ADMIN), "password": DEMO_PASSWORD},
        follow_redirects=False,
    ).status_code == 303
    return c


def test_the_screen_says_the_assistant_is_on_when_a_key_is_set(client, monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    body = client.get(SCREEN).text
    assert ENV_API_KEY in body, "the screen must name the variable it read"
    assert MODEL_ID in body, "the screen must name the model that is pinned"
    assert "not set" not in body.lower().split("assistant", 1)[-1][:400]


def test_the_screen_says_it_is_off_and_names_the_line_to_add(client, monkeypatch):
    """Absence is denial. An off assistant is stated, with the fix beside it."""
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    body = client.get(SCREEN).text
    assert ENV_API_KEY in body
    assert "not set" in body.lower()
    # The exact line, so nobody has to guess the spelling of the variable.
    assert f"{ENV_API_KEY}=" in body


def test_the_value_of_the_key_never_reaches_the_page(client, monkeypatch):
    """The one that matters. A boolean, and nothing else, ever.

    Checked against the whole rendered document rather than one element,
    because a value can leak through a title attribute, a data- attribute or a
    comment as easily as through visible text.
    """
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    body = client.get(SCREEN).text
    assert FAKE_KEY not in body
    # No prefix either. Twelve characters is enough to identify an account.
    assert FAKE_KEY[:12] not in body
    # The distinctive middle, in case a future masking scheme keeps the ends.
    assert "TESTVALUEmustNEVER" not in body

    # A LENGTH CHECK USED TO SIT HERE AND IT WAS WRONG, which is worth leaving a
    # note about rather than quietly deleting. It asserted str(len(FAKE_KEY))
    # was absent from the document -- the two characters "58". Any page carrying
    # a character offset, a byte count or a row total contains "58" sooner or
    # later, so the check failed on a page that leaks nothing and would have
    # passed on one that leaked a differently-sized key. A test that cannot fail
    # for the reason it names is worse than no test: this one would have been
    # "fixed" by widening it until it passed, and the widening is what people
    # remember rather than the assertion.


def test_an_analyst_cannot_read_the_model_status(client, monkeypatch):
    """It sits behind the same permission as the rest of the registry.

    Whether a company pays for a model is not a fact every signed-in person is
    entitled to, and the screen it lives on is already gated. This test exists so
    the block cannot be quietly moved somewhere ungated later.
    """
    monkeypatch.setenv(ENV_API_KEY, FAKE_KEY)
    anon = TestClient(app, base_url="https://testserver")
    assert anon.post(
        "/login",
        data={"email": _account(ROLE_ANALYST), "password": DEMO_PASSWORD},
        follow_redirects=False,
    ).status_code == 303
    response = anon.get(SCREEN)
    assert response.status_code in (403, 200)
    if response.status_code == 200:
        assert ENV_API_KEY not in response.text
        assert FAKE_KEY not in response.text


def test_the_two_model_modules_pin_the_same_id():
    """The screen prints one model id. Two modules must not disagree about it.

    app/chat/agent.py and app/interpretation/propose.py each declare MODEL_ID.
    A screen that reads one while the other calls something else would report a
    model nobody is using, which is worse than reporting none.
    """
    from app.interpretation.propose import MODEL_ID as PROPOSER_MODEL

    assert MODEL_ID == PROPOSER_MODEL, (
        "app/chat/agent.py and app/interpretation/propose.py pin different "
        "models. The registry screen can only truthfully print one."
    )


def test_presence_is_read_from_the_live_environment_not_cached_at_import():
    """A key added after start must show without a restart, and removed likewise.

    The opposite -- reading it once at import -- is the failure app/config.py was
    written for: a process that decided long ago what it had and never looked
    again.
    """
    from app.web.views.integrations import _model_status

    os.environ.pop(ENV_API_KEY, None)
    assert _model_status()["configured"] is False
    os.environ[ENV_API_KEY] = FAKE_KEY
    try:
        assert _model_status()["configured"] is True
    finally:
        os.environ.pop(ENV_API_KEY, None)
