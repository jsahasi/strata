"""The share and invitation registry: what has left the building, and how to stop it.

WHAT THIS SCREEN IS FOR, IN ONE SENTENCE. Enterprise security teams block
products that share invisibly and permit products that share visibly, so this is
the control that makes a share link a thing an administrator can see rather than
a thing they have to trust. It is not a list with a delete button. An admin has
to be able to answer, without asking anybody: what is shared right now, by whom,
with what, when does it expire, has it been opened, from where, and did the
claim still verify when it was opened. Then stop any of it in one click.

THE LAST OF THOSE IS THE ONE THAT MATTERS. A link opened three times where the
third open showed a refusal is a real compliance event -- somebody outside this
company was shown "this statement is withheld and here is why" instead of a
statement. It is also the strongest evidence the product does what it claims, so
it is on the page per open, in the alarm treatment the app already uses for a
withheld claim, and not folded into a count.

FOUR DECISIONS THIS MODULE MAKES.

**The opens are the evidence and the counters are checked against them.**
ShareLink carries open_count and last_opened_at, which app/state/models.py
justifies as a convenience for exactly this screen -- one query per row is too
many on the one page that lists many. This module reads every open for the
company in ONE query instead, so it does not need the convenience, and having
the true numbers in hand it compares them with the stored ones and says on the
page when they disagree. models.py is explicit that a reader who finds them
disagreeing has found a defect in the writer and must believe the rows. A
registry that quietly showed the larger number would hide the defect it is best
placed to find.

**Revoked outranks expired.** A dead link is dead either way, and the state
worth showing is the one a person decided. Expiry is a clock running out.

**Nothing on these screens performs the act it offers.** Revoking is
revoke_share_link() in app/state/sharing.py, the same function the sharer's own
button calls. A decision on an invitation is approve_invitation() or
revoke_invitation() in app/state/invites.py. Bulk is a loop around one act,
never a second implementation of it -- a register that wrote the rows itself
would be a second answer to who may stop a link, and a second audit vocabulary
for the same event.

**A refusal from a write layer is rendered in its own words.** Why an
invitation is not a routing gap, why it has expired, why this account may not
release it -- that is a paragraph from the module that decided it, and this
screen prints it rather than paraphrasing a decision it did not make. Only a
decision that landed redirects, so a reload cannot post it twice.

THREE LIMITS, ALL VISIBLE ON THE SCREEN RATHER THAN ONLY IN THIS COMMENT.

1. *Sharing is on and nobody can turn it off.* sharing_enabled() answers from a
   default, because there is no company_settings table yet -- the proposal is at
   the foot of app/state/models.py. The register shows the value, the source and
   that module's own sentence about it, because "on by default, nobody has set
   it" is a different fact from "somebody turned it on", and a switch rendered
   as a bare word hides which of the two a reviewer is reading. There is no
   screen here to set it, and that is said where an administrator will see it.

2. *The invitation queue is only as available as app/state/invites.py.* Both the
   list and the two decisions come from that module, reached by name at the
   moment of use -- which is what keeps the share register, a different screen
   about a different thing, working while that module is edited under us. When
   it is unreachable the queue shows the banner naming it and no rows at all,
   and the button answers 501 with the same sentence. The tempting alternative
   is flipping the status locally so the button looks like it worked, which
   leaves an invitation marked approved with no account behind it.

3. *There is no CSRF token on these forms.* SameSite=Lax on the session cookie
   is the whole of the defence, exactly as app/web/views/auth.py concedes for
   the login form. It stops a cross-site POST in every browser this product
   supports, and it is not a token.

WHERE THE READS BELONG. The four scoped reads in this file belong in
app/state/queries.py beside versions_for_company. They are here because that
file is owned by another agent in this build and a parallel edit would lose
their work. The tenant guard itself is IMPORTED rather than copied, so there is
still one definition of what an unscoped read is. Same handoff
app/web/views/admin.py left for the same reason. Two of them read the whole page
rather than one link, which is why they are not the per-link reads
app/state/sharing.py already has: one query for a register of many beats one
query per row, and the rest of that module's reads are the right shape for the
screens that show one thing.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# THE MODULE, NOT THE NAMES. app/web/views/admin.py gives the reasoning at
# length: app/auth/policy.py reads its approval mode at import and the tests
# reload it, so an `except` bound to a class object captured at import time
# stops catching the refusal the reloaded module raises, and a 403 becomes a
# 500. Reaching through the module looks the name up when it is needed.
from app.auth import policy
from app.state.db import session_scope
from app.state.identity import users_for_company
from app.state.models import (
    INVITE_ACCEPTED,
    INVITE_APPROVED,
    INVITE_AWAITING_APPROVAL,
    INVITE_EXPIRED,
    INVITE_KIND_HANDOFF,
    INVITE_KIND_PROVISION,
    INVITE_PENDING,
    INVITE_REVOKED,
    SHARE_SUBJECT_CHANGE,
    SHARE_SUBJECT_CLAIM,
    Change,
    Claim,
    Invitation,
    ShareLink,
    ShareOpen,
)

# One definition of an unscoped read, imported rather than restated. Two copies
# of a tenant guard drift, and the copy nobody audited is the one that leaks.
from app.state.queries import _require_scope

# THE SHARE WRITE LAYER. Revoking is performed there and not here: it is the
# same act whether a sharer withdraws their own link or an administrator stops
# somebody else's, and the rule about who may do which lives in one function. It
# also owns the audited action code, so this screen imports the code rather than
# spelling it a second time -- a row written under a second spelling is
# invisible to every query for the first.
from app.state.sharing import (
    ACTION_SHARE_REVOKED,
    SETTING_SOURCE_DEFAULT,
    SETTING_SOURCE_TENANT,
    revoke_share_link,
    sharing_enabled,
)
from app.web import TEMPLATES_DIR
from app.web.deps import current_user
from app.web.templating import build_templates

# The page shell and the tenant, decided in one place for every screen.
from app.web.views.proceedings import chrome, current_company_id, unresolved_escalations

router = APIRouter()
templates = build_templates()

# The two screens and the one write path the register itself owns. /admin is
# already where app/web/views/admin.py puts the gated screens, and a second
# prefix for the same kind of thing would be a menu nobody can guess.
SHARES_URL = "/admin/shares"
INVITES_URL = "/admin/invites"
REVOKE_URL = "/admin/shares/revoke"

# The change screen, where a shared claim or change is read inside the product.
CHANGE_URL = "/changes"

# The permission both screens sit behind. Spelt once; app/state/models.py
# defines it and app/auth/policy.py raises at import if a gated code stops
# existing.
MANAGE = "user.manage"

REFUSED = (
    f"{MANAGE} is required to read the share register or the invitation queue. "
    "Ask an administrator. Both screens are records of what other people have "
    "sent out, which is why they sit with the people who manage accounts."
)

# Who decided the tenant switch, in two words beside the value. The value alone
# would be a lie by omission: a missing row is not "off" and not "on", it is
# nobody having decided, and a security switch that renders an unqualified "on"
# for a default has hidden the difference. sharing_enabled() returns the value,
# the source and a sentence; the register shows all three, and the sentence is
# that module's rather than a second description written here.
SWITCH_SOURCE_WORDS = {
    SETTING_SOURCE_DEFAULT: "nobody set this — product default",
    SETTING_SOURCE_TENANT: "set for this company",
}

# Read by the responsive test in tests/test_share_registry.py, which holds these
# two files to the rules tests/test_responsive.py enforces on every other
# template. Their `<style>` blocks are reached by neither of that file's scans.
TEMPLATE_DIR = TEMPLATES_DIR
TEMPLATES_OWNED = ("shares_admin.html", "invites_admin.html")


# ---------------------------------------------------------------------------
# Audited actions
#
# THIS MODULE DECLARES NONE. Every act on these two screens is performed by a
# write layer -- app/state/sharing.py for a revocation, app/state/invites.py for
# a decision on an invitation -- and each of those audits what it did, under a
# code it owns. ACTION_SHARE_REVOKED is imported above rather than spelt again,
# because a row written under a second spelling of an action code is invisible
# to every query for the first: in the log and unfindable, which is worse than
# missing.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The invite write layer, which this module calls and does not reimplement
#
# WHY IT IS REACHED THIS WAY. Approving a queued invitation is not a status
# change. It creates the account the invited person needs -- a User at
# STATUS_INVITED, no roles until acceptance -- checks the invitation has not
# expired, checks the approver is entitled, routes the item that was waiting,
# and audits all of it. Every one of those rules already exists in
# app/state/invites.py beside the same-domain path that writes the same account.
# A copy of any of them here is how two spellings of one rule come to disagree,
# and this rule governs who exists.
#
# So this screen resolves the invitation, checks the reader may administer
# people, and hands the decision to that module. It is imported lazily because
# the module is being written in parallel with this one: an import at the top of
# the file would take the share register down with it, and the share register
# does not need it.
#
# WHEN IT IS ABSENT, THE SCREEN SAYS SO AND CHANGES NOTHING. A fallback must
# announce itself, and the worst possible fallback here is the tempting one --
# flipping the status locally so the button appears to work, leaving an
# invitation marked approved with no account behind it and an item routed
# nowhere. Instead: the banner names the module, the button answers 501 with the
# same sentence, and the list is empty because the list comes from there too.
# The share register, which is a different screen about a different thing, keeps
# working throughout.
# ---------------------------------------------------------------------------

INVITES_MODULE = "app.state.invites"

# The contract, spelt once. tests/test_invites.py fixes these names and this
# call shape; a mismatch is caught at the call and reported rather than raised.
#
# The LIST is reached the same way, and through the same module, so the queue
# and the write layer cannot come to disagree about what a company's
# invitations are. It is the one read of that table this screen does not define
# for itself, and defining a second one here was the alternative -- which is how
# a queue comes to show a row the module that decides has stopped believing in.
APPROVE_FN = "approve_invitation"
REVOKE_FN = "revoke_invitation"
LIST_FN = "invitations_for_company"


def invite_writer(name: str) -> tuple[object | None, str]:
    """The named function from the invite write layer, or None and why not.

    Never raises. Both failures -- no module, no function -- are ordinary states
    of a build being assembled by several hands, and both have to reach the
    screen as a sentence rather than a stack trace.
    """
    import importlib

    try:
        module = importlib.import_module(INVITES_MODULE)
    except ImportError:
        return None, (
            f"{INVITES_MODULE} is not in this build, so nothing here can approve "
            "or refuse an invitation. Approving is not a status change: it "
            "creates the account the invited person needs, and that account is "
            "written in one place. Until that module is wired in, this queue "
            "cannot be read and cannot be decided."
        )
    function = getattr(module, name, None)
    if not callable(function):
        # `callable` and not `is None`: a name bound to something that is not a
        # function would be called anyway and fail somewhere less legible.
        return None, (
            f"{INVITES_MODULE} defines no {name}() this screen can call, so the "
            "queue has nowhere to go. Nothing was read and nothing was changed."
        )
    return function, ""


# ---------------------------------------------------------------------------
# Scoped reads. Every one belongs in app/state/queries.py. company_id is
# keyword-only with no default, which is the shape that file uses, so the move
# is a cut and paste.
# ---------------------------------------------------------------------------


def share_links_for_company(session: Session, *, company_id: str) -> list[ShareLink]:
    """Every share link this company has minted, live and dead alike.

    The whole table for one tenant, unpaginated. That is honest at this scale
    and it is a known limit: a company with ten thousand links gets a slow page,
    and the fix is a page size plus a filter, not a narrower read -- an admin
    who can only see the first fifty cannot answer "what is shared right now".
    """
    _require_scope(company_id)
    return (
        session.query(ShareLink)
        .filter(ShareLink.company_id == company_id)
        .order_by(ShareLink.created_at.desc(), ShareLink.id.desc())
        .all()
    )


def share_link_for_company(
    session: Session, *, company_id: str, share_id: str
) -> ShareLink | None:
    """One link, or None. Another company's id resolves to None, not to a row."""
    _require_scope(company_id)
    if not share_id:
        return None
    return (
        session.query(ShareLink)
        .filter(ShareLink.company_id == company_id)
        .filter(ShareLink.id == share_id)
        .one_or_none()
    )


def opens_for_company(
    session: Session, *, company_id: str
) -> dict[str, list[ShareOpen]]:
    """Every open of every link this company has minted, grouped by link.

    THE JOIN IS THE TENANCY. share_opens carries no company_id -- deliberately,
    because a second copy of the company there would be a column two writers
    could let drift -- so any read of it has to reach the company through the
    link, exactly as passages_for_company reaches it through the version. A
    query over this table with no join is an unscoped read of who opened what.

    One query for the whole page rather than one per row. An id list would have
    been the other way and would meet SQLite's parameter limit on a company with
    a few hundred links.
    """
    _require_scope(company_id)
    rows = (
        session.query(ShareOpen)
        .join(ShareLink, ShareOpen.share_id == ShareLink.id)
        .filter(ShareLink.company_id == company_id)
        .order_by(ShareOpen.share_id, ShareOpen.id)
        .all()
    )
    grouped: dict[str, list[ShareOpen]] = {}
    for row in rows:
        grouped.setdefault(row.share_id, []).append(row)
    return grouped


def invitation_for_company(
    session: Session, *, company_id: str, invitation_id: str
) -> Invitation | None:
    """One invitation, or None. Another company's id resolves to None.

    The only read of that table defined here. The list the queue renders comes
    from invitations_for_company() in app/state/invites.py -- imported, not
    copied, so the screen and the module that decides cannot come to disagree
    about what a company's invitations are. There is no single-id read there
    yet; this one belongs beside it when somebody moves it.
    """
    _require_scope(company_id)
    if not invitation_id:
        return None
    return (
        session.query(Invitation)
        .filter(Invitation.company_id == company_id)
        .filter(Invitation.id == invitation_id)
        .one_or_none()
    )


def _subjects(
    session: Session, company_id: str, links: list[ShareLink]
) -> dict[tuple[str, str], str | None]:
    """What each link points at, resolved to the change screen that shows it.

    The value is the URL, or None when the row is not in this company. None is a
    fact worth rendering: a link whose subject has gone, or was never here,
    still opens for whoever holds the token, and an admin should see that before
    a recipient does.

    Two queries at most, whatever the page holds.
    """
    _require_scope(company_id)
    wanted_claims = {
        link.subject_id for link in links if link.subject_type == SHARE_SUBJECT_CLAIM
    }
    wanted_changes = {
        link.subject_id for link in links if link.subject_type == SHARE_SUBJECT_CHANGE
    }

    urls: dict[tuple[str, str], str | None] = {}
    if wanted_claims:
        rows = (
            session.query(Claim.id, Claim.change_id)
            .filter(Claim.company_id == company_id)
            .filter(Claim.id.in_(sorted(wanted_claims)))
            .all()
        )
        found = {claim_id: change_id for claim_id, change_id in rows}
        for claim_id in wanted_claims:
            change_id = found.get(claim_id)
            urls[(SHARE_SUBJECT_CLAIM, claim_id)] = (
                f"{CHANGE_URL}/{change_id}" if change_id else None
            )
    if wanted_changes:
        rows = (
            session.query(Change.id)
            .filter(Change.company_id == company_id)
            .filter(Change.id.in_(sorted(wanted_changes)))
            .all()
        )
        found_changes = {change_id for (change_id,) in rows}
        for change_id in wanted_changes:
            urls[(SHARE_SUBJECT_CHANGE, change_id)] = (
                f"{CHANGE_URL}/{change_id}" if change_id in found_changes else None
            )
    return urls


def _people(session: Session, company_id: str) -> dict[str, str]:
    """user id -> the address a person reads a year later.

    The address rather than the display name: two people can share a name and
    nobody shares a mailbox in this schema. The registry is read in a security
    review, where "who was this" has to be answerable without a second lookup.
    """
    return {user.id: user.email for user in users_for_company(session, company_id)}


# ---------------------------------------------------------------------------
# What the templates render
# ---------------------------------------------------------------------------

# The three states a link can be in. Derived, never stored: a column saying
# "expired" would be a fact about the clock kept in a row nobody updates.
STATE_LIVE = "live"
STATE_EXPIRED = "expired"
STATE_REVOKED = "revoked"
SHARE_STATES = (STATE_LIVE, STATE_EXPIRED, STATE_REVOKED)

_STATE_BADGE = {
    STATE_LIVE: "badge--active",
    STATE_EXPIRED: "badge--closed",
    STATE_REVOKED: "badge--closed",
}

_INVITE_BADGE = {
    INVITE_AWAITING_APPROVAL: "badge--material",
    INVITE_PENDING: "badge--draft",
    INVITE_APPROVED: "badge--active",
    INVITE_ACCEPTED: "badge--final",
    INVITE_REVOKED: "badge--closed",
    INVITE_EXPIRED: "badge--closed",
}

# The order the invitation queue reads in: what needs a decision, then what is
# waiting on somebody else, then what is settled, then what is dead. A status
# not in this list sorts last rather than vanishing -- a vocabulary that grows
# must not make rows disappear from an admin screen.
_INVITE_ORDER = (
    INVITE_AWAITING_APPROVAL,
    INVITE_PENDING,
    INVITE_APPROVED,
    INVITE_ACCEPTED,
    INVITE_EXPIRED,
    INVITE_REVOKED,
)


@dataclass(frozen=True, slots=True)
class OpenRow:
    """One open of one link, and what the page showed when it happened.

    `verified` is not whether the claim is good today. It is the record of what
    a named recipient was shown at a named moment, which is the question a
    compliance officer asks a year later and the reason share_opens exists.
    """

    opened_at: str
    ip: str
    verified: bool


@dataclass(frozen=True, slots=True)
class ShareRow:
    """One link in the register, with everything an admin needs to judge it."""

    share_id: str
    state: str
    badge: str
    subject_type: str
    subject_id: str
    subject_url: str | None
    subject_missing: bool
    created_by: str
    created_at: str
    expires_at: str
    revoked_at: str | None
    revoked_by: str | None
    opens: tuple[OpenRow, ...]
    open_count: int
    refused_opens: int
    last_open: str | None
    last_open_refused: bool
    counter_disagrees: str | None


@dataclass(frozen=True, slots=True)
class InviteRow:
    """One invitation, and whether the work behind it can reach anybody yet.

    `kind` and `justification` are one fact in two shapes. Every invitation is
    justified and the kind says by what: a handoff names the item somebody is
    waiting on, a provision names the administrator who added the login. A
    queue that showed an empty item column for half its rows would read as a
    gap in the record rather than as the other half of the rule.
    """

    invitation_id: str
    email: str
    status: str
    badge: str
    kind: str
    justification: str
    invited_by: str
    invited_at: str
    expires_at: str
    accepted_at: str | None
    approved_by: str | None
    subject_type: str | None
    subject_id: str | None
    invited_user: str | None
    unrouted: bool
    awaiting: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _state(link: ShareLink, now: datetime) -> str:
    """Revoked, then expired, then live. Order is the decision -- see the head."""
    if link.revoked_at is not None:
        return STATE_REVOKED
    if link.expires_at is not None and link.expires_at <= now:
        return STATE_EXPIRED
    return STATE_LIVE


def _share_rows(session: Session, company_id: str) -> list[ShareRow]:
    """The register, live first, and each link's opens attached to it."""
    links = share_links_for_company(session, company_id=company_id)
    opens = opens_for_company(session, company_id=company_id)
    urls = _subjects(session, company_id, links)
    people = _people(session, company_id)
    now = _now()

    rows: list[ShareRow] = []
    # Sorted on the timestamps themselves, never on the strings the page shows.
    # The current format happens to sort correctly as text and the next one
    # might not, and a register that quietly reordered itself when somebody
    # changed a date format would be a defect nobody could see in the diff.
    ordering: dict[str, tuple[datetime, datetime]] = {}
    floor = datetime.min.replace(tzinfo=timezone.utc)

    for link in links:
        mine = opens.get(link.id, [])
        seen = tuple(
            OpenRow(
                opened_at=_stamp(row.opened_at) or "time not recorded",
                # An empty address is a gap in the caller, not a fact about the
                # visitor, and it must not render as a blank cell that reads
                # like nobody came from anywhere.
                ip=row.ip or "address not recorded",
                verified=bool(row.verified),
            )
            for row in mine
        )
        counted = len(seen)
        last = mine[-1] if mine else None

        # The counter beside the rows, checked against them rather than
        # believed. See the head of this module.
        disagreement = None
        if (link.open_count or 0) != counted:
            disagreement = (
                f"The stored counter on this link says {link.open_count or 0}. "
                f"share_opens holds {counted}. The rows are the record, so the "
                "count shown is theirs, and the writer that moved the counter "
                "out of step is a defect worth finding."
            )

        subject_url = urls.get((link.subject_type, link.subject_id))
        state = _state(link, now)
        ordering[link.id] = (
            last.opened_at if last is not None else floor,
            link.created_at or floor,
        )
        rows.append(
            ShareRow(
                share_id=link.id,
                state=state,
                badge=_STATE_BADGE.get(state, "badge--closed"),
                subject_type=link.subject_type,
                subject_id=link.subject_id,
                subject_url=subject_url,
                subject_missing=subject_url is None,
                created_by=people.get(
                    link.created_by_user_id, link.created_by_user_id or "unknown"
                ),
                created_at=_stamp(link.created_at) or "",
                expires_at=_stamp(link.expires_at) or "",
                revoked_at=_stamp(link.revoked_at),
                revoked_by=(
                    people.get(link.revoked_by_user_id, link.revoked_by_user_id)
                    if link.revoked_by_user_id
                    else None
                ),
                opens=seen,
                open_count=counted,
                refused_opens=sum(1 for row in seen if not row.verified),
                last_open=_stamp(last.opened_at) if last is not None else None,
                last_open_refused=bool(last is not None and not last.verified),
                counter_disagrees=disagreement,
            )
        )

    # Two passes, and Python's sort is stable, which is what makes the pair
    # work: the most recently used first, then the groups put back in order
    # around them. One pass cannot do it -- the state sorts ascending and the
    # dates descending, and a single key would need one of them negated, which
    # a datetime cannot be. "Sorts by last use" is what app/state/models.py
    # says this screen does.
    order = {state: index for index, state in enumerate(SHARE_STATES)}
    rows.sort(key=lambda row: ordering[row.share_id], reverse=True)
    rows.sort(key=lambda row: order.get(row.state, len(SHARE_STATES)))
    return rows


def _invite_rows(session: Session, company_id: str, reader) -> list[InviteRow]:
    """The queue, ordered by what needs a decision.

    `reader` is invitations_for_company() from the invite write layer, passed in
    rather than imported at the top of this file. That module is the one this
    screen hands decisions to, and reaching it by name at the moment of use is
    what keeps the SHARE register -- which has nothing to do with invitations --
    working when it is being edited under us.
    """
    people = _people(session, company_id)
    rows = []
    for invitation in reader(session, company_id):
        # An account that does not exist is somebody the item cannot route to.
        # It is the ordinary state of a queued cross-domain invite and it is a
        # defect once the invitation is live, so the flag is raised on the two
        # statuses that claim an account and carry none.
        unrouted = invitation.invited_user_id is None and invitation.status in (
            INVITE_PENDING,
            INVITE_APPROVED,
        )
        asker = people.get(
            invitation.invited_by_user_id, invitation.invited_by_user_id or "unknown"
        )
        rows.append(
            InviteRow(
                invitation_id=invitation.id,
                email=invitation.email,
                status=invitation.status,
                badge=_INVITE_BADGE.get(invitation.status, "badge--draft"),
                kind=invitation.kind,
                # The justification, in the words that kind of invitation has.
                # An unknown kind says what it is rather than falling into
                # either sentence: a word this screen does not know is not
                # evidence that an item exists or that an admin authorised it.
                justification=(
                    f"waiting on {invitation.subject_type} {invitation.subject_id}"
                    if invitation.kind == INVITE_KIND_HANDOFF
                    else (
                        f"a login added by {asker}, who holds {MANAGE}"
                        if invitation.kind == INVITE_KIND_PROVISION
                        else f"kind {invitation.kind!r}, which this screen has no words for"
                    )
                ),
                invited_by=people.get(
                    invitation.invited_by_user_id,
                    invitation.invited_by_user_id or "unknown",
                ),
                invited_at=_stamp(invitation.invited_at) or "",
                expires_at=_stamp(invitation.expires_at) or "",
                accepted_at=_stamp(invitation.accepted_at),
                approved_by=(
                    people.get(
                        invitation.approved_by_user_id, invitation.approved_by_user_id
                    )
                    if invitation.approved_by_user_id
                    else None
                ),
                subject_type=invitation.subject_type,
                subject_id=invitation.subject_id,
                invited_user=(
                    people.get(invitation.invited_user_id, invitation.invited_user_id)
                    if invitation.invited_user_id
                    else None
                ),
                unrouted=unrouted,
                awaiting=invitation.status == INVITE_AWAITING_APPROVAL,
            )
        )
    order = {status: index for index, status in enumerate(_INVITE_ORDER)}
    rows.sort(key=lambda row: (order.get(row.status, len(_INVITE_ORDER)), row.invited_at))
    return rows


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _user_id(request: Request) -> str:
    principal = current_user(request)
    return principal.user_id if principal is not None else ""


def _actor(request: Request) -> tuple[str, str | None]:
    """The name for the audit row and the id beside it.

    Both, because they answer different questions: the id is the identity and
    the address is what a person reads a year later.
    """
    principal = current_user(request)
    if principal is None:
        return "person:unauthenticated", None
    return principal.email, principal.user_id


def _may_manage(session: Session, company_id: str, user_id: str) -> str | None:
    """None when allowed, else the reason. The refusal is audited on the way out.

    require() writes an audit row and then raises. The raise is caught HERE,
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


def _shell(company_id: str, waiting: int, title: str) -> dict:
    """base.html's context, plus the two URLs both screens link to each other by."""
    context = chrome(
        company_id,
        page_title=title,
        # Neither screen is in the masthead. Marking one of the six as current
        # while an admin is here would be a small lie in an aria-current
        # attribute.
        nav_active="",
        review_count=waiting,
    )
    context.update(
        {
            "shares_url": SHARES_URL,
            "invites_url": INVITES_URL,
            "revoke_url": REVOKE_URL,
            "permission": MANAGE,
            "refused": REFUSED,
        }
    )
    return context


# Which screen each template is, for the browser title. A refused reader still
# gets the name of the page they were refused, rather than the name of the other
# one -- a small lie in a title is still a lie, and it is the sort a person
# reports as "the product is confused about where I am".
_TITLES = {"shares_admin.html": "Share register", "invites_admin.html": "Invitations"}


def _forbidden(
    request: Request, template: str, company_id: str, reason: str, waiting: int
) -> HTMLResponse:
    """One template, two answers. A separate page would describe one screen twice."""
    context = _shell(company_id, waiting, _TITLES.get(template, "Share register"))
    context["reason"] = reason
    return templates.TemplateResponse(request, template, context, status_code=403)


def _unavailable(
    request: Request, company_id: str, blocked: str, waiting: int, rows: list
) -> HTMLResponse:
    """The invite write layer is not there, so the decision has nowhere to go.

    501 and not 200, because the honest answer is that this part of the product
    is not assembled rather than that the administrator did something wrong, and
    a status a proxy or a log can see is worth more than a sentence only a
    person reads. Not 403: nobody was refused. Not 500: nothing failed.

    The queue renders under the message. An administrator who cannot decide can
    still read what is waiting, and an empty page here would say "nobody has
    been invited", which is a different and false statement.
    """
    context = _shell(company_id, waiting, "Invitations")
    context.update(
        {
            "rows": rows,
            "awaiting_count": sum(1 for row in rows if row.awaiting),
            "unrouted_count": sum(1 for row in rows if row.unrouted),
            "blocked": blocked,
        }
    )
    return templates.TemplateResponse(
        request, "invites_admin.html", context, status_code=501
    )


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


@router.get(SHARES_URL, response_class=HTMLResponse)
def share_register(
    request: Request,
    revoked: int = 0,
    already: int = 0,
    unknown: int = 0,
) -> HTMLResponse:
    """Everything this company has shared, live first, with every open under it.

    The three counts arrive on the redirect from a revoke and are display only.
    A forged query string changes a sentence at the top of the page; the table
    below it is read from the rows and is the answer.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, rows, waiting, switch = None, [], 0, None

    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, user_id)
            if reason is None:
                rows = _share_rows(session, company_id)
                # The one function every sharing path asks, asked here too so
                # the register reports the same answer minting and opening get.
                switch = sharing_enabled(session, company_id=company_id)
            context = _shell(company_id, waiting, "Share register")
    except SQLAlchemyError:
        # A database that cannot be read is a permission check that did not run,
        # and a check that did not run is not a check that passed. On a clone
        # with no tables this screen would otherwise open for anybody, empty --
        # which shows no data and is still the gate failing open.
        reason = (
            "this company's roles could not be read, so nothing confirmed that "
            f"you hold {MANAGE}"
        )
        context = _shell(company_id, 0, "Share register")

    if reason is not None:
        return _forbidden(request, "shares_admin.html", company_id, reason, waiting)

    live = [row for row in rows if row.state == STATE_LIVE]
    context.update(
        {
            "rows": rows,
            "live_count": len(live),
            "refused_links": sum(1 for row in rows if row.refused_opens),
            # The value, and where it came from. Both, always. An interface that
            # showed only the value would render a default the same way it
            # renders a decision, and those are different facts about a control.
            # An unrecognised source shows the raw word rather than nothing: a
            # source this screen has no phrase for is still information.
            "switch_on": bool(switch) if switch is not None else None,
            "switch_note": switch.note if switch is not None else "",
            "switch_source": (
                SWITCH_SOURCE_WORDS.get(switch.source, switch.source)
                if switch is not None
                else ""
            ),
            "states": SHARE_STATES,
            "outcome": {
                "revoked": max(revoked, 0),
                "already": max(already, 0),
                "unknown": max(unknown, 0),
            },
        }
    )
    return templates.TemplateResponse(request, "shares_admin.html", context)


@router.post(REVOKE_URL)
def revoke_shares(
    request: Request,
    share_ids: list[str] = Form(default=[]),
    only: str = Form(default=""),
):
    """Stop one link, or every link named, immediately. Each one audited on its own.

    `only` is the per-row button and it wins over the checkboxes. One form
    cannot nest another, so the row button lives inside the bulk form, and
    without this a click on row three while row one happened to be ticked would
    revoke both -- a control that does more than the person asked for is not a
    control they will trust.

    An empty post revokes nothing. A body that names no link is a client bug or
    a retry that lost its form, not an instruction to stop everything.

    THE REVOCATION ITSELF IS NOT PERFORMED HERE. revoke_share_link() in
    app/state/sharing.py does it, and it is the same function the sharer's own
    revoke button calls. Bulk is a loop around one act, never a second
    implementation of it -- a "revoke many" that wrote the rows itself would be
    a second answer to who may stop a link and what the log says when they do.

    The counts are read BEFORE the call, because the write layer answers a
    second revocation with the row unchanged and no audit entry, which is the
    right behaviour and is indistinguishable afterwards from a fresh one.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    wanted = [only] if only else [value for value in share_ids if value]

    revoked = already = unknown = 0
    reason, waiting = None, 0
    with session_scope() as session:
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            now = _now()
            # Deduplicated, so a form that posted an id twice is one decision.
            for share_id in dict.fromkeys(wanted):
                link = share_link_for_company(
                    session, company_id=company_id, share_id=share_id
                )
                if link is None:
                    # Another company's id and an id that was never issued are
                    # the same answer here, deliberately: telling them apart
                    # would tell this admin something about another tenant.
                    unknown += 1
                    continue
                if link.revoked_at is not None:
                    already += 1
                    continue
                try:
                    revoke_share_link(
                        session,
                        company_id=company_id,
                        share_id=link.id,
                        user_id=user_id,
                        now=now,
                    )
                except policy.PermissionDenied as denied:
                    # Unreachable through this screen, which has already
                    # required user.manage -- and caught rather than assumed
                    # unreachable, because the day the two rules disagree the
                    # honest answer is the refusal, not a 500. Links already
                    # revoked in this loop stay revoked and are reported.
                    reason = denied.reason
                    break
                except ValueError:
                    # The link went between the read above and the write. The
                    # tenant answer is the same as for an id nobody issued.
                    unknown += 1
                    continue
                revoked += 1

    if reason is not None:
        return _forbidden(request, "shares_admin.html", company_id, reason, waiting)

    return RedirectResponse(
        url=f"{SHARES_URL}?revoked={revoked}&already={already}&unknown={unknown}",
        status_code=303,
    )


@router.get(INVITES_URL, response_class=HTMLResponse)
def invitation_queue(
    request: Request, decided: str = "", code: str = ""
) -> HTMLResponse:
    """Every invitation, with the external-domain queue at the top of it.

    The banner saying whether this queue can be decided is worked out on the
    read, not only on the click. A button that cannot do anything must say so
    before somebody presses it -- and it stays pressable, because a greyed
    control with no explanation is the thing this codebase refuses to ship.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, rows, waiting = None, [], 0
    reader, blocked = invite_writer(LIST_FN)

    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, user_id)
            if reason is None and reader is not None:
                rows = _invite_rows(session, company_id, reader)
            context = _shell(company_id, waiting, "Invitations")
    except SQLAlchemyError:
        reason = (
            "this company's roles could not be read, so nothing confirmed that "
            f"you hold {MANAGE}"
        )
        context = _shell(company_id, 0, "Invitations")

    if reason is not None:
        return _forbidden(request, "invites_admin.html", company_id, reason, waiting)

    context.update(
        {
            "rows": rows,
            "awaiting_count": sum(1 for row in rows if row.awaiting),
            "unrouted_count": sum(1 for row in rows if row.unrouted),
            "decided": decided,
            # Both arrive on the redirect from a decision and are display only.
            # A forged query string changes a sentence; the table under it is
            # read from the rows and is the answer.
            "code": code,
            "blocked": blocked,
        }
    )
    return templates.TemplateResponse(request, "invites_admin.html", context)


def _decide(
    request: Request,
    invitation_id: str,
    *,
    function: str,
    actor_field: str,
) -> RedirectResponse | HTMLResponse:
    """Hand one decision on one queued invitation to the invite write layer.

    Three gates before the hand-off, in this order. The reader holds user.manage
    -- refused, and audited, if not. The invitation is in THIS company -- 404 if
    not, and another tenant's id is indistinguishable from one never issued.
    The write layer exists -- 501 with the sentence naming it if not.

    What is NOT checked here, on purpose: whether the invitation is queued,
    whether it has expired, whether this approver is entitled to release this
    particular invitation, and what happens to the item that was waiting. Every
    one of those is a rule in app/state/invites.py, checked there for the
    same-domain path as well, and a copy in this view would be a second answer
    to a question that already has one.

    THE ROW IS THE OUTCOME, NOT THE RETURN VALUE. After the call the invitation
    is read again and its status is what the screen reports. That holds however
    the write layer chooses to shape its result, and it cannot say a decision
    landed when the row disagrees.

    A REFUSAL IS RENDERED, NOT REDIRECTED. It carries the write layer's own
    sentence -- why this invitation is not a routing gap, why it has expired --
    and that is a paragraph, not something to push through a query string. The
    same choice app/web/views/admin.py makes for a graph that fails validation.
    A decision that landed does redirect, so a reload cannot post it twice.
    """
    company_id = current_company_id()
    user_id = _user_id(request)
    reason, waiting, rows = None, 0, []
    found, before, after, code, text = False, "", "", "", ""

    writer, blocked = invite_writer(function)

    with session_scope() as session:
        waiting = _waiting(session, company_id)
        reason = _may_manage(session, company_id, user_id)
        if reason is None:
            invitation = invitation_for_company(
                session, company_id=company_id, invitation_id=invitation_id
            )
            found = invitation is not None
            if invitation is not None and writer is not None:
                who, who_id = _actor(request)
                before = invitation.status
                try:
                    outcome = writer(
                        session,
                        company_id,
                        invitation_id=invitation.id,
                        actor=who,
                        now=_now(),
                        **{actor_field: who_id},
                    )
                except (TypeError, ValueError, AttributeError) as mismatch:
                    # The contract moved under us. Announced, not raised: a
                    # 500 on an admin screen says nothing about which of the
                    # two sides changed, and this one is checkable.
                    session.rollback()
                    blocked = (
                        f"{INVITES_MODULE}.{function}() did not accept the call "
                        f"this screen makes: {mismatch}. Nothing was changed. "
                        "The two sides of that contract have to be brought back "
                        "together before this button can work."
                    )
                else:
                    code = str(getattr(outcome, "reason_code", "") or "")
                    text = str(getattr(outcome, "reason_text", "") or "")
                    session.flush()
                    session.refresh(invitation)
                    after = invitation.status
            reader, _ = invite_writer(LIST_FN)
            if found and reader is not None and (blocked or after == before):
                # Read for the page under the message, and only then: an
                # administrator whose decision was refused can still read the
                # queue. When the module itself is unreachable the list is not
                # readable either, and the banner is the whole of the page --
                # an empty table there would say "nobody has been invited",
                # which is a different statement and a false one.
                rows = _invite_rows(session, company_id, reader)

    if reason is not None:
        return _forbidden(request, "invites_admin.html", company_id, reason, waiting)
    if not found:
        raise HTTPException(status_code=404, detail="no such invitation")
    if blocked:
        return _unavailable(request, company_id, blocked, waiting, rows)

    # A click that changed nothing must not read as one that did.
    if after and after != before:
        return RedirectResponse(
            url=f"{INVITES_URL}?decided=yes&code={code}", status_code=303
        )

    context = _shell(company_id, waiting, "Invitations")
    context.update(
        {
            "rows": rows,
            "awaiting_count": sum(1 for row in rows if row.awaiting),
            "unrouted_count": sum(1 for row in rows if row.unrouted),
            "decided": "refused",
            "code": code,
            # The write layer's own words, or its code where it has no words.
            # Rewriting its sentence here would be this screen explaining a
            # decision it did not make.
            "refusal_text": text,
            "blocked": "",
        }
    )
    return templates.TemplateResponse(request, "invites_admin.html", context)


@router.post(INVITES_URL + "/{invitation_id}/approve")
def approve_invitation(request: Request, invitation_id: str):
    """Release a queued cross-domain invitation, through the write layer.

    approver_user_id, not the caller's word for who they are. The question asked
    of that column afterwards is whether that account was entitled to release
    this invitation, and a display string cannot answer it.
    """
    return _decide(
        request, invitation_id, function=APPROVE_FN, actor_field="approver_user_id"
    )


@router.post(INVITES_URL + "/{invitation_id}/reject")
def reject_invitation(request: Request, invitation_id: str):
    """Refuse a queued cross-domain invitation, through the same write layer.

    Rejecting from this queue and a sharer withdrawing their own invitation are
    the same act on the row -- the vocabulary's word for both is `revoked`, and
    there is no separate word for refused-by-an-administrator. So it is the same
    function, which also suspends any account that invitation created and frees
    the item that was waiting. Telling the two apart afterwards is a job for the
    audit chain, where the actor is recorded: whoever adds a distinct action
    code for an administrator's refusal should add it in app/state/audit.py and
    have the write layer choose it, not have this screen write a second row.
    """
    return _decide(
        request, invitation_id, function=REVOKE_FN, actor_field="revoked_by_user_id"
    )


# ---------------------------------------------------------------------------
# HANDOFF -- what this module needs from files it does not own
#
# app/main.py           DONE by whoever owns that file: shares_admin is imported
#                       and its router included. Nothing further is needed. The
#                       line matters because tests/test_app_wiring.py derives
#                       its answer from the views package rather than from a
#                       hand-written list, so a router nobody mounted fails
#                       there rather than 404ing in front of a reviewer.
#
# app/web/templates/base.html
#                       Nothing is required. Neither screen is in the six-item
#                       masthead, because both are read by almost nobody and
#                       base.html's own note says a nav item nobody can reach
#                       renders as dead text. If an admin menu is added later,
#                       these two belong in it beside /admin/workflows.
#
# app/state/audit.py    Nothing. ACTION_SHARE_REVOKED is imported from
#                       app/state/sharing.py, which writes it. It belongs in
#                       audit.py with the rest of the vocabulary, and that is
#                       that module's handoff rather than this one's.
#
# app/state/queries.py  Move the four scoped reads above, unchanged. Two of them
#                       -- share_links_for_company and opens_for_company -- read
#                       the page rather than one link, which is why they are not
#                       the per-link reads app/state/sharing.py already has.
#
# app/state/invites.py  This screen calls approve_invitation() and
#                       revoke_invitation() with the call shape
#                       tests/test_invites.py fixes:
#                         f(session, company_id, invitation_id=..., actor=...,
#                           now=..., approver_user_id=... | revoked_by_user_id=...)
#                       and lists the queue with invitations_for_company(). A
#                       mismatch is caught and reported on the page rather than
#                       raised, so the failure is visible and not a 500 -- but
#                       it is still a failure, and the two sides have to agree.
#                       A single-id read belongs there beside them; this file
#                       defines the only one that exists.
#
# app/state/identity.py user.invite is in PERMISSION_CODES with no description
#                       and no role holding it, so nobody can invite anybody
#                       and nothing fills this queue. PERMISSION_DESCRIPTIONS
#                       and SYSTEM_ROLE_PERMISSIONS both need it, and which role
#                       holds it is the product decision in the brief: an
#                       analyst may pull in a colleague on the company's own
#                       domain, and anything else waits for user.manage.
# ---------------------------------------------------------------------------


__all__ = [
    # Re-exported, not declared: app/state/sharing.py owns this code and writes
    # the rows under it. Named here so a reader of this screen can see which
    # action its revoke button lands in the chain.
    "ACTION_SHARE_REVOKED",
    "APPROVE_FN",
    "INVITES_MODULE",
    "INVITES_URL",
    "LIST_FN",
    "MANAGE",
    "REFUSED",
    "REVOKE_FN",
    "REVOKE_URL",
    "SHARES_URL",
    "SHARE_STATES",
    "STATE_EXPIRED",
    "STATE_LIVE",
    "STATE_REVOKED",
    "SWITCH_SOURCE_WORDS",
    "TEMPLATES_OWNED",
    "TEMPLATE_DIR",
    "InviteRow",
    "OpenRow",
    "ShareRow",
    "invitation_for_company",
    "invite_writer",
    "opens_for_company",
    "revoke_share_link",
    "router",
    "share_link_for_company",
    "share_links_for_company",
    "templates",
]
