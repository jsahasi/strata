"""The administrative screens, and the one place a person can find them.

WHAT WAS BROKEN. Six screens were mounted, answered 200 to an administrator who
typed the URL, and appeared on no screen anybody could reach: /users,
/permissions, /admin/shares, /admin/invites, /admin/sources and /admin/feedback.
The masthead carried six analyst screens and there was nowhere for an
administrative one to live. A feature nobody can navigate to is a feature nobody
has, and every screen test on all six passed while it was true.

tests/test_app_wiring.py::test_every_screen_a_person_can_open_is_reachable_by_
following_links is the guard that found it -- it signs in and crawls links from
the front door exactly as a person clicking would. That guard proves a way in
EXISTS. This file is the other half, and it proves the way in is HONEST.

WHAT "HONEST" MEANS HERE, and it is the whole design.

  A LINK IS DRAWN ONLY FOR SOMEBODY WHO CAN USE IT. A link that answers 403 on
  click is worse than no link: the person cannot tell "you may not" from "it is
  broken", and they file a bug about the second when the truth is the first.
  test_every_link_the_index_draws_opens_for_the_person_it_was_drawn_for follows
  every link on the page and asserts 200 rather than merely not-404.

  THE MENU IS COMPUTED, NEVER CACHED ON THE SESSION. app/web/deps.py::Principal
  deliberately carries no permissions, because a copy taken at sign-in would
  still say "may approve" after the grant was revoked mid-session. So the menu
  is decided per render from the live grant rows through app/auth/policy.py::
  has(). test_the_menu_follows_the_grant_and_not_the_session holds one session
  open, grants, reloads, revokes, reloads, and watches the masthead move both
  ways without anybody signing out.

  THE MENU REACHES EVERY SCREEN, NOT MOST OF THEM. There was one Jinja2Templates
  object per view module, so a template global registered in one place reached
  one screen and raised NameError-shaped emptiness on the rest. A masthead that
  works on twelve screens and not on four is worse than the masthead that was
  there before, because the reader learns the menu is unreliable rather than
  absent. Two tests below are derived from the modules and the routes rather
  than from a list somebody keeps: a hand-kept list is exactly what let the six
  screens go unlinked in the first place.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on plain http drops it in silence.
"""

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.db import init_db, session_scope
from app.state.identity import (
    ensure_system_roles,
    grant_role,
    revoke_role,
    user_by_email,
)
from app.state.models import PERMISSION_CODES, ROLE_ADMIN, ROLE_ANALYST
from app.web import deps
from app.web.templating import ADMIN_URL, admin_screens

COMPANY = "MEP"
DOCKET = "MPUC-2026-0142"
PAIRED = "CHG-v1-v2-003"

VIEWS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "views"

#: The six the strict xfail named. Written out ONCE, here, so that dropping one
#: from the registry fails a test instead of quietly un-linking a screen again.
#: Every other test in this file derives its subjects from the registry.
THE_SIX = (
    "/users",
    "/permissions",
    "/admin/shares",
    "/admin/invites",
    "/admin/sources",
    "/admin/feedback",
)

#: One screen from every module that extends base.html and takes no parameter,
#: plus the two that take one. The point of the list is coverage of the TEMPLATE
#: OBJECTS -- there is one per view module, and the masthead has to render the
#: same from all of them.
EVERY_SHELL = (
    "/",
    "/projects",
    "/proceedings",
    f"/proceedings/{DOCKET}",
    f"/changes/{PAIRED}",
    "/review",
    "/escalations",
    "/workflow",
    "/admin/workflows",
    ADMIN_URL,
) + THE_SIX


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


def _sign_in(client: TestClient, role: str) -> TestClient:
    email = next(
        account.email for account in demo_account_list() if account.role == role
    )
    response = client.post(
        "/login", data={"email": email, "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def admin(anonymous: TestClient) -> TestClient:
    return _sign_in(anonymous, ROLE_ADMIN)


@pytest.fixture
def analyst(anonymous: TestClient) -> TestClient:
    return _sign_in(anonymous, ROLE_ANALYST)


def _nav(body: str) -> str:
    """The masthead's own links, and nothing from the page below it."""
    assert '<nav aria-label="Screens">' in body, "this page has no masthead"
    return body.split('<nav aria-label="Screens">')[1].split("</nav>")[0]


def _analyst_id(session) -> str:
    email = next(
        account.email for account in demo_account_list() if account.role == ROLE_ANALYST
    )
    person = user_by_email(session, COMPANY, email)
    assert person is not None
    return person.id


# ------------------------------------------------------------------ the way in


def test_the_masthead_offers_the_admin_screens_to_somebody_who_may_open_them(admin):
    """The defect this whole change exists for, stated at its smallest."""
    assert f'href="{ADMIN_URL}"' in _nav(admin.get("/").text)


def test_the_masthead_offers_nothing_administrative_to_an_analyst(analyst):
    """An analyst holds none of the codes, so none of the links are drawn.

    Asserted against every URL in the registry rather than against the index
    alone: a later change that put the six screens straight into the masthead
    would still have to hide them from the person who cannot open them.
    """
    nav = _nav(analyst.get("/").text)
    assert f'href="{ADMIN_URL}"' not in nav
    for screen in admin_screens():
        assert f'href="{screen.url}"' not in nav, screen.url


def test_the_admin_link_is_on_every_screen_rather_than_on_most_of_them(admin):
    """One Jinja2Templates object per view module is the failure this catches.

    A global registered on one environment reaches one screen. The masthead is
    on all of them, so the menu has to be on all of them: a reader who finds
    Admin on the projects list and not on the review queue learns that the menu
    comes and goes, which is worse than never having had one.
    """
    for path in EVERY_SHELL:
        response = admin.get(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        assert f'href="{ADMIN_URL}"' in _nav(response.text), path


# ------------------------------------------------------------------- the index


def test_the_index_lists_every_administrative_screen_the_viewer_may_open(admin):
    body = admin.get(ADMIN_URL).text
    for screen in admin_screens():
        assert f'href="{screen.url}"' in body, screen.url
        assert screen.label in body, screen.label


def test_the_index_carries_all_six_screens_that_had_no_way_in():
    """The registry is the only place these six are named. Guard the list."""
    urls = {screen.url for screen in admin_screens()}
    missing = sorted(set(THE_SIX) - urls)
    assert not missing, (
        f"{missing} are back to having no way in. They are the screens the "
        "strict xfail in tests/test_app_wiring.py named."
    )


def test_the_index_names_the_permission_that_opens_each_screen(admin):
    """An administrator granting access needs the code, not a description of it."""
    body = admin.get(ADMIN_URL).text
    for screen in admin_screens():
        assert screen.permission in body, screen.permission


def test_every_link_the_index_draws_opens_for_the_person_it_was_drawn_for(admin):
    """Not "does not 404". Opens. A 403 on click is the failure being guarded.

    Every internal link on the rendered page, masthead included, followed with
    redirects OFF so that a link which bounces to the login page cannot pass as
    a 200.
    """
    body = admin.get(ADMIN_URL).text
    hrefs = {
        href.split("#")[0].split("?")[0]
        for href in re.findall(r'href="(/[^"]*)"', body)
    }
    hrefs = {href for href in hrefs if href and not href.startswith("/static")}
    assert hrefs, "the index rendered no links at all"

    for href in sorted(hrefs):
        status = admin.get(href, follow_redirects=False).status_code
        assert status == 200, f"the index links to {href}, which answered {status}"


def test_somebody_who_holds_none_of_the_permissions_is_refused_and_told_nothing(
    analyst,
):
    """A refusal that listed the screens would tell them what they may not see.

    workflow_list.html makes the same argument about the count of routes: a
    refusal page that leaks what is behind it has refused nothing.
    """
    response = analyst.get(ADMIN_URL)
    assert response.status_code == 403
    for screen in admin_screens():
        assert f'href="{screen.url}"' not in response.text, screen.url
        assert screen.label not in response.text, screen.label


# ------------------------------------------------- the grant, not the session


def test_the_menu_follows_the_grant_and_not_the_session(analyst):
    """Principal carries no permissions on purpose. This is why.

    One session, held open throughout. The role is granted and revoked
    underneath it and the masthead moves both ways without anybody signing out.
    A menu decided at sign-in would still be offering the administrative screens
    after the third step, which is the exact failure app/web/deps.py::Principal
    refuses to make possible.
    """
    assert f'href="{ADMIN_URL}"' not in _nav(analyst.get("/").text)

    with session_scope() as session:
        ensure_system_roles(session)
        grant_role(
            session,
            COMPANY,
            user_id=_analyst_id(session),
            role_name=ROLE_ADMIN,
            actor="system:test",
        )

    assert f'href="{ADMIN_URL}"' in _nav(analyst.get("/").text)
    assert analyst.get(ADMIN_URL).status_code == 200

    with session_scope() as session:
        revoke_role(
            session,
            COMPANY,
            user_id=_analyst_id(session),
            role_name=ROLE_ADMIN,
            actor="system:test",
        )

    assert f'href="{ADMIN_URL}"' not in _nav(analyst.get("/").text)
    assert analyst.get(ADMIN_URL).status_code == 403


# --------------------------------------------------- the class, not the line


def test_no_view_module_builds_a_jinja_environment_of_its_own():
    """Sixteen environments meant sixteen edits for one template global.

    Derived from the source rather than from a list of modules, because the
    module added next month is the one a list would miss -- and the symptom is
    a masthead that silently drops its menu on that screen alone.
    """
    offenders = sorted(
        path.name
        for path in VIEWS_DIR.glob("*.py")
        if "Jinja2Templates(" in path.read_text(encoding="utf-8")
    )
    assert not offenders, (
        f"{offenders} build their own Jinja environment. A template global "
        "registered by the shared factory does not reach base.html from one "
        "built here, so the masthead loses its menu on exactly those screens. "
        "Use app.web.templating.build_templates()."
    )


def _view_modules():
    """Every module under app/web/views that defines a router."""
    import importlib
    import pkgutil

    import app.web.views as views

    for info in pkgutil.iter_modules(views.__path__):
        module = importlib.import_module(f"app.web.views.{info.name}")
        if hasattr(module, "router"):
            yield info.name, module


def test_every_templates_object_in_the_views_package_can_render_the_masthead():
    """The other direction, asked of the objects rather than of the source."""
    from app.web.templating import MASTHEAD_GLOBALS

    missing = {}
    for name, module in _view_modules():
        templates = getattr(module, "templates", None)
        if templates is None:
            continue
        absent = sorted(set(MASTHEAD_GLOBALS) - set(templates.env.globals))
        if absent:
            missing[name] = absent

    assert not missing, (
        f"these view modules render base.html without what it reads: {missing}"
    )


def test_the_registry_names_routes_the_application_actually_serves():
    """A typo in a URL here draws a link to a 404 on every administrator's page."""
    mounted = {route.path for route in app.routes if hasattr(route, "path")}
    for screen in admin_screens():
        assert screen.url in mounted, f"{screen.url} is not a route this app serves"
        assert screen.permission in PERMISSION_CODES, (
            f"{screen.permission} is not a permission this product defines, so "
            "policy.has() would raise on every render of the masthead"
        )
