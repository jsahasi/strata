"""Who the request is for. One person, one tenant, answered in one place.

This module used to say there was no login, and derive the tenant from the
environment. There is a login now (app/auth/sessions.py), so the answer comes
from the person holding the request: current_user() resolves the session cookie
and current_company() returns that person's company. The seam that was left
here for authentication to replace has been replaced, and the shape it was left
in -- a function, overridable in a test, never a module constant -- is what made
that a small change rather than a rewrite of every view.

Three things live here and nothing else does.

**The principal.** current_user() turns the cookie into a Principal: a frozen
record of who is signed in, which company they belong to, and which session
they are holding. It is a plain dataclass rather than the ORM User row, because
the row belongs to a database session that is closed by the time a template
renders it, and a detached row that lazily loads an attribute at render time
fails in the one place nobody is watching.

**The tenant.** current_company() reads the signed-in person's company. Every
existing call site keeps working unchanged -- it is still a function of no
arguments -- because the principal travels in a ContextVar that AuthMiddleware
sets for the life of the request. Views that resolve the tenant through
Depends(current_company) and views that call current_company() directly
therefore get the same answer, which is the property this module exists for:
two answers to "whose data is this" is how a tenant leak happens.

**The guard.** AuthMiddleware refuses an anonymous request to anything except
the login page, the health check and the stylesheet, and sends it to /login.
Refusing at the middleware rather than per route is deliberate: a route added
next week is protected because it exists, not because somebody remembered a
decorator. Absence is denial, applied to the session.

THE FALLBACK, ANNOUNCED. With nobody signed in, current_company() falls back to
STRATA_COMPANY_ID and then to the demo tenant. That path is unreachable through
the running application -- the middleware sends an anonymous request to /login
before any view runs -- and it exists for the callers that are not requests at
all: the seed, scripts, and the screen tests that mount one router on their own.
If you find that fallback answering inside a request, the middleware is not
installed, and the fix is to install it rather than to widen this function.
"""

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from app.auth.sessions import SESSION_TTL, resolve_session
from app.seed import DATA_DIR
from app.state.db import session_scope
from app.state.sharing import SHARE_PATH_PREFIX
from app.web import STATIC_URL_PATH

# The one tenant in data/company_context.json. Everything in the corpus is
# scoped to it, and it is what an unconfigured deployment gets.
DEMO_COMPANY_ID = "MEP"

# The tenant, and the display name beside it in the masthead. Both still read
# from the environment, and both now only matter where nobody is signed in.
COMPANY_ENV = "STRATA_COMPANY_ID"
COMPANY_NAME_ENV = "STRATA_COMPANY_NAME"

# ---------------------------------------------------------------------------
# The cookie
# ---------------------------------------------------------------------------

SESSION_COOKIE = "strata_session"

# HttpOnly: script cannot read it, so one cross-site scripting bug does not
# hand over every session as well as the page.
# SameSite=Lax: the browser does not attach it to a cross-site POST, which is
# the whole of the CSRF defence on this build -- there is no token.
# Secure: it never travels over plain http. Browsers treat localhost as
# trustworthy, so `make run` still works.
COOKIE_HTTPONLY = True
COOKIE_SAMESITE = "lax"
COOKIE_PATH = "/"

# Turning Secure off is a real downgrade and must be deliberate. It exists for
# a deployment behind a proxy that terminates TLS on another host, and it
# announces itself: cookie_is_secure() is read by the login view, which says on
# the page when the cookie is being sent in the clear. Set explicitly, it wins
# in both directions -- including over the loopback rule below.
COOKIE_SECURE_ENV = "STRATA_COOKIE_SECURE"

# The one place Secure comes off by itself: a plain-http request to the local
# machine, which is what `make run` serves. Chrome and Firefox treat localhost
# as trustworthy and keep a Secure cookie there; Safari does not, and on Safari
# the flag would make the cookie undeliverable -- the browser would drop it, the
# guard would send the person back to /login, and the product would look broken
# to a reviewer running exactly the command the README gives them. So on
# loopback http the flag comes off and the login page says it has.
#
# This cannot weaken a real deployment: it needs the request to be plain http
# AND addressed to this machine. Anything reachable by anyone else fails both.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# The cookie dies with the session it names. One number, from one place.
COOKIE_MAX_AGE = int(SESSION_TTL.total_seconds())

LOGIN_URL = "/login"
LOGOUT_URL = "/logout"
HEALTH_URL = "/healthz"

#: Where the "Sign in as" buttons on the login page post. It authenticates
#: through the same login() as the form beside it, with the seeded demo password
#: supplied by the server rather than printed on the page for somebody to type
#: -- see app/web/views/auth.py::demo_login_post. It is public for the same
#: reason /login is: an anonymous caller is exactly who uses it.
DEMO_LOGIN_URL = "/login/demo"

# Reachable with no session. Everything else redirects.
#
# /login, /login/demo and /healthz only, plus the stylesheet -- an unstyled
# login page is a broken login page. /logout is NOT here: signing out is
# something a signed-in person does, and an anonymous POST to it has nothing to
# end.
#
# These are matched WHOLE, never by prefix, which is why /login/demo has to be
# listed rather than inherited from /login. A prefix rule here would have made
# every path under /login public, and the next thing anybody mounts there gets
# that for free without noticing -- the same defect PUBLIC_PREFIXES documents
# below, arrived at from the other direction.
PUBLIC_PATHS = (LOGIN_URL, DEMO_LOGIN_URL, HEALTH_URL)

#: Prefixes an anonymous caller may reach, each stored WITH its trailing slash.
#: The slash is the control, not decoration: startswith("/s") would make every
#: root path beginning with the letter s public, and a guard list that widens
#: itself by accident is worse than none.
#:
#: The share prefix is here because a share link is meant to work for somebody
#: who has no account -- that is the whole feature. Leaving it out did not make
#: it private, it made it leak: the guard answered 303 to
#: /login?next=%2Fs%2F<token> and put a live bearer token into a query string,
#: where it reaches the access log, the referrer header and browser history.
#: A redirect that carries the credential it was protecting is worse than no
#: guard at all, because it looks like a guard working.
PUBLIC_PREFIXES = (STATIC_URL_PATH + "/", SHARE_PATH_PREFIX + "/")

# The query parameter carrying where the person was going before they were sent
# to sign in.
NEXT_PARAM = "next"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is holding this request. Read-only, and detached from the database.

    Carries the four facts a view needs -- who, which tenant, which session,
    what to print in the masthead -- and nothing else. Permissions are not on
    it on purpose: they are a question for app/auth/policy.py against the live
    grant rows, and a copy cached here would still say "may approve" after the
    role was revoked mid-session.
    """

    user_id: str
    company_id: str
    email: str
    display_name: str
    session_id: str


# The signed-in person, for the life of one request. A ContextVar rather than a
# module global because a global would be shared by every request the process
# is serving at once, and rather than request.state because the call sites that
# predate login -- app/web/views/proceedings.py::current_company_id() and the
# review screens -- have no request in hand.
_principal: ContextVar[Principal | None] = ContextVar("strata_principal", default=None)


def signed_in() -> Principal | None:
    """The principal this request is running under, or None outside a request."""
    return _principal.get()


def principal_for_token(token: str | None) -> Principal | None:
    """Resolve a session token to a principal. Every failure is the same None.

    Opens its own database session, because it is called from the middleware
    before any view has one. resolve_session may write -- it closes a session
    that has just expired and moves last_seen_at -- so this runs inside
    session_scope, which commits.

    A database that cannot be read answers None as well, which reads as "not
    signed in" and sends the caller to the login page. That is a fallback and
    it announces itself there rather than here: the login page checks the same
    tables and says plainly when they are missing, with the command that fixes
    it. The alternative -- a 500 on every request -- tells a reviewer who has
    cloned the repository and not yet seeded nothing they can act on.
    """
    if not token:
        return None

    try:
        with session_scope() as session:
            resolved = resolve_session(session, token)
            if resolved is None:
                return None
            user, live = resolved
            return Principal(
                user_id=user.id,
                company_id=user.company_id,
                email=user.email,
                display_name=user.display_name,
                session_id=live.id,
            )
    except SQLAlchemyError:
        return None


def current_user(request: Request) -> Principal | None:
    """The signed-in person, or None. Usable as a dependency or called directly.

    Resolves the cookie itself when the middleware has not already done it, so
    a router mounted on its own in a test behaves the same way as the assembled
    application. The answer is cached on request.state for the life of the
    request: a page that asks twice must not run two database reads and must
    not be able to get two answers.
    """
    cached = getattr(request.state, "strata_principal", False)
    if cached is not False:
        return cached

    principal = signed_in()
    if principal is None:
        principal = principal_for_token(request.cookies.get(SESSION_COOKIE))
    request.state.strata_principal = principal
    return principal


def current_company() -> str:
    """The company whose rows this request may read.

    The signed-in person's company first. The environment, then the demo
    tenant, only where nobody is signed in -- see the note at the top of this
    module about why that path is not reachable through the application.
    """
    principal = signed_in()
    if principal is not None:
        return principal.company_id
    return os.environ.get(COMPANY_ENV, "").strip() or DEMO_COMPANY_ID


def is_loopback_plaintext(request: Request | None) -> bool:
    """True for a plain-http request addressed to this machine. See LOOPBACK_HOSTS."""
    if request is None:
        return False
    url = request.url
    if url.scheme != "http":
        return False
    return (url.hostname or "").lower() in LOOPBACK_HOSTS


def cookie_is_secure(request: Request | None = None) -> bool:
    """Whether this response marks the session cookie Secure.

    The variable wins when it is set, in either direction -- a deployment that
    has thought about this is not second-guessed. Unset, the answer is yes
    everywhere except plain http to loopback, where the flag would stop the
    cookie reaching the browser at all.

    Passing no request answers the question in general rather than for one
    response, which is what a caller outside a request can ask.
    """
    setting = os.environ.get(COOKIE_SECURE_ENV, "").strip().lower()
    if setting:
        return setting not in ("0", "false", "no", "off")
    return not is_loopback_plaintext(request)


def cookie_settings(request: Request | None = None) -> dict:
    """The keyword arguments for response.set_cookie. One definition.

    The login view and any future view that reissues a session read these from
    here. Two copies of a cookie policy is how one of them ends up without
    HttpOnly -- and how a cookie gets deleted with different attributes from the
    ones it was set with, which some browsers read as a different cookie and
    leave in place.
    """
    return {
        "key": SESSION_COOKIE,
        "httponly": COOKIE_HTTPONLY,
        "samesite": COOKIE_SAMESITE,
        "secure": cookie_is_secure(request),
        "path": COOKIE_PATH,
        "max_age": COOKIE_MAX_AGE,
    }


def is_public_path(path: str) -> bool:
    """True for the paths an anonymous caller may reach.

    Exact matches, plus the prefixes in PUBLIC_PREFIXES. Prefix matching on the
    rest would make /login-as-somebody-else public by accident, which is the
    shape of mistake a guard list is supposed to prevent rather than introduce
    -- which is why every prefix carries its trailing slash.
    """
    return path in PUBLIC_PATHS or any(
        path.startswith(prefix) for prefix in PUBLIC_PREFIXES
    )


def safe_next(target: str | None) -> str | None:
    """Keep a redirect target only when it points back at this site.

    A path beginning with a single slash and no scheme. Anything else -- an
    absolute URL, a protocol-relative //evil.example, a backslash a browser
    reads as a slash -- is dropped rather than corrected, because a login page
    that forwards a person to wherever the query string says is an open
    redirect wearing a helpful face.
    """
    if not target or not isinstance(target, str):
        return None
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return None
    if "\\" in target or "\n" in target or "\r" in target:
        return None
    return target


def login_redirect(path: str, query: str = "", *, remember: bool = True) -> str:
    """Where an anonymous request is sent, carrying where it was going."""
    if not remember:
        return LOGIN_URL
    target = path + (f"?{query}" if query else "")
    if safe_next(target) is None or target == LOGIN_URL:
        return LOGIN_URL
    return f"{LOGIN_URL}?{NEXT_PARAM}={quote(target, safe='')}"


class AuthMiddleware:
    """Resolve the cookie once per request, and refuse the anonymous ones.

    Written as raw ASGI rather than as a BaseHTTPMiddleware subclass for one
    reason: the downstream application then runs inside this call, in this
    context, so the ContextVar set below is visible to the view -- including a
    synchronous view, which Starlette runs in a worker thread with a copy of
    this context. BaseHTTPMiddleware runs the app in a task of its own, and
    which context variables survive that is an implementation detail of a
    library rather than something this product should depend on.
    tests/test_login.py signs in as one company with the environment naming
    another, and reads a screen, which is the assertion that propagation works.

    The database read happens in a worker thread. It is a synchronous SQLite
    query, and running it on the event loop would stall every other request
    for its duration.

    What this does NOT do. It does not check permissions -- that is
    app/auth/policy.py, and a signed-in person with no roles still reaches
    every screen this middleware lets through. It does not rate-limit. And it
    trusts the cookie the browser sent, which is what a session cookie is.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # The stylesheet is not tenant data and does not need to know who is
        # asking. Resolving the cookie for it would put a database read behind
        # every asset on every page, and the browser sends the cookie with all
        # of them.
        if path.startswith(STATIC_URL_PATH + "/"):
            await self.app(scope, receive, send)
            return

        token = _cookie_from_scope(scope, SESSION_COOKIE)
        # No cookie, no read. Its absence has already answered the question.
        principal = (
            await run_in_threadpool(principal_for_token, token) if token else None
        )
        reset = _principal.set(principal)
        try:
            if principal is None and not is_public_path(path):
                query = scope.get("query_string", b"").decode("latin-1")
                # 303, so a POST that hits the wall arrives at the login page
                # as a GET. Replaying the POST after signing in would repost a
                # form the person cannot see.
                remember = scope.get("method", "GET") in ("GET", "HEAD")
                response = RedirectResponse(
                    url=login_redirect(path, query, remember=remember),
                    status_code=303,
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            _principal.reset(reset)


def _cookie_from_scope(scope, name: str) -> str | None:
    """Read one cookie out of the raw ASGI headers.

    Starlette's Request would parse them for us, and building a Request here
    would mean building one per request in the middleware and another in the
    view. The parsing is small enough to do once.
    """
    for key, value in scope.get("headers", ()):
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            head, _, tail = part.partition("=")
            if head.strip() == name:
                return tail.strip()
    return None


def install_auth(app) -> None:
    """Put the guard in front of an application. Called by app/main.py.

    A function rather than a line in main.py so that the tests which assemble
    their own application can install exactly what the product installs. A
    guard that the product has and the test does not is a guard nobody has
    tested.
    """
    app.add_middleware(AuthMiddleware)


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    """Company id to display name, read once from the corpus.

    There is no companies table -- the corpus is one tenant and the name lives
    in data/company_context.json, which app/seed.py already reads. Copying the
    string into this module would give one fact two homes and let them drift.

    A missing or unreadable file returns an empty map rather than raising. The
    consequence is a header showing the tenant id instead of the name, which is
    a smaller failure than a 500 on every screen, and it is still true.
    """
    try:
        context = json.loads(
            (DATA_DIR / "company_context.json").read_bytes().decode("utf-8")
        )
    except (OSError, ValueError):
        return {}

    company = context.get("company", {})
    short_name, name = company.get("short_name"), company.get("name")
    return {short_name: name} if short_name and name else {}


def company_name(company_id: str) -> str:
    """The company's name, or its id where nothing here knows the name.

    Never a guess and never a blank. An unknown tenant shows as its id, which is
    honest and still identifies the scope every read on the page ran under.
    """
    return _names().get(company_id, company_id)


__all__ = [
    "AuthMiddleware",
    "COMPANY_ENV",
    "COMPANY_NAME_ENV",
    "COOKIE_MAX_AGE",
    "COOKIE_SECURE_ENV",
    "DEMO_COMPANY_ID",
    "HEALTH_URL",
    "LOGIN_URL",
    "LOGOUT_URL",
    "NEXT_PARAM",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "Principal",
    "SESSION_COOKIE",
    "company_name",
    "cookie_is_secure",
    "cookie_settings",
    "current_company",
    "current_user",
    "install_auth",
    "is_loopback_plaintext",
    "is_public_path",
    "login_redirect",
    "principal_for_token",
    "safe_next",
    "signed_in",
]
