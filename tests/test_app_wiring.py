"""The application a reviewer actually starts, assembled in app/main.py.

tests/test_screens.py and tests/test_change_view.py build their own FastAPI
objects around the routers, on purpose: a screen has to be testable before
anything wires it in. The gap that leaves is this one -- whether the object
`make run` and `uvicorn app.main:app` start is the same product those files
proved. A router nobody included, or a static mount at the wrong path, passes
every screen test in the repository and serves a reviewer a 404.

The tenant test is the one that matters. The tenant used to be answered twice:
app/web/deps.py returned a hardcoded "MEP" and app/web/views/proceedings.py read
STRATA_COMPANY_ID. Each was right about its own screens and the pair was wrong
about the product -- pointed at a second company, the list and the queue held
their tongue while the change screen served the first company's source text,
claims and name. The two answers are now one function, and the assertion below
is against the assembled application because that is the only place the
disagreement was ever visible.

Offline, no API key, no network.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import load
from app.state.db import init_db, session_scope
from app.state.models import Claim
from app.web import deps

COMPANY = "MEP"
RIVAL = "RIVAL"
DOCKET = "MPUC-2026-0142"

# The change carrying one claim that verifies and one that does not.
PAIRED = "CHG-v1-v2-003"
VERIFIED_CLAIM = "CLM-CHG-2"

MISQUOTE = "ESC-CLM-MISQUOTE"


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture
def client() -> TestClient:
    """The real corpus, behind the real application object."""
    init_db()
    with session_scope() as session:
        load(session)
    return TestClient(app)


def _statement(claim_id: str) -> str:
    with session_scope() as session:
        claim = session.get(Claim, claim_id)
        assert claim is not None, f"{claim_id} is not in the seeded corpus"
        return claim.statement


# ------------------------------------------------------------------ every screen


def test_every_screen_is_reachable_on_the_assembled_app(client):
    """A router left out of app/main.py is a 404 no screen test would catch."""
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get(f"/proceedings/{DOCKET}").status_code == 200
    assert client.get(f"/changes/{PAIRED}").status_code == 200
    assert client.get("/review").status_code == 200


def test_the_static_files_are_served_where_base_html_looks_for_them(client):
    """base.html links both at fixed absolute paths. A 404 here is a bare page."""
    body = client.get("/").text
    assert '<link rel="stylesheet" href="/static/strata.css">' in body

    assert client.get("/static/strata.css").status_code == 200
    assert client.get("/static/citation.js").status_code == 200


# ------------------------------------------------------------------- one tenant


def test_the_whole_app_answers_to_one_tenant(client, monkeypatch):
    """The regression guard: every route moves together when the tenant moves.

    Pointed at a company the corpus holds nothing for, no route may return a row
    -- and the change screen is the one that used to, because it resolved its
    tenant through a different function from the screens either side of it.
    """
    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    statement = _statement(VERIFIED_CLAIM)

    listing = client.get("/")
    assert listing.status_code == 200
    assert DOCKET not in listing.text

    assert client.get(f"/proceedings/{DOCKET}").status_code == 404

    change = client.get(f"/changes/{PAIRED}")
    assert change.status_code == 404
    assert statement not in change.text
    assert "Meridian" not in change.text
    assert "megawatts" not in change.text

    queue = client.get("/review")
    assert queue.status_code == 200
    assert MISQUOTE not in queue.text


def test_the_default_tenant_still_sees_its_own_rows(client):
    """Not vacuous: unconfigured, the same four routes serve the demo company."""
    assert DOCKET in client.get("/").text
    assert _statement(VERIFIED_CLAIM) in client.get(f"/changes/{PAIRED}").text
    assert MISQUOTE in client.get("/review").text
