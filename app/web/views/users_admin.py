"""Logins: who has one, when they last used it, and how to add another.

WHO THIS SCREEN IS FOR AND WHAT THEY ARE LOOKING FOR. An administrator opens it
to answer two questions and almost never a third: **which accounts are dormant
and should be suspended**, and **which invitations were never taken up**. Both
are accounts nobody has signed in as. Rendered as an empty cell they are the
same pixel, and the whole reason users.last_login_at is nullable -- "Never
logged in and logged in a year ago are different facts about an account", from
app/state/models.py -- is thrown away at the last step by a template that prints
a dash. So this screen renders four different things where a lazier one renders
one blank:

    a date          they have signed in; the relative time is what you scan and
                    the exact stamp beside it is what you act on
    NEVER_SIGNED_IN + NEVER_USED_ACTIVE    nothing is stopping them and they
                    have not; the sharpest suspension candidate on the page
    NEVER_SIGNED_IN + INVITE_STILL_OPEN    nothing is wrong yet, the link works,
                    and the expiry says how long you can wait
    NEVER_SIGNED_IN + INVITE_LAPSED        the link is dead, nobody can get in
                    with it, and Resend is the fix -- the row is flagged

app/state/invites.py::AccountRow carries last_login_at as None and says at
length that nothing on the way here may coerce it. This module is the "here".

THE HARD PART OF THE FEATURE IS THE RESEND, AND IT IS NOT A UX DETAIL. "After
that they need a fresh invite" means an expired invitation is NEVER extended.
resend() mints a new token and moves every live row for that address to
INVITE_SUPERSEDED, so at most one token is ever valid for one person, and the
old one dies at that instant rather than at its own expiry. Pushing expires_at
forward would leave every copy of the first link -- in the recipient's inbox, in
the helpdesk ticket where they pasted it, in whatever archived the mail on the
way past -- working for another day, every time anybody pressed the button. This
screen presses that button and does not have a second way to do it.

THE TOKEN IS RENDERED ONCE AND NEVER TRAVELS IN A URL. provision_login and
resend return the raw token to their caller and nowhere else -- it is not
stored, and app/state/invites.py is explicit that it is never written to the
audit chain either. So the two write paths here RENDER their result instead of
redirecting to it: a 303 carrying the token would file a live credential link in
the browser's history, in the proxy log and in the next request's Referer
header. The cost is that a reload re-posts the form, and that cost is paid
openly: a second provision of the same address answers INV_ALREADY_A_USER and a
second resend supersedes the link it just made, both of which are refusals or
repeats rather than damage.

THE MAIL IS SENT, AND WHETHER IT WENT IS ALWAYS ON THE PAGE. app/state/invites.py
sends none -- it hands the token back and says so -- so this screen is the caller
that delivers, through app/notify. That module returns a Delivery carrying an
announcement on EVERY outcome including success, and this screen prints that
sentence beside the link without rewriting it. Three of those outcomes are
ordinary: no relay configured (which is every reviewer's laptop), a relay that
refused, and a mail that went. All three leave a valid link on screen, because
the administrator's fallback is to send it themselves and they can only do that
if they can see it. The one thing they must never have to guess is whether the
person on the other end was told, and that is why the sentence is unconditional
rather than shown only on failure.

AND WHEN THE LINK WOULD NOT OPEN, THE SCREEN SAYS THAT TOO. The acceptance route
is only reachable without a session once app/web/deps.py lets its prefix past
the guard, and that file is not this branch's to change. So this screen ASKS
deps.is_public_path whether the link it is handing over would work, and prints
GUARD_CLOSED when the answer is no. A fallback must announce itself, and the
silent version of this -- handing an administrator a link that sends the
recipient to a sign-in page they have no account for -- is a support ticket
nobody can diagnose from either end.

THE ROLE PICKER OFFERS WHAT WILL WORK, NOT WHAT EXISTS. grantable_roles()
answers with the roles this administrator may hand on, and the ceiling behind it
is real and narrow: an inviter may never grant more than they hold, and the
admin role deliberately holds no approval permission, so an ordinary
administrator can provision another administrator and nothing else. That is a
short list and the screen says why rather than quietly hiding two options. A
forged form naming a role that is not on the list is still refused, by
provision_login and not by this file: the picker is a courtesy, the ceiling is
the control.

THERE IS NO CSRF TOKEN ON THESE FORMS. SameSite=Lax on the session cookie is the
whole of the defence, exactly as app/web/views/auth.py concedes for the login
form and app/web/views/shares_admin.py for the revoke button. It stops a
cross-site POST in every browser this product supports, and it is not a token.

WHY THE INVITE LAYER IS IMPORTED AT THE TOP AND NOT REACHED FOR LAZILY. The
share register does the opposite, and it is right to: it is a screen about share
links that happens to carry an invitation queue, so it can lose the queue and
still be a screen. This one is invitations end to end -- the account list itself
comes from accounts_for_company(). With that module unreachable there is no
page, and a fallback rendering an empty account list would be this screen saying
"nobody works here", which is a different statement and a false one.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# THE MODULE, NOT THE NAMES. app/web/views/admin.py and shares_admin.py both
# give the reasoning at length: app/auth/policy.py reads its approval mode at
# import and the tests reload it, so an `except` bound to a class captured at
# import time stops catching the refusal the reloaded module raises, and a 403
# becomes a 500.
from app.auth import policy
from app.state.db import session_scope
from app.state.invites import (
    INV_ALREADY_ACCEPTED,
    INV_ALREADY_A_USER,
    INV_ALREADY_LIVE,
    INV_DISABLED,
    INV_EMAIL_MALFORMED,
    INV_GRANT_EXCEEDS,
    INV_NOT_PERMITTED,
    INV_NOT_PROVISION,
    INV_NO_EMAIL,
    INV_NO_INVITER,
    INV_NO_NAME,
    INV_NO_ROLE,
    INV_OK,
    INV_UNKNOWN,
    INV_WAS_REVOKED,
    AccountRow,
    accounts_for_company,
    grantable_roles,
    provision_login,
    resend,
)
from app.state.models import (
    INVITE_KIND_PROVISION,
    INVITE_PROVISION_TTL_HOURS,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
)
from app.web import TEMPLATES_DIR, deps
from app.web.deps import current_user

# The acceptance route, for the link this screen hands over and for the guard
# question it asks about that link. One spelling of the path, in the module that
# owns it.
from app.web.views.invite_accept import ACCEPT_PATH_PREFIX, accept_url

# The page shell and the tenant, decided in one place for every screen.
from app.web.views.proceedings import chrome, current_company_id, unresolved_escalations

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

TEMPLATE = "users_admin.html"

# Read by the responsive guard in tests/test_users_admin.py. That file's scans
# reach inline style attributes and the stylesheet; this template carries its
# own rules in the head, which neither of them sees.
TEMPLATE_DIR = TEMPLATES_DIR
TEMPLATES_OWNED = (TEMPLATE,)

# /users rather than /admin/users. The /admin prefix is taken by the workflow
# screens and the share register, and this screen is about people rather than
# about configuration. One noun, and no path here overlaps another router's.
USERS_URL = "/users"
PROVISION_URL = "/users/provision"

# The permission the whole screen sits behind. Spelt once; app/state/models.py
# defines it and app/auth/policy.py raises at import if a gated code stops
# existing.
MANAGE = "user.manage"


def resend_url(invitation_id: str) -> str:
    """Where the Resend button posts. One spelling, for the route and the form."""
    return f"{USERS_URL}/{invitation_id}/resend"


REFUSED = (
    f"{MANAGE} is required to read this screen or to add a login. Ask an "
    "administrator. It lists every account in this workspace and when each one "
    "was last used, which is a record about other people."
)

# ---------------------------------------------------------------------------
# The words that carry the distinction this screen exists for
#
# Constants rather than literals in a template, because the tests assert on them
# and a screen whose meaning lives in a Jinja string is a screen whose meaning
# can be edited away in a diff nobody reads.
# ---------------------------------------------------------------------------

#: An account nobody has ever signed in as. NOT a blank and NOT a dash.
NEVER_SIGNED_IN = "Never signed in"
#: ...and the account WORKS. Whoever it belongs to could sign in today and has
#: not. The strongest suspension candidate on the page, and the one a first
#: draft of this screen got wrong: it said "no invitation has ever been issued",
#: which is true, irrelevant, and reads as a fault in the product.
NEVER_USED_ACTIVE = "the account works and nobody has ever used it"
#: ...and the account is shut. Nobody should be getting in with it, so this is
#: the one never-signed-in row that is not a question for anybody.
NEVER_USED_SUSPENDED = "the account is suspended, and it was never used"
#: ...and their link still works, so there is nothing to do yet.
INVITE_STILL_OPEN = "the invitation is still open"
#: ...and their link is dead, so nobody can get in with it. Resend is the fix.
#:
#: ONLY EVER SAID OF AN ACCOUNT THAT IS STILL WAITING. Somebody who accepted and
#: then never signed in has a dead invitation too -- it is dead because they
#: used it -- and calling that "never taken up" would be the screen stating the
#: opposite of what happened. The status decides this, not the invitation.
INVITE_LAPSED = "the invitation was never taken up"
#: ...and there is no invitation at all to take up. An account at
#: STATUS_INVITED with nothing to accept can only reach a password through a
#: fresh provision, and it is as stranded as one whose link ran out.
NO_INVITATION = "no invitation has ever been issued for this account"

#: An account that has signed in, but not lately. A candidate for suspension,
#: which is the other thing an administrator is scanning for.
DORMANT_DAYS = 90
DORMANT = "not used lately"

JUST_NOW = "just now"

# What the ceiling costs, said on the screen rather than only in the write
# layer's docstring. An administrator who expected to see "analyst" in the
# picker deserves the reason instead of a shrug.
CEILING_NOTE = (
    "You can only grant a role whose permissions you already hold. The admin "
    "role holds none of the analyst permissions and neither approval "
    "permission, so this list is short on purpose: provisioning a login must "
    "never be a way to reach authority nobody gave you."
)

NO_ROLES = (
    "There is no role you may hand on, so no login can be provisioned from this "
    "account. That is the ceiling rule answering, not a fault: ask for a role "
    "that holds what you are meant to be able to pass on."
)

# The two sentences the one-time link is wrapped in.
LINK_ONCE = (
    "This is the only time this link exists. It is not stored anywhere -- the "
    "row holds a hash of it -- and it is not in the audit record either. Copy "
    "it now; reloading this page will not bring it back."
)
# ---------------------------------------------------------------------------
# The mail layer, which this module calls and does not reimplement
#
# WHY IT IS REACHED BY NAME AT THE MOMENT OF USE, rather than imported at the
# top like app/state/invites.py. That module is what this screen is MADE of --
# without it there is no account list and no page. app/notify is not: without it
# the screen still provisions the login, still mints the link, and still shows
# the link, and the only thing missing is that nobody was emailed. So it is the
# case app/web/views/shares_admin.py met and answered -- a dependency being
# written in parallel that one screen can lose without becoming a lie -- and the
# answer is the same one: reach for it when it is needed, and when it is not
# there say so on the page and change nothing.
#
# THE MAIL IS SENT OUTSIDE THE DATABASE TRANSACTION, on purpose. deliver() opens
# a socket, and a relay that accepts the connection and then hangs would hold a
# write transaction open for its timeout. The invitation is committed first and
# the mail is attempted after: an invitation that exists and was not delivered
# is a state the screen can describe, and a transaction held open by SMTP is not.
#
# NOTHING HERE DECIDES WHETHER MAIL WENT. deliver() returns a Delivery carrying
# an announcement on EVERY outcome, including success, and this screen prints
# that sentence. A screen writing its own "sent" would be this file's opinion of
# what another module did with a socket.
# ---------------------------------------------------------------------------

NOTIFY_MODULE = "app.notify"

# The contract, spelt once. A mismatch is caught at the call and reported on the
# page rather than raised: a 500 on an admin screen says nothing about which of
# the two sides changed, and this one is checkable.
MAIL_FN_NEW = "invitation_message"
MAIL_FN_RESENT = "resent_invitation_message"
MAIL_FN_DELIVER = "deliver"
MAIL_FN_TRANSPORT = "transport_from_environment"

NO_MAIL_LAYER = (
    f"No mail was sent: {NOTIFY_MODULE} is not in this build, so nothing here "
    "can email anybody. The link above is real and still valid -- pass it on "
    "yourself. Until something sends it, an invitation nobody delivered looks "
    "exactly like one nobody accepted, and this product cannot tell them apart."
)

# THE ANNOUNCED FALLBACK. See the head of this module.
GUARD_CLOSED = (
    "This link will not open for them yet. app/web/deps.py does not let a "
    f"request for {ACCEPT_PATH_PREFIX}/... past the session guard, so whoever "
    "clicks it is sent to the sign-in page and has no account to sign in with. "
    "The account and the invitation are real; only the door is missing. The "
    "two lines that fix it are in the handoff at the foot of "
    "app/web/views/invite_accept.py."
)

# ---------------------------------------------------------------------------
# Audited actions
#
# THIS MODULE DECLARES NONE. Every act on this screen is performed by
# app/state/invites.py, which audits what it did under a code it owns --
# ACTION_INVITE_CREATED, ACTION_INVITE_SUPERSEDED, ACTION_INVITE_REFUSED -- and
# app/auth/policy.py writes its own ACCESS_DENIED row for a refusal. A row
# written here under a second spelling would be invisible to every query for the
# first: in the log and unfindable, which is worse than missing.
# ---------------------------------------------------------------------------

# One heading per reason code, so an administrator reads what to do before they
# read the paragraph explaining it. The write layer's own sentence is printed
# underneath every one of these, unrewritten: why a role exceeds a ceiling, why
# an address already has an account, why a handoff cannot be resent -- those are
# decisions this screen did not make and must not paraphrase.
OUTCOME_HEADINGS = {
    INV_OK: "Done.",
    INV_NO_EMAIL: "No address, so nobody was invited.",
    INV_EMAIL_MALFORMED: "That address cannot be read.",
    INV_NO_NAME: "No name, so the account would not be attributable.",
    INV_NO_ROLE: "No role was chosen.",
    INV_GRANT_EXCEEDS: "That role is above what you may grant.",
    INV_ALREADY_A_USER: "That address already has an account here.",
    INV_ALREADY_LIVE: "There is already a live invitation to that address.",
    INV_DISABLED: "Invitations are switched off for this workspace.",
    INV_NO_INVITER: "The account acting here is not an active account.",
    INV_NOT_PERMITTED: "You may not do that.",
    INV_UNKNOWN: "There is no such invitation in this workspace.",
    INV_NOT_PROVISION: "That is a handoff invitation, not a provisioned login.",
    INV_ALREADY_ACCEPTED: "That invitation was already accepted.",
    INV_WAS_REVOKED: "That invitation was withdrawn.",
}

# The list is not tenant data, but it is a list of who works here and when they
# last signed in, and the one-time link is on the same page. Neither belongs in
# a shared cache, and the Referer of any link clicked from here should not carry
# the URL of a page holding a credential.
SCREEN_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

_STATUS_BADGE = {
    STATUS_ACTIVE: "badge--active",
    STATUS_INVITED: "badge--draft",
    STATUS_SUSPENDED: "badge--closed",
}

# Active first, then people who have not accepted, then the suspended. A status
# not in this list sorts last rather than vanishing: a vocabulary that grows
# must not make rows disappear from an admin screen.
_STATUS_ORDER = (STATUS_ACTIVE, STATUS_INVITED, STATUS_SUSPENDED)


# ---------------------------------------------------------------------------
# Time, in the two shapes a person needs at once
# ---------------------------------------------------------------------------

# Seconds per unit, largest first. Months and years are the average lengths --
# 30.44 and 365.25 days -- because "3 months ago" is a thing somebody scans and
# not a thing they compute with. The exact stamp is beside it for that.
_UNITS = (
    ("year", 365.25 * 86400),
    ("month", 30.44 * 86400),
    ("day", 86400),
    ("hour", 3600),
    ("minute", 60),
)


def _span(seconds: float) -> str:
    """"3 months", "6 hours". Magnitude only; the caller adds the direction."""
    for name, size in _UNITS:
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {name}" if count == 1 else f"{count} {name}s"
    return JUST_NOW


def relative(moment: datetime, now: datetime) -> str:
    """"3 months ago", "in 6 hours", "just now". Never an empty string.

    Both directions, because this screen shows a login that happened and an
    expiry that has not. One function so the two read alike; a separate
    "time until" would drift in its rounding and an administrator comparing a
    row's two columns would be comparing two different conventions.
    """
    seconds = (now - moment).total_seconds()
    if abs(seconds) < 60:
        return JUST_NOW
    span = _span(abs(seconds))
    return f"{span} ago" if seconds > 0 else f"in {span}"


def _stamp(moment: datetime | None) -> str:
    """The exact time, in UTC, for the person who is about to act on it.

    Empty string for None, and every caller checks the absence FIRST rather than
    printing this. A blank cell is the failure this whole screen is built to
    avoid, so nothing here may produce one by accident.
    """
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _iso(moment: datetime | None) -> str:
    """For <time datetime="...">, which is what a screen reader reads out."""
    return "" if moment is None else moment.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# What the template renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonRow:
    """One account, with the last-login distinction already resolved into words.

    `signed_in` is the branch and `never_because` is what to say when it is
    False. The template does not test None anywhere: AccountRow.last_login_at
    arrives as None and is turned into a sentence HERE, once, where the reason
    for the sentence is written down. A template deciding this would be one
    `{% if %}` away from rendering an empty cell for both cases, which is the
    defect this screen exists to prevent.
    """

    user_id: str
    display_name: str
    email: str
    roles: tuple[str, ...]
    status: str
    status_badge: str

    signed_in: bool
    #: "" when they have signed in. One of the three constants otherwise.
    never_because: str
    last_login_relative: str
    last_login_exact: str
    last_login_iso: str
    dormant: bool

    invitation_id: str | None
    invitation_status: str | None
    invitation_live: bool
    invitation_expires_relative: str
    invitation_expires_exact: str
    #: A resend is offered only where it can do something. See _person_row.
    can_resend: bool
    #: Waiting, and with no live link to reach them by. Resend, or nobody gets
    #: in. This is the first of the two things an administrator is scanning for.
    stranded: bool
    #: Able to sign in, and never has. The second of the two, and the sharper
    #: suspension candidate: dormancy at least started with somebody using it.
    never_used: bool


def _person_row(account: AccountRow, now: datetime) -> PersonRow:
    """One AccountRow, with the two facts an administrator is scanning for split.

    THE ONLY PLACE last_login_at BECOMES WORDS. Every outcome is a different
    instruction to the person reading:

      signed in                     read the date, decide about dormancy
      never, account active         they can sign in and never have; suspend?
      never, account suspended      nothing to do; the account is shut
      never, waiting, link live     wait; the expiry says how long you can
      never, waiting, no live link  resend, or nobody is ever getting in

    THE STATUS IS ASKED BEFORE THE INVITATION, and that ordering is the whole
    correctness of this function. An account that accepted its invitation and
    then never signed in has a dead invitation -- dead because it was used --
    and asking the invitation first printed "the invitation was never taken up"
    about the one person on the page who did everything they were asked to.
    Asking the status first makes that unsayable.

    AN UNKNOWN STATUS SAYS SO rather than falling into one of the sentences
    above, the way invites_admin.html handles a kind it has no words for. A word
    this screen does not recognise is not evidence that somebody is waiting for
    a link, and it is not evidence that they are not.

    A resend is offered for a provisioned login that has not been accepted --
    which includes the lapsed case and the still-open case, because an
    administrator whose colleague lost the mail should not have to wait for the
    clock to run out first. resend() refuses an accepted or withdrawn one and
    says why, so this flag never has to be the last word.
    """
    signed_in = account.has_signed_in
    if signed_in:
        because = ""
    elif account.status == STATUS_ACTIVE:
        because = NEVER_USED_ACTIVE
    elif account.status == STATUS_SUSPENDED:
        because = NEVER_USED_SUSPENDED
    elif account.status != STATUS_INVITED:
        because = (
            f"the account is {account.status}, which this screen has no words for"
        )
    elif account.invitation_id is None:
        because = NO_INVITATION
    elif account.invitation_live:
        because = INVITE_STILL_OPEN
    else:
        because = INVITE_LAPSED

    dormant = bool(
        signed_in
        and account.status == STATUS_ACTIVE
        and (now - account.last_login_at).days >= DORMANT_DAYS
    )

    return PersonRow(
        user_id=account.user_id,
        display_name=account.display_name,
        email=account.email,
        roles=account.roles,
        status=account.status,
        status_badge=_STATUS_BADGE.get(account.status, "badge--draft"),
        signed_in=signed_in,
        never_because=because,
        last_login_relative=(
            relative(account.last_login_at, now) if signed_in else ""
        ),
        last_login_exact=_stamp(account.last_login_at),
        last_login_iso=_iso(account.last_login_at),
        dormant=dormant,
        invitation_id=account.invitation_id,
        invitation_status=account.invitation_status,
        invitation_live=account.invitation_live,
        invitation_expires_relative=(
            relative(account.invitation_expires_at, now)
            if account.invitation_expires_at is not None
            else ""
        ),
        invitation_expires_exact=_stamp(account.invitation_expires_at),
        can_resend=bool(
            account.invitation_id is not None
            and account.invitation_kind == INVITE_KIND_PROVISION
            and account.status == STATUS_INVITED
        ),
        # The status is named again rather than trusted to the branch above. A
        # suspended account with nothing to accept is not stranded -- nobody
        # wants it reachable -- and that must not depend on the order two
        # `elif`s happen to be in.
        stranded=(
            account.status == STATUS_INVITED
            and because in (INVITE_LAPSED, NO_INVITATION)
        ),
        never_used=because == NEVER_USED_ACTIVE,
    )


def _rows(session: Session, company_id: str, now: datetime) -> list[PersonRow]:
    """Every account here, active first, then the ones still waiting."""
    rows = [
        _person_row(account, now)
        for account in accounts_for_company(session, company_id=company_id, now=now)
    ]
    order = {status: index for index, status in enumerate(_STATUS_ORDER)}
    rows.sort(key=lambda row: (order.get(row.status, len(_STATUS_ORDER)), row.email))
    return rows


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _user_id(request: Request) -> str:
    principal = current_user(request)
    return principal.user_id if principal is not None else ""


def _may_manage(session: Session, company_id: str, user_id: str) -> str | None:
    """None when allowed, else the reason. The refusal is audited on the way out.

    require() writes the audit row and then raises. The raise is caught HERE,
    inside the caller's session scope, so the row commits with the response.
    Catching it outside would roll the refusal back with everything else, and a
    denial nobody recorded is a denial nobody can review.
    """
    try:
        policy.require(session, company_id, user_id, MANAGE)
    except policy.PermissionDenied as denied:
        return denied.reason
    return None


def _waiting(session: Session, company_id: str) -> int:
    """The nav's held-for-review count. Zero on a database with no tables.

    Read on the refusal path too: a masthead that drops its count on one screen
    tells the reader the queue emptied, and it did not.
    """
    try:
        return len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        return 0


def _shell(company_id: str, waiting: int) -> dict:
    """base.html's context, plus the URLs every form on this page posts to."""
    context = chrome(
        company_id,
        page_title="Logins",
        # Not in the masthead. Marking one of the six as current while an
        # administrator is here would be a small lie in an aria-current
        # attribute.
        nav_active="",
        review_count=waiting,
    )
    context.update(
        {
            "users_url": USERS_URL,
            "provision_url": PROVISION_URL,
            "permission": MANAGE,
            "refused": REFUSED,
            "never_signed_in": NEVER_SIGNED_IN,
            "dormant_word": DORMANT,
            "dormant_days": DORMANT_DAYS,
            "ceiling_note": CEILING_NOTE,
            "no_roles": NO_ROLES,
            "link_once": LINK_ONCE,
            "ttl_hours": INVITE_PROVISION_TTL_HOURS,
        }
    )
    return context


def _forbidden(
    request: Request, company_id: str, reason: str, waiting: int
) -> HTMLResponse:
    """One template, two answers. A separate page would describe one screen twice."""
    context = _shell(company_id, waiting)
    context["reason"] = reason
    return templates.TemplateResponse(
        request, TEMPLATE, context, status_code=403, headers=SCREEN_HEADERS
    )


def _mail_layer() -> tuple[object | None, str]:
    """The mail module and its four names, or None and the sentence to print.

    Never raises. No module, and a module missing one of the four functions, are
    both ordinary states of a build being assembled by several hands, and both
    have to reach the screen as a sentence rather than as a stack trace.
    """
    import importlib

    try:
        module = importlib.import_module(NOTIFY_MODULE)
    except ImportError:
        return None, NO_MAIL_LAYER
    wanted = (MAIL_FN_NEW, MAIL_FN_RESENT, MAIL_FN_DELIVER, MAIL_FN_TRANSPORT)
    absent = [name for name in wanted if getattr(module, name, None) is None]
    if absent:
        return None, (
            f"No mail was sent: {NOTIFY_MODULE} does not define "
            f"{', '.join(absent)}, so this screen and the mail layer disagree "
            "about how to send an invitation. The link above is real and still "
            "valid -- pass it on yourself."
        )
    return module, ""


def _send(
    request: Request,
    *,
    to_email: str,
    accept_url: str,
    expires_at: datetime,
    resent: bool,
) -> str:
    """Email the link, and return the sentence saying what became of it.

    NEVER RAISES AND NEVER RETURNS AN EMPTY STRING. Something is always printed
    beside a link this screen has just handed over, because the one thing an
    administrator must not have to guess is whether the person on the other end
    was told. deliver() sets an announcement on every outcome for exactly that
    reason, and this returns it unrewritten.

    THE SAME accept_url THAT IS ON THE SCREEN GOES INTO THE MAIL. Built once, by
    _link_for, and passed to both. Composing a second one here would be a second
    chance to get the host wrong, and the failure would be a mail whose link is
    not the link the administrator was looking at.

    A RESEND SENDS A DIFFERENT MAIL, and that is not decoration: the replacement
    says the previous link has stopped working, which is true the instant this
    one was minted rather than at the old one's expiry. A recipient holding both
    mails otherwise tries the first, gets the one sentence a dead invitation
    gets, and decides the product is broken.
    """
    notify, blocked = _mail_layer()
    if notify is None:
        return blocked

    principal = current_user(request)
    compose = getattr(notify, MAIL_FN_RESENT if resent else MAIL_FN_NEW)
    try:
        message = compose(
            to_email=to_email,
            company_name=deps.company_name(current_company_id()),
            # Who pressed the button, which on a resend may not be who
            # provisioned the login. The invitation row records the same person.
            invited_by=(
                principal.display_name if principal is not None else "an administrator"
            ),
            accept_url=accept_url,
            expires_at=expires_at,
        )
        delivery = getattr(notify, MAIL_FN_DELIVER)(
            message, getattr(notify, MAIL_FN_TRANSPORT)()
        )
    except (TypeError, ValueError, AttributeError) as mismatch:
        # The contract moved under us, or a composed value was refused.
        # Announced, not raised: the invitation is written and the link is on
        # screen, and a 500 here would hide both.
        return (
            f"No mail was sent: {NOTIFY_MODULE} did not accept the call this "
            f"screen makes ({mismatch}). The link above is real and still valid "
            "-- pass it on yourself."
        )
    # Its sentence, not one written here. See the head of this section.
    return delivery.announcement


def _guard_note() -> str:
    """"" when an acceptance link would open, else why it would not.

    ASKS THE GUARD RATHER THAN ASSUMING IT. app/web/deps.py is the one place
    that decides what an anonymous request may reach, so this calls the same
    function the middleware calls instead of keeping an opinion about it here.
    When the prefix is listed, this returns "" and the warning disappears with
    no edit to this file.
    """
    if deps.is_public_path(accept_url("a" * 43)):
        return ""
    return GUARD_CLOSED


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _page(
    request: Request,
    *,
    outcome_code: str = "",
    outcome_text: str = "",
    link: str = "",
    link_expires: str = "",
    mail_note: str = "",
    typed: dict | None = None,
) -> HTMLResponse:
    """Read the list and render it, with whatever just happened above it.

    ONE FUNCTION FOR ALL THREE ENTRY POINTS -- the plain GET, a provision and a
    resend -- because all three end on the same page and the two writes must not
    be able to show a different list from the one the GET shows. The list is
    read AFTER the write, from the rows, so what is on screen is what is in the
    database rather than what the write layer said it did.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, rows, waiting, roles = None, [], 0, ()
    now = datetime.now(timezone.utc)

    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, user_id)
            if reason is None:
                rows = _rows(session, company_id, now)
                roles = grantable_roles(
                    session, company_id=company_id, actor=user_id
                )
    except SQLAlchemyError:
        # A database that cannot be read is a permission check that did not run,
        # and a check that did not run is not a check that passed. On a clone
        # with no tables this screen would otherwise open for anybody, empty --
        # which shows no data and is still the gate failing open.
        reason = (
            "this workspace's roles could not be read, so nothing confirmed "
            f"that you hold {MANAGE}"
        )
        # `waiting` keeps whatever was read before the failure, which is 0 when
        # nothing was. Setting it to 0 here would report an empty review queue
        # on the strength of a failure that had nothing to do with the queue.

    if reason is not None:
        return _forbidden(request, company_id, reason, waiting)

    context = _shell(company_id, waiting)
    context.update(
        {
            "rows": rows,
            "roles": roles,
            # Three counts, three different actions. A single "never signed in"
            # count would put the person you must resend to and the person you
            # should probably suspend in the same number, which is the merge
            # this whole screen exists to undo.
            "stranded_count": sum(1 for row in rows if row.stranded),
            "unused_count": sum(1 for row in rows if row.never_used),
            "dormant_count": sum(1 for row in rows if row.dormant),
            # The heading is this screen's, keyed by the code. The paragraph
            # under it is the write layer's own sentence, unrewritten.
            "outcome_code": outcome_code,
            "outcome_heading": OUTCOME_HEADINGS.get(outcome_code, outcome_code),
            "outcome_text": outcome_text,
            "outcome_ok": outcome_code == INV_OK,
            "link": link,
            "link_expires": link_expires,
            "mail_note": mail_note,
            "guard_note": _guard_note() if link else "",
            "typed": typed or {},
        }
    )
    # 200 on a refusal from the write layer as well as on a success, which is
    # what shares_admin.py does for the same reason: nobody was denied and
    # nothing failed. The refusal is a sentence at the top of a page that still
    # works, and the reader can act on it without leaving.
    return templates.TemplateResponse(
        request, TEMPLATE, context, headers=SCREEN_HEADERS
    )


@router.get(USERS_URL, response_class=HTMLResponse)
def user_list(request: Request) -> HTMLResponse:
    """Every account in this workspace, and the form that adds another."""
    return _page(request)


def _link_for(request: Request, outcome) -> tuple[str, str]:
    """The absolute acceptance URL and when it runs out, or two empty strings.

    Absolute, because the administrator is going to paste it into an email and a
    relative path is not a link there. Built from request.base_url rather than
    from a configured hostname, so it is the host they actually reached this
    page on -- a link built from a setting somebody forgot to change is a link
    to the wrong deployment.
    """
    if outcome.token is None or outcome.invitation is None:
        return "", ""
    base = str(request.base_url).rstrip("/")
    expires = outcome.invitation.expires_at
    now = datetime.now(timezone.utc)
    return (
        base + accept_url(outcome.token),
        f"{relative(expires, now)} ({_stamp(expires)})",
    )


@router.post(PROVISION_URL, response_class=HTMLResponse)
def provision(
    request: Request,
    email: str = Form(default=""),
    display_name: str = Form(default=""),
    role: str = Form(default=""),
) -> HTMLResponse:
    """Add a login. Renders the one-time link; never redirects to it.

    THE ACCOUNT IS NOT CREATED HERE. provision_login() in app/state/invites.py
    writes the User through create_user, grants the role through grant_role,
    mints the invitation and audits all three. Every rule about who may do it,
    which roles they may hand on, and what happens to an address that already
    has an account lives there, checked once, with tests pointed at it.

    THE PERMISSION IS CHECKED TWICE AND THAT IS DELIBERATE. Here, so the refusal
    is a 403 with the screen's own sentence and no list is read; and again
    inside provision_login, which is the gate that holds when somebody calls
    that function from a script. Neither is redundant: this one exists so the
    screen behaves like a screen, that one exists so the rule is not a property
    of the screen.

    NOTHING IS VALIDATED HERE. An empty address, a malformed one, a missing name
    and a role above the ceiling are five different refusals with five different
    fixes, and every one of them is decided by the write layer and printed in
    its words. A check copied into this view would be a second opinion about who
    may exist.
    """
    company_id = current_company_id()
    user_id = _user_id(request)

    code, text, link, expires, waiting = "", "", "", "", 0
    recipient, runs_out = "", None
    with session_scope() as session:
        # Read on the refusal path too: a masthead that drops its count on one
        # screen tells the reader the review queue emptied, and it did not.
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            outcome = provision_login(
                session,
                company_id=company_id,
                # A USER ID, not a display string. Invitation.invited_by_user_id
                # is a foreign key and the permission gate needs an account; a
                # label like "person:ada@mep.example" returns INV_NO_INVITER,
                # which reads like a broken account rather than a wrong argument.
                actor=user_id,
                email=email,
                display_name=display_name,
                role=role,
                now=datetime.now(timezone.utc),
            )
            code, text = outcome.reason_code, outcome.reason_text
            link, expires = _link_for(request, outcome)
            if outcome.invitation is not None:
                # Copied out while the row is in hand. The mail goes after the
                # transaction closes, and reaching back into a row from there
                # would be a read on a session that has gone.
                recipient = outcome.invitation.email
                runs_out = outcome.invitation.expires_at

    if reason is not None:
        return _forbidden(request, company_id, reason, waiting)

    # The invitation is committed by here. The mail is attempted after, so a
    # relay that hangs cannot hold a write transaction open, and a mail that
    # fails leaves an account that exists and a screen that says nobody was told.
    mail_note = (
        _send(
            request,
            to_email=recipient,
            accept_url=link,
            expires_at=runs_out,
            resent=False,
        )
        if link and runs_out is not None
        else ""
    )

    return _page(
        request,
        outcome_code=code,
        outcome_text=text,
        link=link,
        link_expires=expires,
        mail_note=mail_note,
        # Handed back so a refused form does not make somebody type it again.
        # Never on success: the boxes are empty for the next person.
        typed=(
            {}
            if code == INV_OK
            else {"email": email, "display_name": display_name, "role": role}
        ),
    )


@router.post(USERS_URL + "/{invitation_id}/resend", response_class=HTMLResponse)
def resend_invitation(request: Request, invitation_id: str) -> HTMLResponse:
    """A NEW link. The previous one stops working at this instant.

    NOT AN EXTENSION, AND THE DIFFERENCE IS THE SECURITY PROPERTY. resend() in
    app/state/invites.py mints a new token and moves every live invitation to
    that address to INVITE_SUPERSEDED. Nothing touches expires_at on the
    standing row -- the dead row keeps the life it actually had, because
    rewriting it would erase what that link's life was.

    An unknown id, another tenant's id, an accepted invitation, a withdrawn one
    and a handoff are all refused there and printed here in that module's words.
    Another tenant's id and one that was never issued get the same answer on
    purpose: telling them apart would tell this administrator that some other
    workspace holds that id.
    """
    company_id = current_company_id()
    user_id = _user_id(request)

    code, text, link, expires, waiting = "", "", "", "", 0
    recipient, runs_out = "", None
    with session_scope() as session:
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            outcome = resend(
                session,
                company_id=company_id,
                actor=user_id,
                invitation_id=invitation_id,
                now=datetime.now(timezone.utc),
            )
            code, text = outcome.reason_code, outcome.reason_text
            link, expires = _link_for(request, outcome)
            if outcome.invitation is not None:
                recipient = outcome.invitation.email
                runs_out = outcome.invitation.expires_at

    if reason is not None:
        return _forbidden(request, company_id, reason, waiting)

    # resent=True, so the mail says the previous link has stopped working. It
    # has -- at the instant the row above was written, not at its own expiry --
    # and a recipient holding both mails who tries the older one gets the single
    # sentence a dead invitation gets, with nothing to tell them why.
    mail_note = (
        _send(
            request,
            to_email=recipient,
            accept_url=link,
            expires_at=runs_out,
            resent=True,
        )
        if link and runs_out is not None
        else ""
    )

    return _page(
        request,
        outcome_code=code,
        outcome_text=text,
        link=link,
        link_expires=expires,
        mail_note=mail_note,
    )


# ---------------------------------------------------------------------------
# HANDOFF -- what this module needs from files it does not own
#
# app/main.py           `users_admin` and `invite_accept` added to the import
#                       from app.web.views, and both routers included:
#
#                         app.include_router(users_admin.router)
#                         app.include_router(invite_accept.router)
#
#                       tests/test_app_wiring.py derives its answer from the
#                       views package rather than a hand-written list, so a
#                       router nobody mounted fails there rather than 404ing in
#                       front of a reviewer. Neither claims a path another
#                       router owns: /users, /users/provision,
#                       /users/{invitation_id}/resend and /invite/{token}.
#
# app/web/deps.py       REQUIRED for the acceptance link to open at all. The two
#                       lines are written out at the foot of
#                       app/web/views/invite_accept.py. Until they land this
#                       screen prints GUARD_CLOSED beside every link it hands
#                       over, which is the announcement rather than the fix.
#
# app/web/templates/base.html
#                       Nothing is required. This screen is not in the six-item
#                       masthead, for the reason base.html's own note gives: a
#                       nav item most people cannot reach renders as dead text.
#                       If an admin menu is added later, /users belongs in it
#                       beside /admin/workflows and /admin/shares.
#
# app/state/invites.py  Nothing required. This screen calls provision_login(),
#                       resend(), accounts_for_company() and grantable_roles()
#                       with the shapes that module publishes. ACCEPT_PATH_PREFIX
#                       and accept_url() belong there rather than in a view; see
#                       the note where they are defined.
#
# app/state/queries.py  Nothing. Every read on this page goes through
#                       accounts_for_company(), which is already company_id
#                       keyword-only with no default. This module defines no
#                       query of its own, which is why there is no list of reads
#                       to move here.
#
# app/notify            Nothing required, and it is already wired. This screen
#                       calls invitation_message() / resent_invitation_message(),
#                       transport_from_environment() and deliver() by name at
#                       the moment of use, so a change to any of those four
#                       signatures is reported on the page rather than raised.
#                       Two notes for whoever owns that module:
#
#                       accept_link() is NOT called here. The link is built once
#                       by _link_for(), from request.base_url and the acceptance
#                       route this repository owns, and the same string goes to
#                       the screen and into the mail. Composing it twice is two
#                       chances to get the host wrong, and the failure would be a
#                       mail whose link is not the one the administrator saw.
#
#                       Nothing on this screen sets a From address or a relay.
#                       transport_from_environment() answers None on a machine
#                       with no relay, deliver() turns that into its announced
#                       fallback, and the page prints that sentence beside a
#                       link that still works. That is the state a reviewer
#                       running `make run` sees and it is not a failure.
# ---------------------------------------------------------------------------


__all__ = [
    "CEILING_NOTE",
    "DORMANT",
    "DORMANT_DAYS",
    "GUARD_CLOSED",
    "INVITE_LAPSED",
    "INVITE_STILL_OPEN",
    "JUST_NOW",
    "LINK_ONCE",
    "MAIL_FN_DELIVER",
    "MAIL_FN_NEW",
    "MAIL_FN_RESENT",
    "MAIL_FN_TRANSPORT",
    "MANAGE",
    "NEVER_SIGNED_IN",
    "NEVER_USED_ACTIVE",
    "NEVER_USED_SUSPENDED",
    "NO_INVITATION",
    "NOTIFY_MODULE",
    "NO_MAIL_LAYER",
    "NO_ROLES",
    "OUTCOME_HEADINGS",
    "PROVISION_URL",
    "REFUSED",
    "SCREEN_HEADERS",
    "TEMPLATE",
    "TEMPLATES_OWNED",
    "TEMPLATE_DIR",
    "USERS_URL",
    "PersonRow",
    "provision",
    "relative",
    "resend_invitation",
    "resend_url",
    "router",
    "templates",
    "user_list",
]
