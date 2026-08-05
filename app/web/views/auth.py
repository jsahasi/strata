"""The login screen, and the two writes behind it.

Three routes: show the form, take the form, end the session. Everything they
decide is decided in app/auth/sessions.py; this module turns a refusal into a
sentence and a success into a cookie.

**One sentence for every refusal.** The page says "email or password is
incorrect" whether the address is unknown, the password is wrong, the account is
suspended or the account is locked. The view cannot say more even if somebody
wanted it to: AuthFailed carries the generic message and no reason code, so
there is nothing here to accidentally render.

**The refusal is committed, not rolled back.** login() writes the failed attempt
into the audit chain and increments the counter, then raises. session_scope()
rolls back on any exception that leaves it, so catching AuthFailed OUTSIDE the
`with` block would discard both -- the throttle would count to five and reset,
for ever, and the log would show no failed logins at all. The except sits inside
the block for that reason. tests/test_login.py proves it through HTTP, which is
the only place the mistake would have shown.

**The tenant a login authenticates against.** A user row is unique per company
and email, so signing in needs to know which company before it knows who. It
uses the tenant the deployment is configured for -- current_company() with
nobody signed in, which is STRATA_COMPANY_ID or the demo company. One tenant per
deployment is a real limit and it is stated in docs; a hosted multi-tenant build
would take the company from the address's domain or from a chooser on this page.

**What is not built.** There is no CSRF token on this form. SameSite=Lax stops a
cross-site POST from carrying the session cookie, which covers sign-out; it does
not stop an attacker signing a victim's browser into an account the attacker
controls. There is no password reset, no invitation flow and no second factor.
"""

import os
from dataclasses import dataclass

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from app.auth.sessions import LOGIN_REFUSED, AuthFailed, login, logout
from app.seed import DEMO_PASSWORD, demo_account_list
from app.state.db import session_scope
from app.state.identity import users_for_company
from app.web.deps import (
    LOGIN_URL,
    LOGOUT_URL,
    NEXT_PARAM,
    SESSION_COOKIE,
    company_name,
    cookie_is_secure,
    cookie_settings,
    current_company,
    current_user,
    is_loopback_plaintext,
    safe_next,
)
from app.web.templating import build_templates

router = APIRouter()
templates = build_templates()

# Where a person lands with nothing else asked for.
# Where a person lands after signing in. NOT "/": in the deployed setup the
# marketing site owns the root and the application sits behind it, so a redirect
# to "/" would sign somebody in and then show them the sales page. /projects is
# the same screen the app serves at "/" anyway -- the project board took the
# landing path when the router collision was resolved (ADR-23).
HOME_URL = "/projects"

# Shown when the database has no users at all. A login form with no accounts
# behind it is a wall, and telling somebody which command fixes it is the same
# courtesy the empty screens already extend (STATE_NO_ROWS in proceedings.py).
NO_ACCOUNTS = (
    "This database has no accounts yet. Run `make seed`, or "
    "`python -m app.seed`, to create the demo accounts."
)

SIGNED_OUT = "You are signed out."

# Turning the demo panel off is one variable, and the panel says which.
DEMO_ACCOUNTS_ENV = "STRATA_DEMO_ACCOUNTS"

# The spellings of "off" this build accepts. Anything else leaves the panel on,
# so a typo does not quietly turn a stated setting into the opposite one.
_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True, slots=True)
class AccountHint:
    """One line of the demo panel: an address, what it may do, and who it is."""

    email: str
    role: str
    display_name: str
    title: str


def _addresses(company_id: str) -> set[str] | None:
    """Every address this tenant can sign in with. None when nobody can look.

    One read for the two things the page needs to know -- whether there is
    anybody at all, and which of the demo accounts really exist. None means the
    tables are not there, which is a different fact from "no users" and gets a
    different sentence.
    """
    try:
        with session_scope() as session:
            return {user.email for user in users_for_company(session, company_id)}
    except SQLAlchemyError:
        return None


def _demo_hints(company_id: str, present: set[str] | None) -> tuple[AccountHint, ...]:
    """The seeded accounts that actually exist in this database. Possibly none.

    Reports what is there rather than what the corpus describes. A deployment
    that seeded its own people shows nothing here, and so does one where the
    seed has not run -- which is the difference between this panel and a list
    of addresses printed from a data file whether or not they work.

    Off entirely when STRATA_DEMO_ACCOUNTS is 0. Publishing working credentials
    on a login page is correct for a demo of a synthetic corpus and wrong
    everywhere else.
    """
    if not present:
        return ()
    if os.environ.get(DEMO_ACCOUNTS_ENV, "").strip().lower() in _OFF:
        return ()

    try:
        expected = demo_account_list()
    except (OSError, ValueError, KeyError):
        return ()

    return tuple(
        AccountHint(
            email=account.email,
            role=account.role,
            display_name=account.display_name,
            title=account.title,
        )
        for account in expected
        if account.email in present
    )


def _page(
    request: Request,
    company_id: str,
    *,
    email: str = "",
    error: str | None = None,
    notice: str | None = None,
    next_url: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the form. The password is never put back into it.

    The address is returned so a person who mistyped their password does not
    retype their address. The password is not, because a page that echoes a
    password puts it in the browser's back cache, in any proxy log that keeps
    response bodies, and on the screen.
    """
    present = _addresses(company_id)
    context = {
        "page_title": "Sign in",
        "company_id": company_id,
        "company_name": company_name(company_id),
        "email": email,
        "error": error,
        "notice": notice,
        "next_url": next_url or "",
        "next_param": NEXT_PARAM,
        "post_url": LOGIN_URL,
        "demo_password": DEMO_PASSWORD,
        "demo_accounts": _demo_hints(company_id, present),
        "demo_env": DEMO_ACCOUNTS_ENV,
        "cookie_secure": cookie_is_secure(request),
        "cookie_local": is_loopback_plaintext(request),
        "no_accounts": None if present else NO_ACCOUNTS,
    }
    return templates.TemplateResponse(
        request, "login.html", context, status_code=status_code
    )


@router.get(LOGIN_URL, response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    """The form. Somebody already signed in is sent on rather than shown it."""
    principal = current_user(request)
    target = safe_next(request.query_params.get(NEXT_PARAM)) or HOME_URL
    if principal is not None:
        return RedirectResponse(url=target, status_code=303)

    notice = SIGNED_OUT if request.query_params.get("signed_out") else None
    return _page(
        request,
        current_company(),
        next_url=safe_next(request.query_params.get(NEXT_PARAM)),
        notice=notice,
    )


@router.post(LOGIN_URL)
def login_post(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next_url: str = Form(default="", alias="next"),
):
    """Sign in, or say the one sentence and give the form back.

    The whole attempt happens inside one session_scope, including the except.
    See the module docstring: catching outside it would roll back the audit row
    and the failed-attempt counter that login() had just written.

    The address recorded is the socket the request arrived on, never
    X-Forwarded-For. A header the caller writes is not evidence, and putting one
    into an append-only log is putting the attacker's own words in the record.
    A deployment behind a proxy fixes this at the server (`uvicorn
    --proxy-headers`, with the proxy trusted), which is where the decision about
    what to trust belongs.
    """
    company_id = current_company()
    target = safe_next(next_url) or HOME_URL
    client = request.client
    ip = client.host if client else ""
    agent = request.headers.get("user-agent", "")

    token = ""
    failed = False
    try:
        with session_scope() as session:
            try:
                _, token = login(
                    session,
                    company_id,
                    email=email,
                    password=password,
                    ip=ip,
                    user_agent=agent,
                )
            except AuthFailed:
                # Inside the block on purpose. The transaction commits the
                # failure the log needs; the flag tells the code after it what
                # to render.
                failed = True
    except SQLAlchemyError:
        # No users table, or the database is not there. Not a wrong password,
        # and saying so would send somebody hunting for a password that was
        # never the problem.
        return _page(
            request,
            company_id,
            email=email,
            error=NO_ACCOUNTS,
            next_url=safe_next(next_url),
            status_code=503,
        )

    if failed:
        return _page(
            request,
            company_id,
            email=email,
            error=LOGIN_REFUSED,
            next_url=safe_next(next_url),
            status_code=401,
        )

    # 303, so a reload of the destination does not repost the credentials.
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(value=token, **cookie_settings(request))
    return response


@router.post(LOGOUT_URL)
def logout_post(request: Request):
    """End the session, drop the cookie, and say so on the way out.

    The cookie is deleted whatever happens to the row. A session the database
    could not revoke is still a session this browser must stop presenting, and
    leaving the cookie in place because a write failed would leave somebody
    looking signed out and being signed in.
    """
    principal = current_user(request)
    if principal is not None:
        try:
            with session_scope() as session:
                logout(
                    session,
                    principal.session_id,
                    f"person:{principal.email}",
                    company_id=principal.company_id,
                )
        except SQLAlchemyError:
            pass

    # Deleted with the attributes it was set with. A cookie cleared with a
    # different path or a different Secure flag is a different cookie to some
    # browsers, and the old one stays where it is.
    settings = cookie_settings(request)
    response = RedirectResponse(url=f"{LOGIN_URL}?signed_out=1", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path=settings["path"],
        httponly=settings["httponly"],
        samesite=settings["samesite"],
        secure=settings["secure"],
    )
    return response


__all__ = ["AccountHint", "HOME_URL", "NO_ACCOUNTS", "SIGNED_OUT", "router"]
