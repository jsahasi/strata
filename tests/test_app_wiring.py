"""The application a reviewer actually starts, assembled in app/main.py.

tests/test_screens.py and tests/test_change_view.py build their own FastAPI
objects around the routers, on purpose: a screen has to be testable before
anything wires it in. The gap that leaves is this one -- whether the object
`make run` and `uvicorn app.main:app` start is the same product those files
proved. A router nobody included, a static mount at the wrong path, or a guard
installed in a test helper and not in main.py all pass every screen test in the
repository and fail the reviewer.

Two questions this file asks that no screen test can.

**Is the wall there?** app/web/deps.py has a session guard and installing it is
one line in main.py. A screen test mounts one router and never notices whether
that line exists. The assertions below are made with redirects switched OFF,
because a client that follows them turns "sent to the login page" into a 200
and the test into a decoration.

**Whose data is on the page?** The tenant used to be answered twice --
app/web/deps.py returned a hardcoded "MEP" and app/web/views/proceedings.py read
STRATA_COMPANY_ID -- and each was right about its own screens while the pair was
wrong about the product. The answer is now the signed-in person's company, so
the test signs in as somebody from another company and watches every screen move
together. The environment variable is left pointing the other way while it does,
which is the only way to show the answer comes from the person.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http would drop it silently.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles
from app.state.models import ROLE_ANALYST, Claim
from app.web import deps

COMPANY = "MEP"
RIVAL = "RIVAL"
DOCKET = "MPUC-2026-0142"

# The change carrying one claim that verifies and one that does not.
PAIRED = "CHG-v1-v2-003"
VERIFIED_CLAIM = "CLM-CHG-2"

MISQUOTE = "ESC-CLM-MISQUOTE"

RIVAL_EMAIL = "analyst@rival.example"
RIVAL_PASSWORD = "rival-analyst-password"

# The escalation queue moved off /review when the review centre took that path.
ESCALATIONS = "/escalations"

SCREENS = (
    "/",
    "/projects",
    "/proceedings",
    f"/proceedings/{DOCKET}",
    f"/changes/{PAIRED}",
    "/review",
    ESCALATIONS,
)


def _analyst_email() -> str:
    """The seeded account holding the analyst role, from the corpus itself."""
    return next(
        account.email
        for account in demo_account_list()
        if account.role == ROLE_ANALYST
    )


def _sign_in(client: TestClient, email: str, password: str):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


@pytest.fixture(autouse=True)
def unset_company(monkeypatch):
    """Start every test from the default tenant, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture
def anonymous() -> TestClient:
    """The real corpus and the real accounts, behind the real application."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client(anonymous: TestClient) -> TestClient:
    """The same client, signed in as the seeded analyst."""
    assert _sign_in(anonymous, _analyst_email(), DEMO_PASSWORD).status_code == 303
    return anonymous


def _statement(claim_id: str) -> str:
    with session_scope() as session:
        claim = session.get(Claim, claim_id)
        assert claim is not None, f"{claim_id} is not in the seeded corpus"
        return claim.statement


# ------------------------------------------------------------------ the wall


def test_the_guard_is_installed_on_the_assembled_application(anonymous):
    """One missing line in main.py and every screen is public. Redirects off."""
    for path in SCREENS:
        response = anonymous.get(path, follow_redirects=False)
        assert response.status_code == 303, f"{path} answered an anonymous request"
        assert response.headers["location"].startswith("/login")


def test_the_way_in_and_the_health_check_stay_open(anonymous):
    """A wall with no door, or a health check behind it, is a broken deployment."""
    assert anonymous.get("/login").status_code == 200
    assert anonymous.get("/healthz").status_code == 200


def test_no_screen_leaks_a_claim_to_somebody_who_is_not_signed_in(anonymous):
    """The redirect must be a redirect, not a page with the answer in the body."""
    statement = _statement(VERIFIED_CLAIM)
    for path in SCREENS:
        body = anonymous.get(path, follow_redirects=False).text
        assert statement not in body
        assert DOCKET not in body


# ------------------------------------------------------------------ every screen


def test_every_screen_is_reachable_once_signed_in(client):
    """A router left out of app/main.py is a 404 no screen test would catch."""
    assert client.get("/healthz").status_code == 200
    for path in SCREENS:
        assert client.get(path, follow_redirects=False).status_code == 200, path


def test_the_static_files_are_served_where_base_html_looks_for_them(client):
    body = client.get("/").text
    assert '<link rel="stylesheet" href="/static/strata.css">' in body

    assert client.get("/static/strata.css").status_code == 200
    assert client.get("/static/citation.js").status_code == 200


# ------------------------------------------------------------------- one tenant


def test_the_whole_app_answers_to_the_company_of_whoever_signed_in(anonymous, monkeypatch):
    """The regression guard: every route moves together, and it moves with the person.

    Signed in as somebody from a company the corpus holds nothing for, no route
    may return a row -- and the change screen is the one that used to, because
    it resolved its tenant through a different function from the screens either
    side of it.

    STRATA_COMPANY_ID is left pointing at MEP for the whole of it. If any screen
    reads the variable instead of the person, it serves MEP's rows here and the
    assertions below fail.
    """
    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )

    # The form authenticates against the tenant the deployment is configured
    # for, so the rival signs in with the variable set to their company.
    monkeypatch.setenv(deps.COMPANY_ENV, RIVAL)
    assert _sign_in(anonymous, RIVAL_EMAIL, RIVAL_PASSWORD).status_code == 303
    monkeypatch.setenv(deps.COMPANY_ENV, COMPANY)

    statement = _statement(VERIFIED_CLAIM)

    listing = anonymous.get("/")
    assert listing.status_code == 200
    assert DOCKET not in listing.text

    assert anonymous.get(f"/proceedings/{DOCKET}").status_code == 404

    change = anonymous.get(f"/changes/{PAIRED}")
    assert change.status_code == 404
    assert statement not in change.text
    assert "Meridian" not in change.text
    assert "megawatts" not in change.text

    queue = anonymous.get("/review")
    assert queue.status_code == 200
    assert MISQUOTE not in queue.text


def test_the_seeded_analyst_still_sees_their_own_rows(client):
    """Not vacuous: the same four routes serve MEP to a person who belongs to it."""
    assert DOCKET in client.get("/").text
    assert _statement(VERIFIED_CLAIM) in client.get(f"/changes/{PAIRED}").text
    assert MISQUOTE in client.get(ESCALATIONS).text


# --------------------------------------------------- the class, not the line

# SCREENS above is hand-written, which is why it did not notice that
# app/web/views/projects.py and app/web/views/review_centre.py -- 2,000 lines
# and nine routes between them -- were included nowhere, while base.html linked
# to one of them from every page. A list a person maintains cannot catch a
# module that person forgot. The two tests below derive their answer from the
# application and the templates instead, so the next router left out fails here
# without anybody remembering to add it.


def _view_modules():
    """Every module under app/web/views that defines a router."""
    import importlib
    import pkgutil

    import app.web.views as views

    for info in pkgutil.iter_modules(views.__path__):
        module = importlib.import_module(f"app.web.views.{info.name}")
        if hasattr(module, "router"):
            yield info.name, module


def test_every_router_in_the_views_package_is_mounted(client):
    """A router nobody included is a 404 that every screen test still passes."""
    mounted = {route.path for route in app.routes if hasattr(route, "path")}

    missing = {
        name: [r.path for r in module.router.routes if r.path not in mounted]
        for name, module in _view_modules()
    }
    missing = {name: paths for name, paths in missing.items() if paths}

    assert not missing, f"routers defined but not included in app/main.py: {missing}"


def test_no_router_claims_a_path_another_router_already_owns(client):
    """Two routers on one path is the collision that kept these unmounted."""
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for name, module in _view_modules():
        for route in module.router.routes:
            for method in sorted(getattr(route, "methods", ()) or ()):
                key = (method, route.path)
                if key in seen and seen[key] != name:
                    clashes.append(f"{method} {route.path}: {seen[key]} and {name}")
                seen.setdefault(key, name)

    assert not clashes, "two routers claim one path: " + "; ".join(clashes)


def test_every_nav_link_the_masthead_renders_resolves(client):
    """base.html's own comment: no screen may render as a link that would 404."""
    hrefs = set()
    for path in SCREENS:
        body = client.get(path).text
        nav = body.split('<nav aria-label="Screens">')[1].split("</nav>")[0]
        hrefs |= set(re.findall(r'href="([^"]+)"', nav))

    assert hrefs, "the masthead rendered no links at all"
    for href in sorted(hrefs):
        status = client.get(href, follow_redirects=False).status_code
        assert status != 404, f"the nav links to {href}, which 404s"
