"""Every response carries the headers, and the guard is derived from the app.

WHY THIS FILE EXISTS. A `curl -sD -` at https://strata.sudama.ai/login returned
alt-svc, content-type, date, server and content-length. Nothing else. No frame
refusal, no nosniff, no HSTS. Four admin views set Referrer-Policy by hand and
every other route on the product sent none.

THE ATTACK THAT PICKED THE HEADERS. This product's whole claim is that a NAMED
PERSON approved something. Put /escalations in an invisible frame under a page
somebody wants to click, and a real approval arrives from a real person who
meant to click something else -- and the hash-chained audit log records, quite
faithfully, that they did it. A trustworthy log of a click nobody meant to make
is worse than no log, because it is evidence. X-Frame-Options: DENY and
frame-ancestors 'none' are the two spellings of the refusal, old and new, and
the product sends both because a browser that honours only one of them is still
a browser somebody approves from.

WHY THE SWEEPS ARE DERIVED AND NOT LISTED. A file holding the paths that must
carry headers is right on the day it is written and wrong on the day after,
because the next screen is mounted without it. Both sweeps below read
app.routes on the ASSEMBLED application -- the object `make run` starts -- so a
route added next month is swept because it exists.

NO ROUTE IS EXEMPT FROM THE ANONYMOUS SWEEP, and that is the point rather than
an oversight. An anonymous caller to a guarded path never reaches the view: the
session guard answers 303 and that redirect IS the response, so it has to carry
the headers too. Path parameters are filled with a placeholder for the same
reason -- what is being asked is whether the middleware stamped the response,
not what the response said. The signed-in sweep names its exclusions inline.

Offline, no API key, no network.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.db import init_db, session_scope
from app.state.models import ROLE_ADMIN
from app.web import STATIC_DIR
from app.web.headers import (
    ALWAYS,
    CSP,
    CSP_REPORT_ONLY,
    HSTS,
    HSTS_HEADER,
    SecurityHeadersMiddleware,
    header_pairs,
    hsts_applies,
)

# Filled into every path parameter. It matches no share token, no invitation and
# no id in the corpus, which is fine: a 404 and a 303 are both responses and
# both must carry the headers.
PLACEHOLDER = "derived-sweep-placeholder"

# The screen the attack aims at. It draws the escalation queue, and each row on
# it posts to /escalations/{id}/resolve -- the approval whose whole worth is the
# name attached to it.
APPROVAL_SCREEN = "/escalations"

#: Not opened by the signed-in sweep, each with its reason. The anonymous sweep
#: above it opens every one of these, so nothing here is unswept -- these are
#: excluded from the SECOND pass only.
SIGNED_IN_EXEMPT = {
    "/logout": "ends the session the sweep is holding, so nothing after it is signed in",
}


def _get_paths() -> list[str]:
    """Every GET path the assembled application serves, parameters filled in.

    Read off app.routes rather than listed. The static mount carries no methods
    and is swept separately by _static_asset().
    """
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "GET" not in methods:
            continue
        paths.add(re.sub(r"\{[^}]+\}", PLACEHOLDER, path))
    return sorted(paths)


def _static_asset() -> str:
    """A real file under the real static mount, both read off the application.

    Naming the stylesheet here would pass on the day it was written and stop
    proving anything the day the mount moved.
    """
    mount = next(route for route in app.routes if getattr(route, "name", "") == "static")
    name = sorted(path.name for path in STATIC_DIR.iterdir() if path.is_file())[0]
    return f"{mount.path}/{name}"


def _admin_email() -> str:
    return next(
        account.email for account in demo_account_list() if account.role == ROLE_ADMIN
    )


@pytest.fixture(scope="module")
def corpus():
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    return True


@pytest.fixture
def anonymous(corpus) -> TestClient:
    """The assembled application over https, nobody signed in."""
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def admin(anonymous: TestClient) -> TestClient:
    """The same client, signed in as the account that can open every screen."""
    response = anonymous.post(
        "/login",
        data={"email": _admin_email(), "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return anonymous


def _assert_stamped(response, where: str) -> None:
    """The four headers that go out on every response, whatever the status."""
    for name, value in ALWAYS.items():
        assert response.headers.get(name) == value, (
            f"{where} answered {response.status_code} without {name}"
        )
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"], (
        f"{where} sent a policy that does not refuse framing"
    )


# ------------------------------------------------------- the derived sweeps


def test_every_get_route_is_stamped_anonymously(anonymous: TestClient):
    """Walk app.routes. Ask each GET for a page with no session. Read the headers.

    Nothing is exempt. A guarded path answers 303 from the session guard and
    that redirect is a response a browser renders and follows, so it carries
    the headers or the sweep fails.
    """
    paths = _get_paths()
    assert len(paths) > 15, "app.routes yielded almost nothing, so this proves nothing"

    for path in paths:
        response = anonymous.get(path, follow_redirects=False)
        _assert_stamped(response, f"anonymous GET {path}")


def test_every_get_route_is_stamped_signed_in(admin: TestClient):
    """The same walk, holding a session, so the views themselves run.

    The anonymous pass never reaches a view -- the guard answers first. This one
    renders the real screens, which is where a route that writes its own
    Response object rather than returning a template would otherwise slip out
    unstamped.
    """
    swept = 0
    for path in _get_paths():
        if path in SIGNED_IN_EXEMPT or PLACEHOLDER in path:
            continue
        response = admin.get(path, follow_redirects=False)
        _assert_stamped(response, f"signed-in GET {path}")
        swept += 1
    assert swept > 15, "the signed-in sweep opened almost nothing"


def test_static_files_are_stamped(anonymous: TestClient):
    """The stylesheet too. StaticFiles is mounted, not routed, and is missed by
    any sweep that only walks APIRoute objects."""
    path = _static_asset()
    response = anonymous.get(path)
    assert response.status_code == 200
    _assert_stamped(response, f"static {path}")


def test_the_approval_screen_refuses_framing(admin: TestClient):
    """The named attack, asked directly rather than as one row of a sweep."""
    response = admin.get(APPROVAL_SCREEN)
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_login_page_is_stamped(anonymous: TestClient):
    """The page the measurement was taken against. It is public, so it is the
    one a stranger can frame without a session at all."""
    response = anonymous.get("/login")
    assert response.status_code == 200
    _assert_stamped(response, "GET /login")


# ------------------------------------------------------------------- HSTS


def test_hsts_over_https(anonymous: TestClient):
    response = anonymous.get("/login")
    assert response.headers[HSTS_HEADER] == HSTS


def test_no_hsts_over_plain_http(corpus):
    """A browser ignores HSTS off a plaintext response anyway. Sending it is
    still a claim the transport cannot back, so it is not sent."""
    client = TestClient(app, base_url="http://elsewhere.example")
    assert HSTS_HEADER not in client.get("/login").headers


def test_no_hsts_on_loopback(corpus):
    """`make run` serves http://127.0.0.1. Pin that and a developer has pinned
    their own machine to https for a year, and every other project they serve
    on that port breaks with it. The https spelling is refused too: the pin is
    what does the damage, and a local certificate does not make it harmless."""
    for base in (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "https://localhost:8000",
    ):
        client = TestClient(app, base_url=base)
        assert HSTS_HEADER not in client.get("/login").headers, base


def test_hsts_rule_agrees_with_the_cookie_rule():
    """One definition of "this machine". deps.py answers it for the cookie and
    this module must not answer it differently for the pin."""
    from starlette.requests import Request

    from app.web.deps import is_loopback_plaintext

    def _request(url: str) -> Request:
        scheme, _, rest = url.partition("://")
        host, _, path = rest.partition("/")
        return Request(
            {
                "type": "http",
                "scheme": scheme,
                "path": "/" + path,
                "query_string": b"",
                "headers": [(b"host", host.encode())],
                "server": (host.split(":")[0], 443 if scheme == "https" else 80),
            }
        )

    for url in ("http://127.0.0.1:8000/login", "http://localhost/login"):
        request = _request(url)
        assert is_loopback_plaintext(request), url
        assert not hsts_applies(request), url

    assert hsts_applies(_request("https://strata.sudama.ai/login"))
    assert not hsts_applies(_request("http://strata.sudama.ai/login"))


# ------------------------------------------- what the policy says, and does not


def test_the_enforced_policy_is_the_narrow_one():
    """Enforced: framing, base, plugins, form targets. Nothing about script or
    style, because breaking the demo silently is not a security win."""
    assert "frame-ancestors 'none'" in CSP
    assert "script-src" not in CSP
    assert "style-src" not in CSP


def test_the_report_only_policy_covers_script_and_style():
    """Report-only, so the console names a violation and no page loses a rule."""
    assert "script-src" in CSP_REPORT_ONLY
    assert "style-src" in CSP_REPORT_ONLY


def test_both_spellings_of_the_frame_refusal():
    """A browser honouring only one of them is still a browser somebody approves
    from, so both go out."""
    assert ALWAYS["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in ALWAYS["Content-Security-Policy"]


# ------------------------------------------------ a route's own header wins


def test_a_header_the_route_set_is_left_alone():
    """Set if absent, never overwrite. A view that chose a value chose it."""
    existing = [(b"referrer-policy", b"strict-origin-when-cross-origin")]
    merged = dict(header_pairs(existing, {"Referrer-Policy": "no-referrer"}))
    assert merged[b"referrer-policy"] == b"strict-origin-when-cross-origin"


def test_a_header_the_route_did_not_set_is_added():
    merged = dict(header_pairs([], {"Referrer-Policy": "no-referrer"}))
    assert merged[b"referrer-policy"] == b"no-referrer"


def test_no_duplicate_referrer_policy_on_a_route_that_sets_its_own(
    anonymous: TestClient,
):
    """/s/{token} sends its own Referrer-Policy. Two of them on one response is
    the shape of a middleware that appended rather than filled in."""
    response = anonymous.get(f"/s/{PLACEHOLDER}", follow_redirects=False)
    assert len(response.headers.get_list("referrer-policy")) == 1


# ------------------------------------------------------- wiring, not behaviour


def test_the_middleware_is_outside_the_session_guard():
    """Order is load-bearing. The guard's 303 has to be stamped, and it only is
    if this middleware wraps the guard rather than sitting inside it. Starlette
    inserts each added middleware at the front, so the outermost is index 0."""
    from app.web.deps import AuthMiddleware

    classes = [layer.cls for layer in app.user_middleware]
    assert classes.index(SecurityHeadersMiddleware) < classes.index(AuthMiddleware)


def test_non_http_scopes_pass_through():
    """A lifespan or websocket scope has no response headers to stamp and must
    not be handled as if it did."""
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope["type"])

    import asyncio

    middleware = SecurityHeadersMiddleware(downstream)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
