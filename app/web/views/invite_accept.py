"""The acceptance page: /invite/<token>. The second route with no session.

READ app/web/views/share.py BEFORE THIS FILE. It is the first hole in the wall
and it wrote down what has to replace the guard when you cut one: an unguessable
token stored only as a digest, one thing and never a list, an expiry that cannot
be absent, a revocation, and an audit row for the act. Everything below follows
that shape, and the two safety headers it defined are IMPORTED from it rather
than spelt again, because a second copy of a security header is a copy somebody
edits alone.

WHY THIS HOLE HAS TO EXIST. Every other screen is behind AuthMiddleware, which
turns an anonymous request away before any view runs. This is the page that
hands somebody their first credential, so requiring one to reach it is circular.
There is no way to make it not a hole; there is only a way to make it a narrow
one.

WHAT MAKES IT NARROWER THAN THE SHARE PAGE. A share link lives seven days and
shows a claim. This one lives twenty-four hours -- INVITE_PROVISION_TTL_HOURS,
and nothing outside app/state/models.py can lengthen it -- and it sets a
password, which is the most valuable thing in this product to steal: whoever
holds it chooses the credential on an account whose role an admin has already
granted. So it is single use, it dies the instant a resend mints a replacement,
and every refusal is the same sentence.

ONE SENTENCE FOR FOUR CAUSES, AND THE RESPONSE IS BYTE FOR BYTE THE SAME.
Expired, revoked, superseded and never-issued all render ACCEPT_UNAVAILABLE with
a 404 and nothing computed from the request -- no token, no id, no timestamp, no
address. Four responses that differed by a byte would be a probe: a caller
holding a guess could learn which tokens are real, and a caller holding a
withdrawn link could learn it was withdrawn rather than never issued. The real
reason goes into the audit chain, where the company is known and the person
reading it is entitled to it. app/state/invites.py::accept writes that row.

THE PAGE NEVER NAMES THE ADDRESS, ON ANY OF ITS STATES. "Is there an account for
this address" is a question about somebody else, and answering it from a page
anybody can reach turns a leaked link into an address-confirmation oracle. It
DOES name the workspace on the live state, and that is a considered trade: the
token is 32 random bytes, so whoever holds one was sent it, and a person being
asked to choose a password is entitled to know what they are choosing it for. A
page that asked for a credential and refused to say for what is the shape of a
phishing page.

THE PASSWORD RULE IS ON THE PAGE BEFORE ANYBODY TYPES. app/state/identity.py
enforces a length and nothing else, and the page says exactly that rather than
implying a complexity rule nobody checks. Telling somebody the rule after they
fail it is how a person ends up submitting four times.

THE GET DECIDES NOTHING AND WRITES NOTHING. It chooses which of two pages to
render, using invitation_is_live -- the same predicate accept() uses, called
rather than restated, so there is still one definition of a live token. If the
two ever disagree, accept() is the one that is right: it is the only thing here
that changes anything. Writing nothing on the GET also matters because mail
clients and chat previews fetch a link before a person clicks it, and a GET that
consumed the token or filled the audit chain would break on the first Outlook
preview.

THE LIMIT, CONCEDED RATHER THAN LEFT TO BE FOUND. The token is in the URL path,
so any access log in front of this application records it -- the reverse proxy's
log, whatever ships those logs, and the browser's own history. The three headers
above stop it reaching a Referer header, a shared cache or a search index; they
cannot stop an access log. /s/<token> made the same trade and it is the same
trade: a token in a query string is logged identically, and a token in a form
body cannot be put in a link at all, which is what an emailed invitation is. What
narrows it here is the clock -- twenty-four hours, and single use -- so a token
recovered from a log a week later opens nothing. Moving the secret out of the URL
means a two-step flow (an opaque id in the link, a code typed on the page), which
is a real design and is not this one.

WHAT THIS FILE DOES NOT DO. It does not mint invitations, resend them or revoke
them -- those are acts by a signed-in administrator and live on
app/web/views/users_admin.py, against app/state/invites.py. It does not sign the
person in afterwards. Acceptance and authentication are two decisions, and
merging them would mean holding a token was a way to obtain a session as well as
a password.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from app.state.db import session_scope
from app.state.identity import MIN_PASSWORD_LENGTH
from app.state.invites import (
    ACCEPT_OK,
    ACCEPT_PASSWORD,
    ACCEPT_UNAVAILABLE,
    accept,
    hash_invite_token,
    invite_token_matches,
)
from app.state.models import Invitation
from app.state.routing import invitation_is_live
from app.web import TEMPLATES_DIR
from app.web.deps import LOGIN_URL, company_name
from app.web.templating import build_templates

# THE THREE HEADERS, FROM THE ROUTE THAT DEFINED THEM. Referrer-Policy because
# the token is in the path and a browser following the sign-in link would
# otherwise put a working credential link in the Referer header of a request to
# another page. Cache-Control because a dead link must not be answered from a
# shared cache after it dies. X-Robots-Tag because a crawler that reached a
# token would put it in a search index.
from app.web.views.share import SAFETY_HEADERS

router = APIRouter()
templates = build_templates()

TEMPLATE = "invite_accept.html"

# Read by the responsive guard in tests/test_users_admin.py, which holds this
# template to the rules tests/test_responsive.py enforces on the rest. That
# file's scans reach inline style attributes and the stylesheet, and this
# template carries its own rules in the head.
TEMPLATE_DIR = TEMPLATES_DIR
TEMPLATES_OWNED = (TEMPLATE,)

# The prefix, spelt here because this module owns the route.
#
# IT BELONGS IN app/state/invites.py, beside the twin app/state/sharing.py holds
# for /s. app/web/deps.py has to name it to let the guard through, and deps.py
# cannot import from app.web.views without a cycle -- every view imports deps.
# Moving the constant into the state layer is the fix and it is in the handoff
# at the foot of this file; until then deps.py carries the literal and this is
# the only other place it is written.
ACCEPT_PATH_PREFIX = "/invite"

# Caught before anything is written, and NOT a reason code from the write layer:
# accept() has no notion of a second box, because a confirmation field is a
# property of this form rather than of acceptance. Its own sentence for the same
# reason -- ACCEPT_PASSWORD is what the product refused, and this is what the
# person mistyped.
PASSWORDS_DIFFER = (
    "The two passwords are not the same. Nothing was changed and this link "
    "still works."
)

# What the page says about the rule, before anybody types. One sentence, and it
# states the only rule there is: identity.hash_password checks a length and
# nothing else, and implying a complexity rule the product does not enforce
# would be a page describing a system that is not this one.
PASSWORD_RULE = (
    f"At least {MIN_PASSWORD_LENGTH} characters. There is no rule about capitals, "
    "digits or symbols -- length is the only one, so a passphrase of ordinary "
    "words is a good choice here."
)

HEADING_LIVE = "Choose a password"
HEADING_DEAD = "This link is no longer valid"
HEADING_DONE = "Your login is ready"

# The dead page's whole body, and none of it is derived from the request.
DEAD_TEXT = ACCEPT_UNAVAILABLE
DEAD_NEXT = (
    "Ask whoever sent it for a fresh one. A new link is issued rather than this "
    "one extended, so the one in your inbox stops working the moment the new "
    "one is sent."
)


def accept_url(token: str) -> str:
    """The path a recipient opens. One spelling, for the sender and the router."""
    return f"{ACCEPT_PATH_PREFIX}/{token}"


@dataclass(frozen=True, slots=True)
class Peek:
    """What the GET is allowed to know: whether to draw a form, and for whom.

    DECIDES NOTHING. accept() is the only thing that decides whether a token is
    live, and this exists so a person holding a dead link is told so before they
    choose a password rather than after. `workspace` is the company's display
    name and is the only thing on the live page that came out of the database.
    """

    live: bool
    workspace: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip(request: Request) -> str:
    """The address this request came from, as far as this process can tell.

    No forwarded header is trusted, for the reason app/web/views/share.py gives:
    one the client sets is a value the client chose, and an audit row saying an
    acceptance came from wherever the visitor typed is worse than one saying it
    came from the proxy.
    """
    client = request.client
    return client.host if client is not None else ""


def _peek(token: str) -> Peek:
    """Is there a live invitation behind this token, and whose workspace is it.

    A read and only a read: no row is written, no status moves, no audit entry
    is appended. See the module head on why a GET must be free -- mail clients
    fetch links before people click them.

    NO COMPANY ARGUMENT, AND THAT IS NOT AN UNSCOPED READ. The token IS the
    scope, exactly as in accept() and app/auth/sessions.py::resolve_session:
    there is no signed-in person to take a company from, the digest is unique
    across the table, and every fact used afterwards comes off the row the
    digest found.
    """
    if not token or not isinstance(token, str):
        return Peek(False, "")
    try:
        with session_scope() as session:
            invitation = (
                session.query(Invitation)
                .filter(Invitation.token_hash == hash_invite_token(token))
                .one_or_none()
            )
            # Two steps, and the second decides. The query is an index probe on
            # a hash; the comparison is constant time, which is the rule this
            # codebase holds everywhere -- no `==` decides whether a secret is
            # right.
            if invitation is None or not invite_token_matches(
                token, invitation.token_hash
            ):
                return Peek(False, "")
            if not invitation_is_live(invitation, _utcnow()):
                return Peek(False, "")
            if not invitation.invited_user_id:
                # A cross-domain handoff waiting for an administrator has no
                # account behind it yet, so there is nothing for a password to
                # belong to. accept() refuses it and so does this.
                return Peek(False, "")
            return Peek(True, company_name(invitation.company_id))
    except SQLAlchemyError:
        # A database that cannot be read has not told us this link is good, and
        # absence is denial. The dead page is the honest answer and it is the
        # same one a real dead link gets, so nothing is leaked by the failure.
        return Peek(False, "")


def _dead(request: Request) -> HTMLResponse:
    """One response for expired, revoked, superseded and unknown alike.

    Byte for byte the same in all four cases: no token, no id, no timestamp, no
    address, nothing computed from the request. A caller who could tell a
    superseded link from one that never existed would hold a probe for which
    tokens are real, and this page is reachable by anybody.
    """
    return templates.TemplateResponse(
        request,
        TEMPLATE,
        {
            "page_title": HEADING_DEAD,
            "state": "dead",
            "heading": HEADING_DEAD,
            "dead_text": DEAD_TEXT,
            "dead_next": DEAD_NEXT,
        },
        status_code=404,
        headers=SAFETY_HEADERS,
    )


def _form(
    request: Request,
    token: str,
    workspace: str,
    *,
    problem: str = "",
) -> HTMLResponse:
    """The page that asks for a password. `problem` is what to fix, never why not.

    A problem here is always something the person can act on -- a password the
    product refused, two boxes that disagree -- and the link is still good. A
    dead link never reaches this function.
    """
    return templates.TemplateResponse(
        request,
        TEMPLATE,
        {
            "page_title": HEADING_LIVE,
            "state": "form",
            "heading": HEADING_LIVE,
            "workspace": workspace,
            "post_url": accept_url(token),
            "password_rule": PASSWORD_RULE,
            "minimum": MIN_PASSWORD_LENGTH,
            "problem": problem,
        },
        headers=SAFETY_HEADERS,
    )


@router.get(ACCEPT_PATH_PREFIX + "/{token}", response_class=HTMLResponse)
def invitation_page(request: Request, token: str) -> HTMLResponse:
    """The form, or the one sentence a dead link gets. Nothing is written here."""
    seen = _peek(token)
    if not seen.live:
        return _dead(request)
    return _form(request, token, seen.workspace)


@router.post(ACCEPT_PATH_PREFIX + "/{token}", response_class=HTMLResponse)
def set_password(
    request: Request,
    token: str,
    password: str = Form(default=""),
    confirm: str = Form(default=""),
) -> HTMLResponse:
    """Set the password and finish the account. Single use, both kinds.

    THE ACCEPTANCE IS NOT PERFORMED HERE. app/state/invites.py::accept does it,
    through app/state/identity.py::hash_password, which is the one scrypt path
    in this codebase. A second one would be a second set of cost parameters to
    raise, and in practice one of them never gets raised.

    THE CONFIRMATION FIELD IS CHECKED FIRST, and it is the only rule this view
    owns. Two boxes are a property of this form, not of acceptance; accept() has
    no argument for a second one and should not grow one. Checking it before the
    call also means a mistyped confirmation costs nobody a scrypt hash.

    A REAL ATTEMPT GOES STRAIGHT TO accept(), WITHOUT PEEKING FIRST, AND THAT IS
    NOT AN OVERSIGHT. accept() records a refused acceptance in the audit chain --
    "Priya clicked the withdrawn link on Tuesday" is exactly what an
    administrator needs, and app/state/invites.py is explicit that the reason
    goes in the chain even though it never reaches the caller. An earlier version
    of this function checked the token itself first and returned the dead page
    before accept() ran, which silently dropped that row. The peek belongs on the
    GET, where nothing is being attempted; on the POST somebody is trying a
    credential and the attempt is a fact worth keeping.

    The workspace name for the two pages below therefore comes off the outcome
    rather than out of a second read. On a refusal it is None, and the dead page
    renders nothing derived from the request anyway.

    NO REDIRECT ON SUCCESS. A 303 would put the token in the browser's history
    and in the next request's Referer, which is the whole reason the two headers
    above exist. The page is rendered, and a reload re-posts a token that is now
    accepted, which lands on the same dead page as any other spent link.
    """
    if password != confirm:
        # Nothing has been attempted against the credential path, so nothing is
        # recorded. The peek here only decides whether there is a form to send
        # them back to -- a dead link is dead whatever was typed into it.
        seen = _peek(token)
        if not seen.live:
            return _dead(request)
        return _form(request, token, seen.workspace, problem=PASSWORDS_DIFFER)

    with session_scope() as session:
        outcome = accept(
            session,
            token=token,
            password=password,
            now=_utcnow(),
            ip=_client_ip(request),
        )
        code, text = outcome.reason_code, outcome.reason_text
        workspace = company_name(outcome.company_id) if outcome.company_id else ""

    if code == ACCEPT_PASSWORD:
        # A DIFFERENT ANSWER FROM A DEAD LINK, on purpose. The invitation is
        # fine and the person is entitled to be told what to fix; saying "this
        # invitation is not available" for a short password would send somebody
        # back to the sender for a link that works.
        return _form(request, token, workspace, problem=text)
    if code != ACCEPT_OK:
        # ACCEPT_REFUSED, and anything a future version of that module adds.
        # An unrecognised code is not evidence that something worked.
        return _dead(request)

    return templates.TemplateResponse(
        request,
        TEMPLATE,
        {
            "page_title": HEADING_DONE,
            "state": "done",
            "heading": HEADING_DONE,
            "workspace": workspace,
            # NOT the address. See the module head: this page never names it,
            # and the person knows which mailbox the link arrived in.
            "done_text": (
                "Your password is set. Sign in with the address this link was "
                "sent to."
            ),
            "login_url": LOGIN_URL,
        },
        headers=SAFETY_HEADERS,
    )


# ---------------------------------------------------------------------------
# HANDOFF -- what this module needs from files it does not own
#
# app/web/deps.py       REQUIRED, and the feature does not exist without it.
#                       PUBLIC_PATHS holds exact paths, so a token URL cannot be
#                       listed there and the guard sends every acceptance link to
#                       /login. The same two lines /s/<token> is already waiting
#                       for, written once for both:
#
#                         PUBLIC_PREFIXES = ("/s", "/invite")
#
#                         def is_public_path(path: str) -> bool:
#                             return (
#                                 path in PUBLIC_PATHS
#                                 or path.startswith(STATIC_URL_PATH + "/")
#                                 or any(
#                                     path.startswith(prefix + "/")
#                                     for prefix in PUBLIC_PREFIXES
#                                 )
#                             )
#
#                       Prefix matching is what makes /login-as-somebody-else
#                       public by accident, which is why the existing function
#                       refuses it -- so the prefixes are a SEPARATE tuple with
#                       two members and a trailing slash on every comparison,
#                       and nothing is added to it without an argument like the
#                       one at the head of this file.
#
#                       The literals go there because deps.py cannot import from
#                       app.web.views: every view imports deps, and the cycle
#                       would be immediate. The real fix is one line further --
#                       ACCEPT_PATH_PREFIX belongs in app/state/invites.py beside
#                       SHARE_PATH_PREFIX in app/state/sharing.py, which deps.py
#                       may import from freely. Then both spellings here and
#                       there are deleted.
#
# app/main.py           `invite_accept` added to the import from app.web.views
#                       and `app.include_router(invite_accept.router)` beside
#                       share.router, which is where the other unauthenticated
#                       route is mounted. tests/test_app_wiring.py derives its
#                       answer from the views package, so a router nobody
#                       mounted fails there rather than 404ing in front of a
#                       reviewer.
#
# app/web/templates/base.html
#                       Nothing. This page deliberately does not extend it: a
#                       shell rendering six screens' worth of links on a page
#                       nobody had to sign in to reach is an access-control
#                       failure wearing a stylesheet, and every one of those
#                       links would bounce to /login anyway.
#
# app/state/invites.py  Nothing required. ACCEPT_PATH_PREFIX and accept_url()
#                       belong there when somebody moves them; this module is
#                       their only caller today.
# ---------------------------------------------------------------------------


__all__ = [
    "ACCEPT_PATH_PREFIX",
    "ACCEPT_UNAVAILABLE",
    "DEAD_NEXT",
    "DEAD_TEXT",
    "HEADING_DEAD",
    "HEADING_DONE",
    "HEADING_LIVE",
    "PASSWORDS_DIFFER",
    "PASSWORD_RULE",
    "SAFETY_HEADERS",
    "TEMPLATE",
    "TEMPLATES_OWNED",
    "TEMPLATE_DIR",
    "Peek",
    "accept_url",
    "invitation_page",
    "router",
    "set_password",
    "templates",
]
