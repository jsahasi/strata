"""Two ways somebody gets an account here, and the reason for each one.

TWO KINDS, ONE LIFECYCLE. A HANDOFF pulls a colleague onto the item that is
waiting for them; routing found a person with no account and stopped, and the
invitation names the work. A PROVISION is an admin adding a login: no item, and
the justification is the admin, who must hold user.manage and whose id is on the
row. models.py carries the argument for why one control became two rules rather
than being deleted. Everything below the split -- the token scheme, the hashing,
acceptance, revocation, the one sentence a dead link gets -- is shared, because
two lifecycles for one kind of secret is two sets of expiry rules to get right.

THE TWO CLOCKS ARE DIFFERENT ON PURPOSE. A handoff gets seven days: it may sit
until somebody notices the item, and shortening it would push the escalation
back to the shared queue overnight. A provision gets twenty-four hours, and no
caller can lengthen it, because an admin provisioning a login has a named person
waiting who was told to expect it, and a credential-setting link is the most
valuable thing in this product to steal.

A RESEND IS A NEW INVITATION, NEVER A LONGER ONE. resend() mints a new token and
moves the old row to INVITE_SUPERSEDED, so at most one token is ever live for a
person. Pushing expires_at forward on the standing row would keep whatever
copies of the first link exist -- in an inbox, a helpdesk ticket, a mail archive
-- working for another day, every time anybody pressed the button.

AN INVITER MAY NEVER GRANT MORE THAN THEY HOLD -- ON THE PROVISIONING PATH,
WHICH IS THE ONLY PATH THAT TAKES A ROLE. Provisioning names a role, and without
a ceiling it would be the shortest path in the product from user.manage to any
authority at all: the admin role deliberately holds no approval permission, so an
admin who could provision an obligation_owner and accept it could approve.
_grant_ceiling refuses that and names the codes. THE COST IS REAL AND IS NOT
HIDDEN: an ordinary admin can provision another admin and nothing else, because
admin holds none of the analyst or approver codes. grantable_roles exists so a
screen offers what will work instead of a list that refuses.

THE HANDOFF PATH GRANTS MORE THAN ITS INVITER HOLDS, AND SAYS SO EVERY TIME.
Accepting a handoff grants ROLE_OBLIGATION_OWNER, and admin, the only stock role
carrying user.invite, holds neither action.approve nor action.reject. So the one
kind of account that can invite always grants strictly more than it holds here.
That is the feature rather than a leak in it: this module exists to create the
approver for a duty that has none, and a ceiling that refused it would mean no
company could ever create one.

NO GRANT ESCAPES THE CEILING UNSEEN, WHICH IS A DIFFERENT AND KEEPABLE CLAIM.
_grant_within_ceiling is the only function in this module that calls
identity.grant_role -- a test reads the module and fails if that stops being
true. It measures every grant against whoever released the invitation, lets a
path past only where CEILING_WAIVED_BY_KIND declares it in writing, and writes
invite.ceiling_waived into the chain naming the codes and the person each time
it does. The excess also comes back on AcceptOutcome, so the screen shows it
rather than the audit chain holding it alone. THE SENTENCE ABOVE ONCE READ AS AN
UNQUALIFIED RULE OVER BOTH PATHS WHILE _grant_ceiling HAD ONE CALLER, WHICH IS
WHY THE RULE IS NOW A DOOR RATHER THAN A PARAGRAPH: two callers asked to
remember one rule is how the claim and the code came apart, and a third path
added next month would have missed it the same way.

WHAT THIS CLOSES. app/state/routing.py walks an escalation to the claim, the
claim to the change, the change to the obligation and the obligation to its
owner, and refuses correctly at every hop that breaks. One of those refusals was
never really a dead end: the owner is a real person named in
data/company_context.json who has no account here. The item sat in the shared
queue for ever and nothing anybody could do inside the product changed that.
This module creates the account, hands the item over, and says out loud that it
has not landed yet.

THE ADDRESS IS NEVER INVENTED, AND THAT IS THE FIRST RULE HERE.
data/company_context.json carries owner_name and owner_title for every duty and
no email address at all -- grep it. app/seed.py builds first.last@mep.example
so the demo has logins; doing the same for a colleague you intend to reach is
the same guess as a citation whose quote nobody checked, and on this project an
address inferred from a name pattern bounced this morning. So the address comes
from the person inviting, and its absence is a refusal (INV_NO_EMAIL) rather
than a pattern applied to a name.

AN INVITE IS NOT A PRIVILEGE-ESCALATION PATH. Acceptance grants
ROLE_OBLIGATION_OWNER and nothing else -- not what the inviter holds, not
anything the caller asks for, because nothing here takes a role as an argument
and Invitation has no column to put one in. That is one rule in one function
(accept_invitation) with tests pointed straight at it.

The narrow role is still MORE than any inviter holds: obligation_owner carries
action.approve and no role that can invite carries it. THE ESCALATION THIS OPENS,
WITH THE RIGHT ACTOR NAMED. It is an ADMINISTRATOR and not an analyst -- this
paragraph said analyst until 2026-08-04, and an analyst cannot reach _invite at
all, because the gate is user.invite or user.manage and user.invite sits on admin
and nowhere else. An administrator invites an address they control at their own
company's domain, accepts it, and holds action.approve. Two things stand against
it and neither is strong. Same-domain invites go direct only for an address that
is not the inviter's own under a plus-tag comparison, which catches a plus-tag on
one mailbox and does not catch a second mailbox (InviteOutcome.self_invite) --
and an administrator controls their own domain. And separation of duties is
enforced where the approval happens, not here: this module hands somebody a role,
it does not decide whether they may approve a particular action. Both halves are
needed; neither is sufficient. Said plainly rather than implied, because the
second half lives in another file.

This is the same hole app/state/identity.py names on the grid beside user.invite,
and the one ADR-64 concedes for grant_role, where an administrator can already
grant themselves obligation_owner outright. Closing it needs a second approver on
a privilege change, which does not exist in this build. This module is a second
route to that hole and opens no authority the hole did not already offer.

THE SWITCHES ARE ANSWERED IN ONE PLACE AND ANNOUNCE THEMSELVES. There is no
companies table and no settings table (see the note at the foot of
app/state/models.py), so "does this tenant allow invites" and "does this tenant
force approval" are read from the environment and reported with their source
attached. A missing value is not off and not on, it is nobody having decided,
and a security switch that silently defaults to permissive is the worst instance
of a fallback that does not announce itself.

WHAT THIS MODULE DOES NOT DO. It sends no mail. It returns the token once, to
the caller, and whoever holds that caller has to deliver it -- so an invitation
nobody delivers looks exactly like an invitation nobody accepted, and the
product cannot tell the two apart. It does not prove an address exists; only
sending to it does. And the domain rule below decides who is "the same
organisation" from the accounts a tenant already has, which is a derivation and
not a fact the schema holds.
"""

import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

# The MODULE, not the names on it, for the reason app/state/sharing.py gives at
# length: tests/test_policy.py reloads app.auth.policy to prove the approval
# mode is read at import, and a reload rebinds every class in it. A
# `from ... import PermissionDenied` here would hold the class from before the
# reload while the raise inside policy.require used the one from after, and
# `except PermissionDenied` would quietly stop catching a refusal.
from app.auth import policy
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTOR_SYSTEM,
    ACTOR_USER,
    record_event,
)
from app.state.identity import (
    create_user,
    grant_role,
    hash_password,
    hash_session_token,
    new_session_token,
    normalise_email,
    permissions_for_role,
    permissions_for_user,
    role_for_company,
    roles_for_company,
    session_token_matches,
    set_user_status,
    user_by_email,
    user_for_company,
    user_has_permission,
    users_for_company,
)
from app.state.models import (
    INVITATION_STATUSES,
    INVITE_ACCEPTED,
    INVITE_APPROVED,
    INVITE_AWAITING_APPROVAL,
    INVITE_DEFAULT_TTL_DAYS,
    INVITE_EXPIRED,
    INVITE_KIND_HANDOFF,
    INVITE_KIND_PROVISION,
    INVITE_KINDS,
    INVITE_MAX_TTL_DAYS,
    INVITE_PENDING,
    INVITE_PROVISION_TTL_HOURS,
    INVITE_REVOKED,
    INVITE_SUBJECT_ESCALATION,
    INVITE_SUBJECT_OBLIGATION,
    INVITE_SUBJECT_TYPES,
    INVITE_SUPERSEDED,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    Escalation,
    Invitation,
    Obligation,
    Role,
    User,
    UserRole,
)
from app.state.queries import _require_scope
from app.state.routing import (
    ROUTE_OBLIGATION_UNOWNED,
    invitation_for_invited_user,
    invitation_is_live,
    obligation_for_company,
    resolve_escalation_owner,
    route_escalation,
    set_obligation_owner,
    shared_queue,
    unroute_escalation,
)

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE BELONG IN app/state/audit.py BESIDE THE REST OF THE VOCABULARY AND MUST
# BE MOVED THERE RATHER THAN RESTATED. They sit here for the reason
# app/state/routing.py and app/chat/tools.py give for the same thing: that file
# is owned by another agent in this build and a parallel edit would lose their
# work. When they move, this block is deleted and the import changes; nothing
# else does. It is in the handoff list.
#
# ACTION_USER_CREATED, ACTION_USER_SUSPENDED, ACTION_ROLE_GRANTED and
# ACTION_ACCESS_DENIED are NOT restated: identity.py already writes the first
# three through create_user, set_user_status and grant_role, and the fourth is
# imported from audit.py above. A second spelling of a string another module
# owns is the drift these constants exist to stop.
# ---------------------------------------------------------------------------

# Same domain, nobody had to approve it, the account exists at STATUS_INVITED.
ACTION_INVITE_CREATED = "invite.created"
# Another domain, or a tenant that forces approval: the row is written, NO
# account exists, and the item stays visibly unrouted. A separate code from the
# one above because "who let an outsider in" and "who did nobody have to ask
# about" are different questions, and the answer to the first is a name.
ACTION_INVITE_QUEUED = "invite.queued"
ACTION_INVITE_APPROVED = "invite.approved"
ACTION_INVITE_ACCEPTED = "invite.accepted"
ACTION_INVITE_REVOKED = "invite.revoked"
# The product looked at an invitation somebody asked for and would not write
# one. A refusal is a decision, and a decision nobody can see afterwards is a
# decision nobody can dispute -- routing.py makes the same argument for
# escalation.unrouted, and this is that argument applied to people.
ACTION_INVITE_REFUSED = "invite.refused"
# A later invitation replaced this one and its token stopped working. A code of
# its own rather than invite.revoked, because pressing resend is a normal
# Tuesday and revoked is an accusation -- filing every resend under it would put
# "somebody withdrew this" in the log each time an admin helped a colleague who
# lost the mail. It is also not invite.refused: something was written.
ACTION_INVITE_SUPERSEDED = "invite.superseded"
# An account was granted a role carrying authority the person who released the
# invitation does not hold themselves. A code of its own rather than folding it
# into user.role_granted, and the argument is audit.py's own for approval.waived:
# "somebody was given a role" and "somebody handed on authority they do not have"
# are different questions, and reading the second as the first would make a
# manufactured approver look like routine account admin a year later. That is the
# one reading a log must never allow. The row names the codes and the person, so
# the question "who here approves through an account an administrator made" is
# one query rather than a reconstruction.
ACTION_INVITE_CEILING_WAIVED = "invite.ceiling_waived"

INVITE_ACTIONS = (
    ACTION_INVITE_CREATED,
    ACTION_INVITE_QUEUED,
    ACTION_INVITE_APPROVED,
    ACTION_INVITE_ACCEPTED,
    ACTION_INVITE_REVOKED,
    ACTION_INVITE_REFUSED,
    ACTION_INVITE_SUPERSEDED,
    ACTION_INVITE_CEILING_WAIVED,
)


# ---------------------------------------------------------------------------
# Reason codes
#
# One code per fix, the rule app/state/routing.py sets out: "could not invite"
# collapses a dozen different fixes into one line nobody can act on. The prefix
# is INV_ rather than INVITE_ because models.py already owns INVITE_PENDING and
# its five siblings as STATUS values, and one prefix meaning two things is how a
# status ends up compared against a reason code.
# ---------------------------------------------------------------------------

#: Written, live, the account exists and the work is on their desk.
INV_OK = "INV_OK"
#: Written and waiting for a holder of user.manage. No account, item unrouted.
INV_QUEUED = "INV_QUEUED"
#: The invitation was withdrawn and the account it made, if any, suspended.
INV_REVOKED_OK = "INV_REVOKED_OK"

#: Invites are switched off for this tenant.
INV_DISABLED = "INV_DISABLED"
#: The account said to be inviting is not an active account in this company.
INV_NO_INVITER = "INV_NO_INVITER"
#: The inviter holds neither user.invite nor user.manage; the approver, at
#: approval time, does not hold user.manage.
INV_NOT_PERMITTED = "INV_NOT_PERMITTED"
#: No address was supplied. NEVER derived from the name. See the module head.
INV_NO_EMAIL = "INV_NO_EMAIL"
#: An address nothing can read. Refused rather than repaired.
INV_EMAIL_MALFORMED = "INV_EMAIL_MALFORMED"
#: No display name. An account nobody can name in a review is not attributable,
#: and a name read off the local part of an address is the same guess again.
INV_NO_NAME = "INV_NO_NAME"
#: The escalation or obligation named does not exist for this company.
INV_NO_SUBJECT = "INV_NO_SUBJECT"
#: The item's problem is not a missing owner account. route_reason_code on the
#: outcome says which problem it is, so a screen can offer the right fix.
INV_NOT_A_GAP = "INV_NOT_A_GAP"
#: Two or more unowned obligations. Which duty would this person own? Nothing
#: here can say, so nobody is invited.
INV_OBLIGATIONS_AMBIGUOUS = "INV_OBLIGATIONS_AMBIGUOUS"
#: That address already has an account here. Give them the duty; do not make a
#: second person out of one.
INV_ALREADY_A_USER = "INV_ALREADY_A_USER"
#: A live invitation for that address is already out.
INV_ALREADY_LIVE = "INV_ALREADY_LIVE"
#: Somebody withdrew an invitation to this address. Re-inviting would undo that
#: decision quietly, so the way back is a holder of user.manage.
INV_WAS_REVOKED = "INV_WAS_REVOKED"
#: Approval was asked for something that is not waiting for approval.
INV_NOT_QUEUED = "INV_NOT_QUEUED"
#: Too late. An expired invitation cannot be approved into life.
INV_EXPIRED = "INV_EXPIRED"
#: No such invitation for this company. Another tenant's id lands here.
INV_UNKNOWN = "INV_UNKNOWN"
#: Already accepted. Withdrawing an account is a suspension, a different
#: decision with a different audit code.
INV_ALREADY_ACCEPTED = "INV_ALREADY_ACCEPTED"
#: No role was named, or the name is not a role this company can grant. NEVER
#: defaulted to the narrowest role: a caller who did not say what this login is
#: for has not decided, and picking for them writes a decision nobody made.
INV_NO_ROLE = "INV_NO_ROLE"
#: The role asked for carries permissions the person provisioning does not
#: hold. The text names the codes. See _grant_ceiling.
INV_GRANT_EXCEEDS = "INV_GRANT_EXCEEDS"
#: A resend was asked for on a handoff invitation. Different fix, named in the
#: text, because a handoff's item has to be re-checked before anybody is
#: re-admitted to it.
INV_NOT_PROVISION = "INV_NOT_PROVISION"
INV_REASON_CODES = (
    INV_OK,
    INV_QUEUED,
    INV_REVOKED_OK,
    INV_DISABLED,
    INV_NO_INVITER,
    INV_NOT_PERMITTED,
    INV_NO_EMAIL,
    INV_EMAIL_MALFORMED,
    INV_NO_NAME,
    INV_NO_SUBJECT,
    INV_NOT_A_GAP,
    INV_OBLIGATIONS_AMBIGUOUS,
    INV_ALREADY_A_USER,
    INV_ALREADY_LIVE,
    INV_WAS_REVOKED,
    INV_NOT_QUEUED,
    INV_EXPIRED,
    INV_UNKNOWN,
    INV_ALREADY_ACCEPTED,
    INV_NO_ROLE,
    INV_GRANT_EXCEEDS,
    INV_NOT_PROVISION,
)

# Acceptance has its own, deliberately tiny, vocabulary. See ACCEPT_REFUSED.
ACCEPT_OK = "ACCEPT_OK"
ACCEPT_REFUSED = "ACCEPT_REFUSED"
ACCEPT_PASSWORD = "ACCEPT_PASSWORD"
ACCEPT_REASON_CODES = (ACCEPT_OK, ACCEPT_REFUSED, ACCEPT_PASSWORD)

# THE ONE SENTENCE A DEAD INVITATION GETS. Unknown, expired, revoked, already
# accepted and not yet approved all end here. Five sentences would be a probe: a
# caller holding a guess could learn which tokens are real, and a caller holding
# a withdrawn link could learn it was withdrawn rather than never issued. The
# real reason goes in the audit row, where the company is known and the person
# reading it is entitled to it.
ACCEPT_UNAVAILABLE = (
    "This invitation is not available. Invitations expire, and whoever sent this "
    "one can withdraw it at any time. Ask them for a new one."
)


# ---------------------------------------------------------------------------
# The two tenant switches
#
# No companies table and no settings table, so these are read from the
# environment and are GLOBAL rather than per company -- the same shape, and the
# same known gap, as the ADR-006 confidence threshold in app/state/claims.py.
# Read at call time rather than at import, so flipping one takes effect without
# a restart and a test can prove the refusal.
#
# Switch mirrors app/state/sharing.py::Setting, which another agent wrote in
# this same build for the sharing half of the feature. The two should become one
# type on the day company_settings lands, which is the day both stop reading the
# environment; the source strings are already spelt the same so that merge is a
# deletion rather than a translation.
# ---------------------------------------------------------------------------

SETTING_INVITES_ENABLED = "invites.enabled"
SETTING_INVITES_NEED_APPROVAL = "invites.need_approval"

ENV_INVITES_ENABLED = "STRATA_INVITES_ENABLED"
ENV_INVITES_NEED_APPROVAL = "STRATA_INVITES_NEED_APPROVAL"

SOURCE_DEFAULT = "default"
SOURCE_ENVIRONMENT = "environment"
SOURCE_TENANT = "tenant"

# What a tenant nobody has asked gets. Invites on, approval not forced: a
# product that cannot pull in the person who has to approve the work is not this
# product. Both are permissive, so both say out loud that nobody decided them.
INVITES_ENABLED_BY_DEFAULT = True
INVITES_NEED_APPROVAL_BY_DEFAULT = False

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class Switch:
    """A switch, its value, and where that value came from.

    The value alone is not enough, which is the whole reason this is a type
    rather than a bool. A switch nobody has set is not off and not on; it is
    undecided, and an interface that renders an unqualified "on" for a default
    has hidden the difference. `decided` is what a screen branches on to say
    "on by default, nobody has set it" rather than "on".
    """

    key: str
    value: bool
    source: str
    scope: str
    note: str

    def __bool__(self) -> bool:
        return self.value

    @property
    def decided(self) -> bool:
        return self.source != SOURCE_DEFAULT

    @property
    def announcement(self) -> str:
        state = "on" if self.value else "off"
        if not self.decided:
            return (
                f"{self.key} is {state} by default. Nobody has set it: there is "
                "no settings table yet, so this is the product's default rather "
                "than a decision anybody made."
            )
        return f"{self.key} is {state}, set in the {self.source} and applying {self.scope}."


@dataclass(frozen=True, slots=True)
class InvitePolicy:
    """Both switches, answered together, for one company."""

    company_id: str
    enabled: Switch
    need_approval: Switch


def _switch(key: str, variable: str, default: bool) -> Switch:
    raw = os.environ.get(variable)
    if raw is None or raw.strip() == "":
        return Switch(
            key=key,
            value=default,
            source=SOURCE_DEFAULT,
            scope="every tenant",
            note=f"no {variable} in the environment and no settings table",
        )
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        value = True
    elif word in _FALSE_WORDS:
        value = False
    else:
        # Raises rather than falling back to the default. A switch nobody can
        # read is a switch nobody set, and taking the permissive answer for it
        # would be the fallback that does not announce itself, on the one class
        # of setting where that failure is worst.
        raise ValueError(
            f"{variable}={raw!r} is not a switch value. Use one of "
            f"{sorted(_TRUE_WORDS)} or {sorted(_FALSE_WORDS)}. Refusing to "
            "guess which was meant on a setting that decides who gets an account."
        )
    return Switch(
        key=key,
        value=value,
        source=SOURCE_ENVIRONMENT,
        scope="every tenant, because there is nowhere to hold it per company",
        note=f"{variable}={raw.strip()!r}",
    )


def invite_policy(session: Session, *, company_id: str) -> InvitePolicy:
    """The two switches for one company. THE ONLY PLACE EITHER IS DECIDED.

    Every path below asks this rather than testing an environment variable of
    its own, so the day company_settings lands there is one function to change
    and no literal to hunt for. The session argument is unused today and is in
    the signature for the same reason: when the row exists, this reads it.
    """
    _require_scope(company_id)
    return InvitePolicy(
        company_id=company_id,
        enabled=_switch(
            SETTING_INVITES_ENABLED, ENV_INVITES_ENABLED, INVITES_ENABLED_BY_DEFAULT
        ),
        need_approval=_switch(
            SETTING_INVITES_NEED_APPROVAL,
            ENV_INVITES_NEED_APPROVAL,
            INVITES_NEED_APPROVAL_BY_DEFAULT,
        ),
    )


# ---------------------------------------------------------------------------
# Whose domain is the company's
#
# The contract says a same-domain address may be invited directly because the
# same domain is the same organisation. The schema holds no such fact: there is
# no companies table and no domain column anywhere. So it is DERIVED from the
# accounts the tenant already has, and the derivation is allowed to fail.
#
# Only ACTIVE accounts vote. An account at STATUS_INVITED has not accepted, so
# a queued cross-domain invitation cannot widen the very boundary that is
# deciding whether it needed approval.
#
# THE CONSEQUENCE, STATED RATHER THAN DISCOVERED. Once a tenant admits somebody
# on a second domain and they accept, the derivation is ambiguous for ever after
# and EVERY invite goes to a holder of user.manage. That is fail-closed and
# visible, and it is a real cost: the fast path closes for a company that hires
# one contractor. The fix is the settings table holding the domain explicitly,
# and the `stated` argument here is where that value will arrive.
# ---------------------------------------------------------------------------

DOMAIN_FROM_CALLER = "caller"
DOMAIN_FROM_ACCOUNTS = "accounts"
DOMAIN_UNDECIDED_NONE = "no_accounts"
DOMAIN_UNDECIDED_SEVERAL = "several"


@dataclass(frozen=True, slots=True)
class CompanyDomain:
    """The company's email domain, or the reason nothing here can say."""

    company_id: str
    domain: str | None
    source: str
    candidates: tuple[str, ...]

    @property
    def decided(self) -> bool:
        return self.domain is not None

    @property
    def announcement(self) -> str:
        if self.source == DOMAIN_FROM_CALLER:
            return f"the company's domain was given as {self.domain}"
        if self.source == DOMAIN_FROM_ACCOUNTS:
            return (
                f"every active account in this company is on {self.domain}, so "
                "that is being read as the company's own domain"
            )
        if self.source == DOMAIN_UNDECIDED_SEVERAL:
            return (
                "the accounts in this company sit on "
                f"{len(self.candidates)} different domains "
                f"({', '.join(self.candidates)}), so nothing here can say which "
                "one is the company's. Every invite goes to an admin until "
                "somebody states it."
            )
        return (
            "this company has no active accounts to read a domain from, so "
            "every invite goes to an admin."
        )


#: What _domain_of gives back for a string it cannot read as one address. Empty
#: rather than None so every caller keeps comparing strings, and empty can never
#: equal a real domain -- but same_organisation refuses it by name anyway rather
#: than trusting that, because "no domain matched no domain" is the shape of
#: answer this product has been bitten by before.
NO_DOMAIN = ""


def _domain_of(email: str) -> str:
    """The label after the one @, or NO_DOMAIN when there is not exactly one.

    THE COUNT IS CHECKED HERE AS WELL AS IN normalise_email, AND THE REPETITION
    IS THE POINT -- it is the backward half of the same fix. normalise_email
    stopped victim@evil.example@mep.example being WRITTEN on 2026-08-04. It says
    nothing about the rows already in the table, and this is the function that
    reads them: company_domain derives a tenant's own domain from the addresses
    of its active accounts, straight off User.email, and this used to return
    everything after the LAST @ -- so a row like that voted for mep.example and
    then answered "same organisation" about itself. The live strata.db in this
    repository predates the guard. A forward path and a backward path, and only
    the forward one was closed.

    NO_DOMAIN rather than a guess. There is no way to tell which @ was meant,
    and this decision skips the administrator's approval queue, so an unreadable
    address makes the derivation ambiguous exactly as a second real domain does
    and every invite goes to a holder of user.manage until somebody sorts the
    row out. That is the cost, and it is loud.
    """
    if email.count("@") != 1:
        return NO_DOMAIN
    return email.rpartition("@")[2]


def company_domain(
    session: Session, company_id: str, *, stated: str | None = None
) -> CompanyDomain:
    """The company's domain, and where that answer came from."""
    _require_scope(company_id)
    if stated:
        return CompanyDomain(
            company_id=company_id,
            domain=stated.strip().lower().lstrip("@"),
            source=DOMAIN_FROM_CALLER,
            candidates=(),
        )

    rows = (
        session.query(User.email)
        .filter(User.company_id == company_id)
        .filter(User.status == STATUS_ACTIVE)
        .all()
    )
    found = sorted({_domain_of(email) for (email,) in rows if email})
    if not found:
        return CompanyDomain(company_id, None, DOMAIN_UNDECIDED_NONE, ())
    if len(found) > 1:
        return CompanyDomain(
            company_id, None, DOMAIN_UNDECIDED_SEVERAL, tuple(found)
        )
    return CompanyDomain(company_id, found[0], DOMAIN_FROM_ACCOUNTS, tuple(found))


def same_organisation(address: str, domain: CompanyDomain) -> bool:
    """Exact domain match, case already folded, and NO SUBDOMAIN COUNTS.

    contractor.mep.example is not mep.example here. A label to the left of a
    company's domain is delegated, often to somebody who is not the company --
    that is what subdomains are for -- so treating it as the same organisation
    would hand the fast path to whoever holds the delegation. An undecidable
    company domain is never a match: fail closed.

    AN ADDRESS THIS CANNOT READ IS NEVER A MATCH EITHER, and that is checked by
    name rather than left to the comparison. NO_DOMAIN is the empty string and
    could not equal a real domain anyway -- but a tenant whose every stored
    address was unreadable would derive NO_DOMAIN as its domain, and the
    comparison would then answer True for exactly the addresses it must refuse.
    Two failures cancelling to a pass is how a guard reports safety. Said out
    loud because the arithmetic is not obvious to a reader in a hurry.
    """
    if not domain.decided:
        return False
    reading = _domain_of(address)
    if reading == NO_DOMAIN:
        return False
    return reading == domain.domain


def _looks_like_the_inviter(inviter_email: str, address: str) -> bool:
    """True when the address is plausibly the inviter's own with a tag on it.

    A HEURISTIC, AND TREATED AS ONE. Plus-addressing is a convention rather than
    a rule, so this decides nothing on its own: a match sends the invitation to
    a holder of user.manage instead of down the fast path. Being wrong costs an
    admin ten seconds; not checking costs the segregation of duties the whole
    permission grid exists to express, because an analyst who cannot approve
    could invite their own second address and accept it.
    """

    def canonical(email: str) -> str:
        local, _at, domain = email.rpartition("@")
        return f"{local.partition('+')[0]}@{domain}"

    return canonical(inviter_email) == canonical(address)


# ---------------------------------------------------------------------------
# Tokens
#
# The scheme app/state/identity.py already defines, reached by an alias rather
# than reimplemented. secrets.token_urlsafe(32) is 32 random bytes -- the floor
# the design asks for -- and the stored value is sha256 of it, which is right
# for the reason LoginSession.token_hash gives: the input is 32 bytes this
# process generated, so there is no dictionary to run against it and a slow KDF
# would only tax every open. The names below say "invite" because that is what
# the call sites are about; the objects are identity's, so the two cannot drift.
# ---------------------------------------------------------------------------

new_invite_token = new_session_token
hash_invite_token = hash_session_token
invite_token_matches = session_token_matches


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InviteOutcome:
    """What happened, why, and what a screen needs to say next.

    token IS RETURNED EXACTLY ONCE and is never stored anywhere. Whoever holds
    this object has to deliver it; nothing here sends mail.

    routed_escalation_ids is what actually moved. It is empty on a queued
    invitation -- no account exists yet, so the item is still in the shared
    queue with its reason recorded, which is what routing.py already does
    correctly and must keep doing.
    """

    reason_code: str
    reason_text: str
    invitation_id: str | None = None
    invited_user_id: str | None = None
    status: str | None = None
    token: str | None = None
    needs_approval: bool = False
    self_invite: bool = False
    route_reason_code: str | None = None
    obligation_id: str | None = None
    routed_escalation_ids: tuple[str, ...] = ()

    @property
    def written(self) -> bool:
        """True when an invitation row exists because of this call."""
        return self.reason_code in (INV_OK, INV_QUEUED)


@dataclass(frozen=True, slots=True)
class ProvisionOutcome:
    """What provision_login and resend did, and the raw token if they made one.

    A type of its own rather than InviteOutcome, which carries the routing
    fields a handoff needs -- obligation_id, routed_escalation_ids,
    needs_approval -- and would be empty for all of them here. An outcome whose
    half its fields are permanently None invites a caller to read meaning into
    the emptiness.

    invitation IS THE ROW, not its id. The two callers of this are a screen that
    renders the expiry and a mail sender that needs the address, and both
    otherwise re-read the row they were just handed.

    token IS THE RAW TOKEN AND THIS IS THE ONLY MOMENT IT EXISTS. It is never
    stored -- Invitation.token_hash holds sha256 of it -- and it is never
    written to the audit chain. Whoever holds this object delivers it or nobody
    does, which means an invitation nobody delivered looks exactly like one
    nobody accepted, and this product cannot tell the two apart.
    """

    reason_code: str
    reason_text: str
    invitation: Invitation | None = None
    token: str | None = None
    user_id: str | None = None
    #: On a resend, a row that was live and is now dead because of this call.
    #: None on a first issue, and None when the resend killed nothing -- which
    #: happens when the caller named a row that some earlier resend had already
    #: replaced. It names the caller's own row when that is one of the ones that
    #: died, because that is the id the screen is holding.
    superseded_invitation_id: str | None = None

    @property
    def provisioned(self) -> bool:
        return self.reason_code == INV_OK


@dataclass(frozen=True, slots=True)
class AccountRow:
    """One account on the admin screen, with everything that screen shows.

    last_login_at IS None WHEN THE PERSON HAS NEVER SIGNED IN, AND STAYS None.
    models.py argues the column: "never logged in" and "logged in a year ago"
    are different facts about an account, and a default of now would erase the
    difference. That argument is worth exactly nothing if the read on the way to
    the screen coerces it to a date or a string to keep a template simple. The
    screen renders the distinction; this read must not destroy it first.

    The invitation fields are all None when there is none outstanding, rather
    than a set of empty strings, for the same reason.

    invitation_live IS COMPUTED, NOT STORED. A row does not change status when
    its clock runs out -- routing.py decides expiry by comparison at read time
    so an invitation that lapses overnight changes what the screen says in the
    morning without any job having run -- so this is the answer at `now` and
    invitation_status is the word on the row. Both are here because they answer
    different questions: "can they still use it" and "what happened to it".
    """

    user_id: str
    email: str
    display_name: str
    status: str
    roles: tuple[str, ...]
    last_login_at: datetime | None
    invitation_id: str | None = None
    invitation_status: str | None = None
    invitation_kind: str | None = None
    invitation_expires_at: datetime | None = None
    invitation_live: bool = False

    @property
    def has_signed_in(self) -> bool:
        """For a screen that wants the fact without testing None itself."""
        return self.last_login_at is not None


@dataclass(frozen=True, slots=True)
class AcceptOutcome:
    """The result of somebody accepting, or the one sentence a dead link gets.

    On ACCEPT_REFUSED every field but the code and the text is None, on purpose:
    a refusal that says which company the token belonged to has answered a
    question the holder of a dead token was not entitled to ask.
    """

    reason_code: str
    reason_text: str
    company_id: str | None = None
    user_id: str | None = None
    invitation_id: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    granted_roles: tuple[str, ...] = ()
    #: The codes the granted role carries that whoever released the invitation
    #: does not hold themselves. Empty on a grant that stayed under the ceiling,
    #: and empty on every provisioned login, which is refused before it gets
    #: here. It is CARRIED RATHER THAN LEFT IN THE LOG because the hardest
    #: decision in this module has to be visible in the product and not only to
    #: somebody who thinks to read the audit chain afterwards. See
    #: CEILING_WAIVED_BY_KIND for what the product does about it, which is
    #: record it rather than refuse it, and why.
    granted_beyond_ceiling: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.reason_code == ACCEPT_OK


# ---------------------------------------------------------------------------
# Small guards, spelt the way the rest of app/state does
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_actor(actor: str) -> str:
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    return actor


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone; a naive timestamp is a defect")
    return moment


def require_justification(
    *, kind: str, subject_type: str | None, subject_id: str | None
) -> None:
    """No invitation is unjustified. Raises when one would be.

    THE RULE THE TWO NOT NULLS USED TO BE. subject_type and subject_id are
    nullable now because a provisioned login has no item, and the reason the
    columns were required has not gone anywhere: an invitation an admin cannot
    account for is an account nobody asked for. So the rule is stated by kind.
    A handoff must still name its item, exactly as before. A provision may name
    none, and pays for it elsewhere -- user.manage, and the admin's id on the
    row, checked in provision_login.

    Raises rather than returning a reason code, unlike everything else in this
    module. A caller reaching here with a handoff and no item has a defect in
    it, not a refusal to render: nothing a user typed can produce this, because
    the subject comes from the screen the button was on.

    THE CHECK CONSTRAINT IN models.py SAYS THE SAME THING, AND BOTH ARE WANTED.
    This one gives a sentence and leaves the transaction alive; that one binds
    a writer who never called this. An IntegrityError at flush aborts the whole
    transaction, which on this path would discard the audit entry alongside the
    row -- the argument create_user already makes about duplicate addresses.
    """
    if kind not in INVITE_KINDS:
        raise ValueError(
            f"kind must be one of {INVITE_KINDS}; got {kind!r}. An invitation "
            "whose kind nothing recognises is one nothing can say the "
            "justification for."
        )
    if kind != INVITE_KIND_HANDOFF:
        return
    if not subject_type or not subject_id:
        raise ValueError(
            f"a {INVITE_KIND_HANDOFF} invitation names the item that justified "
            f"it; got subject_type={subject_type!r} subject_id={subject_id!r}. "
            "An admin reading the queue has to see what the person is being "
            f"pulled in to do. A bare account is a {INVITE_KIND_PROVISION} "
            "invitation, which needs user.manage and records who performed it."
        )


def _new_invitation_id(session: Session) -> str:
    """INV-0001, counting this company's rows and every other tenant's.

    A counter rather than a random suffix because models.py illustrates it that
    way and an admin queue reads these aloud. It is scanned across the whole
    table rather than per company, so the id stays unique where the primary key
    is -- two tenants minting at the same instant race for a number and the
    loser gets an IntegrityError, which is loud, rather than a duplicate, which
    is not.
    """
    used = [
        int(row.rpartition("-")[2])
        for (row,) in session.query(Invitation.id).all()
        if row.rpartition("-")[2].isdigit()
    ]
    return f"INV-{(max(used) + 1) if used else 1:04d}"


def _refused(
    session: Session,
    company_id: str,
    *,
    code: str,
    text: str,
    actor: str,
    actor_user_id: str | None = None,
    moment: datetime,
    subject_id: str = "",
    action: str = ACTION_INVITE_REFUSED,
    **extra,
) -> InviteOutcome:
    """Record the refusal and hand it back. Every refusal here is audited.

    The reason a decision not to invite somebody is in the chain at all: an
    analyst who says "I asked for that person weeks ago" and an admin who says
    "nobody ever asked" are otherwise two accounts of an empty table.
    """
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type="invitation",
        subject_id=subject_id,
        reason=f"{code}: {text}",
        occurred_at=moment,
        actor_user_id=actor_user_id,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    session.flush()
    return InviteOutcome(reason_code=code, reason_text=text, **extra)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def invitations_for_company(
    session: Session, company_id: str, *, status: str | None = None
) -> list[Invitation]:
    _require_scope(company_id)
    query = session.query(Invitation).filter(Invitation.company_id == company_id)
    if status is not None:
        if status not in INVITATION_STATUSES:
            raise ValueError(
                f"status must be one of {INVITATION_STATUSES}; got {status!r}"
            )
        query = query.filter(Invitation.status == status)
    return query.order_by(Invitation.invited_at, Invitation.id).all()


def invitations_awaiting_approval(
    session: Session, company_id: str
) -> list[Invitation]:
    """The admin queue. Cross-domain invitations nobody has released yet."""
    return invitations_for_company(
        session, company_id, status=INVITE_AWAITING_APPROVAL
    )


def invitation_for_company(
    session: Session, company_id: str, invitation_id: str
) -> Invitation | None:
    """One invitation, or None. Another company's id resolves to None here."""
    _require_scope(company_id)
    if not invitation_id:
        return None
    return (
        session.query(Invitation)
        .filter(Invitation.company_id == company_id)
        .filter(Invitation.id == invitation_id)
        .one_or_none()
    )


def _invitations_to(
    session: Session, company_id: str, email: str
) -> list[Invitation]:
    """Every invitation this company sent that address, newest first."""
    _require_scope(company_id)
    try:
        cleaned = normalise_email(email)
    except ValueError:
        return []
    return (
        session.query(Invitation)
        .filter(Invitation.company_id == company_id)
        .filter(Invitation.email == cleaned)
        .order_by(Invitation.invited_at.desc(), Invitation.id.desc())
        .all()
    )


def live_invitation_for_email(
    session: Session, company_id: str, *, email: str, now: datetime | None = None
) -> Invitation | None:
    """An invitation this address can accept right now, or None.

    ix_invitations_company_email is deliberately not unique -- revoke and
    re-invite are two legitimate rows -- so "is one live" is a question the
    write layer answers, and this is where it answers it.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")
    for row in _invitations_to(session, company_id, email):
        if invitation_is_live(row, moment):
            return row
    return None


def open_invitation_for_email(
    session: Session, company_id: str, *, email: str, now: datetime | None = None
) -> Invitation | None:
    """An invitation to this address that is still going somewhere, or None.

    WIDER THAN live_invitation_for_email, AND THE TWO ARE NOT THE SAME QUESTION.
    "Can this person accept" excludes an invitation waiting for an admin,
    because nobody can accept it yet. "Is one already out" includes it, because
    a second row would put the same decision in front of the admin twice and
    leave whichever they refused standing in the other. Using the narrow read
    for the duplicate check let a queued address be invited again, which a test
    caught; the two reads are kept apart rather than one of them widened.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")
    for row in _invitations_to(session, company_id, email):
        if row.status == INVITE_AWAITING_APPROVAL and row.expires_at > moment:
            return row
        if invitation_is_live(row, moment):
            return row
    return None


def revoked_invitation_for_email(
    session: Session, company_id: str, *, email: str
) -> Invitation | None:
    """The most recent withdrawn invitation to this address, or None."""
    _require_scope(company_id)
    try:
        cleaned = normalise_email(email)
    except ValueError:
        return None
    return (
        session.query(Invitation)
        .filter(Invitation.company_id == company_id)
        .filter(Invitation.email == cleaned)
        .filter(Invitation.status == INVITE_REVOKED)
        .order_by(Invitation.invited_at.desc(), Invitation.id.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# The gap an invitation is allowed to close
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Gap:
    """One unowned duty and everything currently waiting on it."""

    obligation_id: str
    escalation_ids: tuple[str, ...]


def _subject_exists(
    session: Session, company_id: str, *, subject_type: str, subject_id: str
) -> bool:
    """Is there such an item in this company at all.

    Split out from the gap resolution below because the two answer different
    questions and the refusals have to happen in that order. "There is no such
    escalation" is a fact about the request; "that escalation does not need an
    invitation" is a fact about the world, and a caller who typed the wrong id
    must not be told about the state of somebody else's inbox.
    """
    if subject_type == INVITE_SUBJECT_ESCALATION:
        return (
            session.query(Escalation)
            .filter(Escalation.company_id == company_id)
            .filter(Escalation.id == subject_id)
            .one_or_none()
        ) is not None
    return obligation_for_company(session, company_id, subject_id) is not None


def _gap_for_subject(
    session: Session,
    company_id: str,
    *,
    subject_type: str,
    subject_id: str,
    moment: datetime,
) -> tuple[_Gap | None, str | None, str, str | None]:
    """Resolve the item into one unowned obligation, or say why it is not one.

    Returns (gap, refusal_code, refusal_text, route_reason_code). Re-derived
    rather than stored, and re-run at approval time as well as at invite time,
    for the reason routing.py gives about its own queue: an obligation somebody
    gave an owner this morning must change what happens this afternoon without
    anything having re-run. An approval that admitted a person into a gap that
    had since been closed would be creating an account for no reason at all.
    """
    if subject_type == INVITE_SUBJECT_ESCALATION:
        escalation = (
            session.query(Escalation)
            .filter(Escalation.company_id == company_id)
            .filter(Escalation.id == subject_id)
            .one_or_none()
        )
        if escalation is None:
            return (
                None,
                INV_NO_SUBJECT,
                f"no escalation {subject_id!r} for this company",
                None,
            )
        resolution = resolve_escalation_owner(
            session, company_id, escalation_id=subject_id, now=moment
        )
        if resolution.routed:
            return (
                None,
                INV_NOT_A_GAP,
                f"this item is already on somebody's desk: {resolution.reason_text}",
                resolution.reason_code,
            )
        if resolution.reason_code != ROUTE_OBLIGATION_UNOWNED:
            return (
                None,
                INV_NOT_A_GAP,
                "an invitation is the fix for one refusal -- an obligation whose "
                "owner has no account here -- and this item is refused for "
                f"another reason: {resolution.reason_text}",
                resolution.reason_code,
            )
        ids = resolution.obligation_ids
        if len(ids) != 1:
            return (
                None,
                INV_OBLIGATIONS_AMBIGUOUS,
                f"this change touches {len(ids)} obligations with no owner "
                f"({', '.join(ids)}). Which duty would this person own? Nothing "
                "here can say, so nobody is invited.",
                resolution.reason_code,
            )
        return _Gap(obligation_id=ids[0], escalation_ids=(subject_id,)), None, "", None

    obligation = obligation_for_company(session, company_id, subject_id)
    if obligation is None:
        return (
            None,
            INV_NO_SUBJECT,
            f"no obligation {subject_id!r} for this company",
            None,
        )
    if obligation.owner_user_id is not None:
        return (
            None,
            INV_NOT_A_GAP,
            f"{subject_id} already names an owner account. Moving a duty to "
            "somebody else is a reassignment, not an invitation.",
            None,
        )
    waiting = tuple(
        item.escalation.id
        for item in shared_queue(session, company_id)
        if subject_id in item.resolution.obligation_ids
    )
    return _Gap(obligation_id=subject_id, escalation_ids=waiting), None, "", None


# ---------------------------------------------------------------------------
# Writing an invitation
# ---------------------------------------------------------------------------


def _admit(
    session: Session,
    company_id: str,
    *,
    invitation: Invitation,
    display_name: str,
    gap: _Gap,
    actor: str,
    actor_user_id: str,
    moment: datetime,
) -> tuple[str, tuple[str, ...]]:
    """Create the invited account, put it on the duty, and hand the work over.

    The order is load-bearing. The account has to exist before the duty can
    name it, the invitation has to point at the account before routing can tell
    a pending handoff from a dead end, and the duty has to have an owner before
    the escalation can walk to one. Anything else routes nothing and says
    nothing about why.

    The password is 32 random bytes nobody keeps. An invited account is not an
    account with a blank password waiting to be guessed; it is an account with
    an unguessable one and a status that cannot sign in, and acceptance replaces
    both. NO ROLE IS GRANTED HERE -- the grant happens on acceptance, so an
    invitation nobody ever accepts leaves nothing behind that can act.
    """
    person = create_user(
        session,
        company_id,
        email=invitation.email,
        display_name=display_name,
        password=secrets.token_urlsafe(32),
        actor=actor,
        status=STATUS_INVITED,
        created_at=moment,
    )
    invitation.invited_user_id = person.id
    session.flush()

    obligation = set_obligation_owner(
        session,
        company_id,
        obligation_id=gap.obligation_id,
        owner_user_id=person.id,
        actor=actor,
        only_when_unowned=True,
        occurred_at=moment,
    )
    if obligation.owner_user_id != person.id:
        # only_when_unowned refused, which means the duty gained an owner
        # between the gap check and this write. Raising rolls the whole
        # transaction back, and that is the wanted outcome: the alternative is
        # an invited account attached to nothing, in a company that never
        # needed it, with an audit row saying somebody was invited to a duty
        # that already had a person on it.
        raise ValueError(
            f"{gap.obligation_id} gained an owner while this invitation was "
            "being written; nothing has been kept."
        )

    routed: list[str] = []
    for escalation_id in gap.escalation_ids:
        resolution = route_escalation(
            session,
            company_id,
            escalation_id=escalation_id,
            actor=actor,
            actor_user_id=actor_user_id,
            now=moment,
        )
        if resolution.routed:
            routed.append(escalation_id)
    return person.id, tuple(routed)


def _invite(
    session: Session,
    company_id: str,
    *,
    subject_type: str,
    subject_id: str,
    email: str | None,
    display_name: str,
    invited_by_user_id: str,
    actor: str,
    ttl_days: int,
    stated_company_domain: str | None,
    now: datetime | None,
) -> InviteOutcome:
    """The whole decision, in the order the refusals have to happen."""
    _require_scope(company_id)
    who = _require_actor(actor)
    moment = _require_aware(now or _utcnow(), "now")
    if subject_type not in INVITE_SUBJECT_TYPES:
        raise ValueError(
            f"subject_type must be one of {INVITE_SUBJECT_TYPES}; got {subject_type!r}"
        )
    if not 1 <= ttl_days <= INVITE_MAX_TTL_DAYS:
        raise ValueError(
            f"an invitation lives between 1 and {INVITE_MAX_TTL_DAYS} days; "
            f"got {ttl_days}"
        )

    policy = invite_policy(session, company_id=company_id)
    if not policy.enabled.value:
        return _refused(
            session,
            company_id,
            code=INV_DISABLED,
            text=(
                "invites are switched off for this tenant, so nobody can be "
                f"pulled in. {policy.enabled.announcement}"
            ),
            actor=who,
            moment=moment,
            subject_id=subject_id,
        )

    inviter = user_for_company(session, company_id, invited_by_user_id)
    if inviter is None or inviter.status != STATUS_ACTIVE:
        return _refused(
            session,
            company_id,
            code=INV_NO_INVITER,
            text=(
                f"{invited_by_user_id!r} is not an active account in this "
                "company, so there is nobody to attribute this invitation to."
            ),
            actor=who,
            moment=moment,
            subject_id=subject_id,
        )

    may_invite = user_has_permission(session, company_id, inviter.id, "user.invite")
    # A holder of user.manage can create any account outright, so refusing them
    # the narrower path would be theatre -- and it would push them onto the
    # wider one, which is the opposite of what this permission split is for.
    may_manage = user_has_permission(session, company_id, inviter.id, "user.manage")
    if not (may_invite or may_manage):
        return _refused(
            session,
            company_id,
            code=INV_NOT_PERMITTED,
            text=(
                f"{inviter.display_name} holds neither user.invite nor "
                "user.manage, so they cannot pull somebody into this company."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            action=ACTION_ACCESS_DENIED,
        )

    if not email or not str(email).strip():
        return _refused(
            session,
            company_id,
            code=INV_NO_EMAIL,
            text=(
                f"there is no address for {display_name or 'this person'}. The "
                "company context names the owner of every duty and carries no "
                "address for any of them, and an address built out of a name is "
                "a guess -- one bounced on this project this morning. Ask "
                "whoever knows it; nothing here will invent it."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
        )
    try:
        cleaned = normalise_email(str(email))
    except ValueError as error:
        return _refused(
            session,
            company_id,
            code=INV_EMAIL_MALFORMED,
            text=(
                f"{error}. An address nothing can read is refused rather than "
                "repaired: a repaired address is a guess at who was meant."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
        )

    if not display_name or not display_name.strip():
        return _refused(
            session,
            company_id,
            code=INV_NO_NAME,
            text=(
                f"no name was given for {cleaned}. An account nobody can name "
                "in a review is not attributable, and a name read off the local "
                "part of an address is the same guess as the address."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
        )

    if not _subject_exists(
        session, company_id, subject_type=subject_type, subject_id=subject_id
    ):
        return _refused(
            session,
            company_id,
            code=INV_NO_SUBJECT,
            text=f"no {subject_type} {subject_id!r} for this company",
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
        )

    # WHAT IS ALREADY TRUE OF THIS PERSON COMES BEFORE WHAT IS TRUE OF THE ITEM,
    # and the order was wrong until a test caught it. Inviting the same person
    # to the same item twice hit "that item is already on somebody's desk" --
    # true, useless, and it never named the invitation that put them there. The
    # question the caller asked is about a person, so the answer is about a
    # person, and the item's own state is the refusal underneath it.
    existing = user_by_email(session, company_id, cleaned)
    if existing is not None and existing.status == STATUS_ACTIVE:
        return _refused(
            session,
            company_id,
            code=INV_ALREADY_A_USER,
            text=(
                f"{cleaned} already has an active account here "
                f"({existing.display_name}). Put the duty on that account rather "
                "than making a second person out of one."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            invited_user_id=existing.id,
        )

    live = open_invitation_for_email(session, company_id, email=cleaned, now=moment)
    if live is not None:
        return _refused(
            session,
            company_id,
            code=INV_ALREADY_LIVE,
            text=(
                f"{cleaned} already has a live invitation ({live.id}, "
                f"{live.status}, expires {live.expires_at.isoformat()}). A "
                "second one would put two acceptance links in one inbox."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            invitation_id=live.id,
            status=live.status,
        )

    withdrawn = revoked_invitation_for_email(session, company_id, email=cleaned)
    if withdrawn is not None:
        return _refused(
            session,
            company_id,
            code=INV_WAS_REVOKED,
            text=(
                f"an invitation to {cleaned} was withdrawn ({withdrawn.id}). "
                "Sending another would undo that decision without anybody "
                "revisiting it, so this needs a holder of user.manage."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            invitation_id=withdrawn.id,
            status=withdrawn.status,
        )

    if existing is not None:
        # An account that is here and not active and has no live or withdrawn
        # invitation behind it: an invitation that ran out, or somebody an admin
        # suspended. Reviving either is an account decision, and quietly writing
        # a second invitation over the top would hide which of the two it was.
        return _refused(
            session,
            company_id,
            code=INV_ALREADY_A_USER,
            text=(
                f"{cleaned} already has an account here "
                f"({existing.display_name}, {existing.status}), and no live "
                "invitation behind it. Reinstating that account is a "
                "user.manage decision rather than a fresh invitation."
            ),
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            invited_user_id=existing.id,
        )

    gap, code, text, route_code = _gap_for_subject(
        session,
        company_id,
        subject_type=subject_type,
        subject_id=subject_id,
        moment=moment,
    )
    if gap is None:
        return _refused(
            session,
            company_id,
            code=code,
            text=text,
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
            subject_id=subject_id,
            route_reason_code=route_code,
        )

    # --- who decides: the domain, the permission, and the tenant switch ---
    domain = company_domain(
        session, company_id, stated=stated_company_domain
    )
    same_domain = same_organisation(cleaned, domain)
    self_invite = _looks_like_the_inviter(inviter.email, cleaned)
    # user.manage takes the fast path too, on the same domain. Sending an admin
    # round the approval queue would only mean they approved their own request,
    # and refusing them the narrow path would push them onto create_user, which
    # is wider and attached to no item at all.
    direct = (
        same_domain
        and (may_invite or may_manage)
        and not policy.need_approval.value
        and not self_invite
    )

    if direct:
        held = "user.invite" if may_invite else "user.manage"
        why = (
            f"{inviter.display_name} holds {held} and {cleaned} is on the "
            f"company's own domain ({domain.announcement})"
        )
    elif self_invite:
        why = (
            f"{cleaned} looks like {inviter.display_name}'s own address with a "
            "tag on it. An analyst who cannot approve must not be able to make "
            "an account that can, so a person decides this one."
        )
    elif policy.need_approval.value:
        why = f"this tenant sends every invitation to an admin. {policy.need_approval.announcement}"
    elif not same_domain:
        why = (
            f"{cleaned} is not on the company's own domain. {domain.announcement}"
        )
    else:
        why = (
            f"{inviter.display_name} may ask for an invitation and does not hold "
            "user.invite, so a holder of user.manage releases it."
        )

    # The rule the NOT NULL used to be. Unreachable from a form -- the subject
    # comes from the screen the button was on -- so it raises rather than
    # refusing, and it raises BEFORE the row is added, so an IntegrityError from
    # the matching check constraint cannot take the audit entry down with it.
    require_justification(
        kind=INVITE_KIND_HANDOFF, subject_type=subject_type, subject_id=subject_id
    )

    token = new_invite_token()
    invitation = Invitation(
        id=_new_invitation_id(session),
        company_id=company_id,
        email=cleaned,
        invited_by_user_id=inviter.id,
        invited_at=moment,
        token_hash=hash_invite_token(token),
        expires_at=moment + timedelta(days=ttl_days),
        accepted_at=None,
        status=INVITE_PENDING if direct else INVITE_AWAITING_APPROVAL,
        approved_by_user_id=None,
        kind=INVITE_KIND_HANDOFF,
        subject_type=subject_type,
        subject_id=subject_id,
        invited_user_id=None,
    )
    session.add(invitation)
    session.flush()

    invited_user_id: str | None = None
    routed: tuple[str, ...] = ()
    if direct:
        invited_user_id, routed = _admit(
            session,
            company_id,
            invitation=invitation,
            display_name=display_name.strip(),
            gap=gap,
            actor=who,
            actor_user_id=inviter.id,
            moment=moment,
        )

    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_INVITE_CREATED if direct else ACTION_INVITE_QUEUED,
        subject_type="invitation",
        subject_id=invitation.id,
        # The address and the item, never the token. A tamper-evident log is
        # read by more people than this table is, and a secret written into an
        # append-only record cannot be taken out again.
        reason=(
            f"{display_name.strip()} <{cleaned}> invited to {subject_type} "
            f"{subject_id} for {gap.obligation_id}; {why}"
        ),
        occurred_at=moment,
        actor_user_id=inviter.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()

    return InviteOutcome(
        reason_code=INV_OK if direct else INV_QUEUED,
        reason_text=why,
        invitation_id=invitation.id,
        invited_user_id=invited_user_id,
        status=invitation.status,
        token=token,
        needs_approval=not direct,
        self_invite=self_invite,
        obligation_id=gap.obligation_id,
        routed_escalation_ids=routed,
    )


def invite_owner_for_escalation(
    session: Session,
    company_id: str,
    *,
    escalation_id: str,
    email: str | None,
    display_name: str,
    invited_by_user_id: str,
    actor: str,
    ttl_days: int = INVITE_DEFAULT_TTL_DAYS,
    stated_company_domain: str | None = None,
    now: datetime | None = None,
) -> InviteOutcome:
    """Invite the person who owns the duty this escalation lands on.

    The one dead end routing could not clear. It applies only where the walk
    ends in ROUTE_OBLIGATION_UNOWNED on exactly one duty; every other refusal
    routing makes is a different fix and comes back as INV_NOT_A_GAP with the
    routing code attached.
    """
    return _invite(
        session,
        company_id,
        subject_type=INVITE_SUBJECT_ESCALATION,
        subject_id=escalation_id,
        email=email,
        display_name=display_name,
        invited_by_user_id=invited_by_user_id,
        actor=actor,
        ttl_days=ttl_days,
        stated_company_domain=stated_company_domain,
        now=now,
    )


def invite_owner_for_obligation(
    session: Session,
    company_id: str,
    *,
    obligation_id: str,
    email: str | None,
    display_name: str,
    invited_by_user_id: str,
    actor: str,
    ttl_days: int = INVITE_DEFAULT_TTL_DAYS,
    stated_company_domain: str | None = None,
    now: datetime | None = None,
) -> InviteOutcome:
    """Invite the owner of a duty that has nobody's name on it.

    The same decision reached from the other screen: unowned_obligations() is a
    list of work, and this is the button beside a row of it. Everything waiting
    on that duty routes at once, because they were all waiting on the same
    missing person.
    """
    return _invite(
        session,
        company_id,
        subject_type=INVITE_SUBJECT_OBLIGATION,
        subject_id=obligation_id,
        email=email,
        display_name=display_name,
        invited_by_user_id=invited_by_user_id,
        actor=actor,
        ttl_days=ttl_days,
        stated_company_domain=stated_company_domain,
        now=now,
    )


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def approve_invitation(
    session: Session,
    company_id: str,
    *,
    invitation_id: str,
    approver_user_id: str,
    actor: str,
    display_name: str | None = None,
    now: datetime | None = None,
) -> InviteOutcome:
    """Release a queued invitation: the account is made and the item routes.

    A holder of user.manage only. They may be the person who asked for it: an
    admin can already create any account outright, so refusing self-approval
    here would stop nothing and would push the work onto the wider path. Both
    rows are in the chain, which is what "visible, never silent" means.

    The gap is re-derived rather than trusted. An obligation that gained an
    owner while the invitation sat in the queue is no longer a gap, and creating
    an account for it would be creating an account for nothing.

    display_name IS OPTIONAL HERE AND FALLS BACK TO THE ADDRESS ITSELF, which is
    not the guess this module refuses elsewhere: the address is a fact somebody
    typed, and showing it as the account's name says exactly as much as is
    known. What would be a guess is the other direction -- reading "Priya
    Nandakumar" out of p.nandakumar@ -- and nothing here does that. The screen
    that shows the queue has the name the inviter typed and should pass it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    moment = _require_aware(now or _utcnow(), "now")

    # THE SAME SWITCH _invite ASKS, ASKED HERE TOO, and it was missing until
    # 2026-08-04. _invite refuses with "invites are switched off for this
    # tenant, so nobody can be pulled in" -- and that sentence was false while
    # this function could still release a queued invitation into a live account
    # with a working token. A switch that stops the front door and not the
    # release queue behind it is not the control its own refusal claims to be.
    policy_switch = invite_policy(session, company_id=company_id)
    if not policy_switch.enabled.value:
        return _refused(
            session,
            company_id,
            code=INV_DISABLED,
            text=(
                "invites are switched off for this tenant, so a queued "
                f"invitation cannot be released. {policy_switch.enabled.announcement}"
            ),
            actor=who,
            moment=moment,
            subject_id=invitation_id,
        )

    invitation = invitation_for_company(session, company_id, invitation_id)
    if invitation is None:
        # One message for "not here" and "not yours". Which of the two it is
        # would itself tell a caller that another tenant holds that id.
        return InviteOutcome(
            reason_code=INV_UNKNOWN,
            reason_text=f"no invitation {invitation_id!r} for this company",
        )

    approver = user_for_company(session, company_id, approver_user_id)
    if (
        approver is None
        or approver.status != STATUS_ACTIVE
        or not user_has_permission(session, company_id, approver.id, "user.manage")
    ):
        return _refused(
            session,
            company_id,
            code=INV_NOT_PERMITTED,
            text=(
                f"{approver_user_id!r} does not hold user.manage in this "
                "company, and releasing an invitation is an account decision."
            ),
            actor=who,
            moment=moment,
            subject_id=invitation.id,
            action=ACTION_ACCESS_DENIED,
            invitation_id=invitation.id,
            status=invitation.status,
        )

    if invitation.status != INVITE_AWAITING_APPROVAL:
        return _refused(
            session,
            company_id,
            code=INV_NOT_QUEUED,
            text=(
                f"{invitation.id} is {invitation.status}, not awaiting "
                "approval. There is nothing here to release."
            ),
            actor=who,
            actor_user_id=approver.id,
            moment=moment,
            subject_id=invitation.id,
            invitation_id=invitation.id,
            status=invitation.status,
        )
    if invitation.expires_at <= moment:
        return _refused(
            session,
            company_id,
            code=INV_EXPIRED,
            text=(
                f"{invitation.id} ran out on "
                f"{invitation.expires_at.isoformat()}. Approving it would hand "
                "somebody a link that cannot be used; ask for a fresh one."
            ),
            actor=who,
            actor_user_id=approver.id,
            moment=moment,
            subject_id=invitation.id,
            invitation_id=invitation.id,
            status=invitation.status,
        )

    gap, code, text, route_code = _gap_for_subject(
        session,
        company_id,
        subject_type=invitation.subject_type,
        subject_id=invitation.subject_id,
        moment=moment,
    )
    if gap is None:
        return _refused(
            session,
            company_id,
            code=code,
            text=(
                f"{text} The invitation is left where it is rather than "
                "creating an account nothing is waiting for."
            ),
            actor=who,
            actor_user_id=approver.id,
            moment=moment,
            subject_id=invitation.id,
            invitation_id=invitation.id,
            status=invitation.status,
            route_reason_code=route_code,
        )

    invitation.status = INVITE_APPROVED
    invitation.approved_by_user_id = approver.id
    session.flush()

    invited_user_id, routed = _admit(
        session,
        company_id,
        invitation=invitation,
        display_name=(display_name or invitation.email).strip(),
        gap=gap,
        actor=who,
        actor_user_id=approver.id,
        moment=moment,
    )
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_INVITE_APPROVED,
        subject_type="invitation",
        subject_id=invitation.id,
        reason=(
            f"{approver.display_name} released {invitation.email} onto "
            f"{invitation.subject_type} {invitation.subject_id}"
        ),
        occurred_at=moment,
        actor_user_id=approver.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()

    return InviteOutcome(
        reason_code=INV_OK,
        reason_text=(
            f"{approver.display_name} released this invitation; the item is on "
            f"{invitation.email}'s desk and is not moving until they accept."
        ),
        invitation_id=invitation.id,
        invited_user_id=invited_user_id,
        status=invitation.status,
        needs_approval=False,
        obligation_id=gap.obligation_id,
        routed_escalation_ids=routed,
    )


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def revoke_invitation(
    session: Session,
    company_id: str,
    *,
    invitation_id: str,
    revoked_by_user_id: str,
    actor: str,
    now: datetime | None = None,
) -> InviteOutcome:
    """Withdraw an invitation, suspend the account it made, free the work.

    THREE THINGS HAPPEN AND ALL THREE ARE NEEDED. The invitation dies, so the
    link stops working. The account THIS invitation created is suspended, so a
    half-made person cannot linger -- and an account that was already here is
    never touched, which is what invited_user_id is for. And the item goes back
    to the shared queue, because an item on a desk nobody occupies is the
    "looks handled and is not" failure routing.py exists to refuse.

    An accepted invitation cannot be revoked. Taking somebody's account away
    after they have joined is a suspension, a different decision with its own
    audit code, and doing it through this door would hide it under an
    invitation the person had already answered.

    WHO MAY. The person who sent it, or a holder of user.manage. The same rule
    the share link follows, and it is checked here rather than left to the view:
    without it any account in the tenant could take an item off a colleague's
    desk and suspend the person standing on it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    moment = _require_aware(now or _utcnow(), "now")

    invitation = invitation_for_company(session, company_id, invitation_id)
    if invitation is None:
        return InviteOutcome(
            reason_code=INV_UNKNOWN,
            reason_text=f"no invitation {invitation_id!r} for this company",
        )

    revoker = user_for_company(session, company_id, revoked_by_user_id)
    entitled = (
        revoker is not None
        and revoker.status == STATUS_ACTIVE
        and (
            revoker.id == invitation.invited_by_user_id
            or user_has_permission(session, company_id, revoker.id, "user.manage")
        )
    )
    if not entitled:
        return _refused(
            session,
            company_id,
            code=INV_NOT_PERMITTED,
            text=(
                f"{revoked_by_user_id!r} neither sent {invitation.id} nor holds "
                "user.manage, so they cannot withdraw it."
            ),
            actor=who,
            moment=moment,
            subject_id=invitation.id,
            action=ACTION_ACCESS_DENIED,
            invitation_id=invitation.id,
            status=invitation.status,
        )

    if invitation.status == INVITE_ACCEPTED:
        return _refused(
            session,
            company_id,
            code=INV_ALREADY_ACCEPTED,
            text=(
                f"{invitation.email} accepted on "
                f"{invitation.accepted_at.isoformat()}. Withdrawing the "
                "invitation now would not take the account back; suspend the "
                "account if that is what is meant."
            ),
            actor=who,
            moment=moment,
            subject_id=invitation.id,
            invitation_id=invitation.id,
            status=invitation.status,
            invited_user_id=invitation.invited_user_id,
        )
    if invitation.status == INVITE_REVOKED:
        return InviteOutcome(
            reason_code=INV_REVOKED_OK,
            reason_text=f"{invitation.id} was already withdrawn",
            invitation_id=invitation.id,
            status=invitation.status,
        )

    invitation.status = INVITE_REVOKED
    session.flush()

    freed: list[str] = []
    made = invitation.invited_user_id
    if made:
        person = user_for_company(session, company_id, made)
        # Only an account still at STATUS_INVITED, and only the one this row
        # created. A person who accepted a later invitation, or who was already
        # active, keeps their account.
        if person is not None and person.status == STATUS_INVITED:
            set_user_status(session, company_id, made, STATUS_SUSPENDED, who)
            for escalation in (
                session.query(Escalation)
                .filter(Escalation.company_id == company_id)
                .filter(Escalation.assigned_to_user_id == made)
                .filter(Escalation.resolved_at.is_(None))
                .order_by(Escalation.id)
                .all()
            ):
                unroute_escalation(
                    session,
                    company_id,
                    escalation_id=escalation.id,
                    actor=who,
                    reason=f"invitation {invitation.id} withdrawn",
                    actor_user_id=revoked_by_user_id or None,
                    now=moment,
                )
                freed.append(escalation.id)
            # The duty goes back to having nobody's name on it, which is a state
            # the interface shows as work rather than a tidy empty column.
            for obligation_id in _duties_held_by(session, company_id, made):
                set_obligation_owner(
                    session,
                    company_id,
                    obligation_id=obligation_id,
                    owner_user_id=None,
                    actor=who,
                    occurred_at=moment,
                )

    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_INVITE_REVOKED,
        subject_type="invitation",
        subject_id=invitation.id,
        reason=(
            f"{invitation.email} withdrawn; "
            f"{len(freed)} item(s) back in the shared queue"
        ),
        occurred_at=moment,
        actor_user_id=revoked_by_user_id or None,
        actor_kind=ACTOR_USER if revoked_by_user_id else ACTOR_SYSTEM,
    )
    session.flush()

    return InviteOutcome(
        reason_code=INV_REVOKED_OK,
        reason_text=(
            f"{invitation.email} is withdrawn. "
            f"{len(freed)} item(s) went back to the shared queue."
        ),
        invitation_id=invitation.id,
        invited_user_id=made,
        status=invitation.status,
        routed_escalation_ids=tuple(freed),
    )


def _duties_held_by(session: Session, company_id: str, user_id: str) -> tuple[str, ...]:
    """Obligation ids this account owns, scoped to the company like every read."""
    return tuple(
        row
        for (row,) in session.query(Obligation.id)
        .filter(Obligation.company_id == company_id)
        .filter(Obligation.owner_user_id == user_id)
        .order_by(Obligation.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Provisioning: an admin adds a login
#
# The other reason an invitation exists. No item is waiting, so the control that
# said every invitation names its work is answered the other way -- by an admin
# who holds user.manage and whose id goes on the row. See models.py.
#
# `actor` IN THIS SECTION IS A USER ID, NOT A DISPLAY STRING, AND THAT DIFFERS
# FROM `actor` EVERYWHERE ELSE IN THIS MODULE. It has to be: the permission gate
# needs an account to check, and Invitation.invited_by_user_id is a foreign key
# to users.id. Called out here rather than left to be discovered, because a
# caller passing "person:ada@mep.example" gets INV_NO_INVITER and a caller
# passing a display name gets the same, which reads like a broken account rather
# than a wrong argument.
# ---------------------------------------------------------------------------


def _provision_refused(
    session: Session,
    company_id: str,
    *,
    code: str,
    text: str,
    actor: str,
    actor_user_id: str | None,
    moment: datetime,
    subject_id: str = "",
    action: str = ACTION_INVITE_REFUSED,
    **extra,
) -> ProvisionOutcome:
    """_refused, in the shape this half of the module returns."""
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=action,
        subject_type="invitation",
        subject_id=subject_id,
        reason=f"{code}: {text}",
        occurred_at=moment,
        actor_user_id=actor_user_id,
        actor_kind=ACTOR_USER if actor_user_id else ACTOR_SYSTEM,
    )
    session.flush()
    return ProvisionOutcome(reason_code=code, reason_text=text, **extra)


def _role_names(
    session: Session, company_id: str, user_ids: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """Live role names per account, in one query. Alphabetical within an account.

    Alphabetical rather than grant order, so a screen listing twenty accounts
    does not reorder itself when somebody re-grants a role, and two accounts
    holding the same pair read the same.

    SCOPED IN THREE PLACES ON ONE QUERY, copying permissions_for_user exactly:
    the user's company, the grant's company, and the role being either a system
    role or this company's own. Each column was written by a different call, and
    a bug in any one of them hands somebody another tenant's authority -- or, on
    this read, shows an admin a colleague from another tenant.
    """
    if not user_ids:
        return {}
    rows = (
        session.query(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .join(User, User.id == UserRole.user_id)
        .filter(User.company_id == company_id)
        .filter(UserRole.company_id == company_id)
        .filter(UserRole.user_id.in_(user_ids))
        .filter(UserRole.revoked_at.is_(None))
        .filter(or_(Role.company_id.is_(None), Role.company_id == company_id))
        .all()
    )
    held: dict[str, set[str]] = {}
    for user_id, name in rows:
        held.setdefault(user_id, set()).add(name)
    return {user_id: tuple(sorted(names)) for user_id, names in held.items()}


def _grant_ceiling(
    session: Session, company_id: str, *, actor_user_id: str, role_name: str
) -> tuple[str, ...]:
    """The codes this role carries that the person granting it does not hold.

    AN INVITER MAY NEVER GRANT MORE THAN THEY HOLD, and empty means they may.

    IT ANSWERS THE QUESTION AND IT DOES NOT ENFORCE THE ANSWER. _grant_within_ceiling
    is what every grant in this module goes through, and it is the only caller
    that matters; provision_login also asks here directly so it can refuse before
    it makes an account, which is a better place for that refusal than after.
    Reading this and acting on it yourself is how the rule ended up holding on
    one of the two paths that grant authority.

    Without this, "provision a login" is the shortest path in the product from
    user.manage to any authority at all. The admin role deliberately holds no
    approval permission -- identity.py says so and docs/security.html rests the
    approval design on it -- so an admin who could provision an obligation_owner
    login, accept it from their own mailbox and sign in as it would hold
    action.approve, and every refusal in app/auth/policy.py would be answering
    about the wrong account.

    THE COST IS REAL AND IS NOT HIDDEN. An ordinary admin holds none of the
    analyst codes and neither approval code, so an ordinary admin can provision
    another admin and nothing else. That is a narrow product and it is the
    honest reading of the rule. The way out is a tenant role that holds what its
    holder is meant to be able to hand on -- grantable_roles is what a screen
    calls so it offers the list that will work rather than one that refuses.
    """
    wanted = permissions_for_role(session, company_id, role_name)
    held = permissions_for_user(session, company_id, actor_user_id)
    return tuple(sorted(wanted - held))


class GrantExceedsCeiling(ValueError):
    """A grant refused because the granter does not hold what they are handing on.

    A ValueError, so a caller that only knows this module raises ValueErrors
    still catches it, and a caller that wants to tell this refusal from a bad
    argument can name it. The same shape queries.py::CrossTenantRow takes, for
    the same reason.

    It is raised rather than returned as a reason code, unlike almost everything
    else here. Nothing a user typed can produce it: provision_login answers the
    ceiling with INV_GRANT_EXCEEDS before any account exists, so reaching this is
    a defect in a caller rather than a refusal to render.
    """


#: THE PATHS THIS PRODUCT KNOWINGLY LETS PAST THE CEILING, AND THE SENTENCE THE
#: CHAIN CARRIES EVERY TIME ONE IS USED. Keyed by invitation kind, because the
#: kind is the decision -- both callers already hold one, so neither has to
#: remember to pass a flag, and a kind that is not in here is refused by default.
#:
#: A DECLARATION RATHER THAN A SILENCE. Principle 26 in docs/best-practices.html:
#: a fallback that does not announce itself is worse than the failure it hides.
#: This is that principle applied to a control rather than to a feature -- the
#: ceiling is not quietly absent on the handoff path, it is declared absent, in
#: one table, with the argument attached and an audit row written every time it
#: is used.
CEILING_WAIVED_BY_KIND: dict[str, str] = {
    INVITE_KIND_HANDOFF: (
        "a handoff exists to create the approver for a duty that has none, so a "
        "ceiling here would mean no company could ever create one: admin is the "
        "only stock role carrying user.invite and it holds neither approval "
        "code, so every handoff grants more than its inviter holds. The grant is "
        "made and recorded rather than refused. What it opens is an "
        "administrator inviting an address they control at their own domain, "
        "accepting it, and approving through it -- the same hole ADR-64 concedes "
        "for identity.py::grant_role, reached by a second route. Closing it needs "
        "a second approver on a privilege change, which this build does not have."
    ),
}


def _grant_within_ceiling(
    session: Session,
    company_id: str,
    *,
    user_id: str,
    role_name: str,
    granted_by_user_id: str,
    kind: str,
    actor: str,
    granted_at: datetime,
) -> tuple[str, ...]:
    """THE ONLY PLACE THIS MODULE GRANTS A ROLE. Returns the codes it let past.

    THE CLASS, NOT THE LINE, AND HERE IS THE LINE IT REPLACED. _grant_ceiling had
    exactly one caller, inside provision_login, and acceptance called grant_role
    for ROLE_OBLIGATION_OWNER unconditionally a few hundred lines away. The rule
    the module claimed in capitals held on one of the two paths that grant
    authority, and a reader had no way to see which. Two callers asked to
    remember one rule is how that happened, and a third path added next month
    would have missed it the same way. So the rule is not written down for
    callers to honour: it is the door they go through. Nothing in this module
    calls identity.grant_role except this function, and a test reads the module
    with ast and fails if that stops being true -- the shape queries.py's
    row_for_company takes for tenant scope, for the same reason.

    THREE OUTCOMES, AND EACH IS A DIFFERENT FACT.

      the granter holds everything the role carries -- it is granted, quietly,
      and the empty tuple comes back;

      they do not, and this kind of invitation declares itself in
      CEILING_WAIVED_BY_KIND -- it is granted, the codes they did not hold are
      written into the chain under an action of their own, and those codes come
      back so the caller can put them on the screen;

      they do not, and nothing declares this path -- GrantExceedsCeiling, and
      nothing is written at all.

    THE CEILING IS COMPUTED AGAINST WHOEVER RELEASED THE INVITATION, not against
    whoever is accepting it. The person accepting holds nothing yet -- that is
    the point of them -- so measuring against them would make the ceiling zero
    and every grant an excess. The releaser is the admin who approved a queued
    invitation where there was one, and the inviter otherwise, and both are
    accounts rather than display strings, which is what makes a ceiling
    computable at all. ADR-64 records that identity.py::grant_role cannot have a
    ceiling for exactly the opposite reason.

    WHAT IT STILL DOES NOT DO. It does not stop an administrator manufacturing an
    approver at an address they control. Refusing that would mean no duty could
    ever get an owner, which is the product. It makes the act visible at the
    moment it happens, in the record a regulator reads, naming the codes and the
    person -- which is what this codebase does everywhere it cannot refuse:
    policy.py announces DEMO_SELF_APPROVAL, permissions.py writes a conflict into
    the chain as it is created, and neither pretends to be a refusal.
    """
    _require_scope(company_id)
    if not granted_by_user_id:
        # No account to measure against is not "no excess". It is a caller that
        # cannot say who is handing this on, and a ceiling computed against
        # nobody would come back empty and read as a clean grant.
        raise GrantExceedsCeiling(
            f"nothing can grant {role_name!r} without an account to answer for "
            "it: a ceiling measured against nobody is not a ceiling. Pass the "
            "id of whoever released this invitation."
        )

    excess = _grant_ceiling(
        session, company_id, actor_user_id=granted_by_user_id, role_name=role_name
    )
    waiver = CEILING_WAIVED_BY_KIND.get(kind)
    granter = user_for_company(session, company_id, granted_by_user_id)
    named = granter.email if granter is not None else granted_by_user_id

    if excess and waiver is None:
        raise GrantExceedsCeiling(
            f"{named} cannot grant {role_name}: it carries {', '.join(excess)}, "
            "which they do not hold. An inviter may never grant more than they "
            f"hold, and no {kind} invitation is declared as an exception to that "
            "in CEILING_WAIVED_BY_KIND. Nothing was granted."
        )

    grant_role(
        session,
        company_id,
        user_id=user_id,
        role_name=role_name,
        actor=actor,
        granted_at=granted_at,
    )

    if excess:
        record_event(
            session,
            company_id=company_id,
            actor=f"person:{named}",
            action=ACTION_INVITE_CEILING_WAIVED,
            subject_type="user",
            subject_id=user_id,
            # The codes, the role, the person who released it and the argument
            # for allowing it. NEVER the token: a tamper-evident log is read by
            # more people than this table is, and a secret written into an
            # append-only record cannot be taken out again.
            reason=(
                f"{named} released {role_name}, which carries "
                f"{', '.join(excess)}, and holds none of those codes. {waiver}"
            ),
            occurred_at=granted_at,
            actor_user_id=granter.id if granter is not None else None,
            actor_kind=ACTOR_USER if granter is not None else ACTOR_SYSTEM,
        )
        session.flush()
    return excess


def grantable_roles(
    session: Session, *, company_id: str, actor: str
) -> tuple[str, ...]:
    """The roles this account may hand to somebody else. For the role picker.

    Empty for anybody who does not hold user.manage, which is the same answer
    provision_login gives them, so the screen and the write layer cannot
    disagree about who may do this.

    A read, so it audits nothing. The refusal that matters is the one
    provision_login writes when somebody asks for a role that is not here.
    """
    _require_scope(company_id)
    if not actor:
        return ()
    person = user_for_company(session, company_id, actor)
    if person is None or person.status != STATUS_ACTIVE:
        return ()
    if not user_has_permission(session, company_id, person.id, "user.manage"):
        return ()
    held = permissions_for_user(session, company_id, person.id)
    return tuple(
        role.name
        for role in roles_for_company(session, company_id)
        if permissions_for_role(session, company_id, role.name) <= held
    )


def _provisioning_actor(
    session: Session, company_id: str, actor: str, moment: datetime
) -> tuple[User | None, ProvisionOutcome | None]:
    """Resolve the admin and check user.manage. One or the other comes back.

    THE PERMISSION GATE IS app/auth/policy.py AND NOT A ROLE TEST WRITTEN HERE.
    That module is where a denial is defined, and it appends the ACCESS_DENIED
    row itself -- so this returns the refusal without auditing a second time.
    Two rows for one denial would double every count of "who was turned away",
    and the second would be this module's opinion of the first.
    """
    person = user_for_company(session, company_id, actor)
    if person is None or person.status != STATUS_ACTIVE:
        return None, _provision_refused(
            session,
            company_id,
            code=INV_NO_INVITER,
            text=(
                f"{actor!r} is not an active account in this company, so there "
                "is nobody to answer for this login. Note that `actor` here is "
                "a user id rather than a display string."
            ),
            actor=f"user:{actor}",
            actor_user_id=None,
            moment=moment,
        )
    try:
        policy.require(session, company_id, person.id, "user.manage")
    except policy.PermissionDenied as denied:
        # policy.require already wrote the ACCESS_DENIED row. Nothing further
        # is recorded here, on purpose.
        return None, ProvisionOutcome(
            reason_code=INV_NOT_PERMITTED, reason_text=denied.reason
        )
    return person, None


def provision_login(
    session: Session,
    *,
    company_id: str,
    actor: str,
    email: str,
    display_name: str,
    role: str,
    now: datetime | None = None,
) -> ProvisionOutcome:
    """An admin adds a login. Returns the invitation and the raw token, once.

    actor IS THE ADMIN'S USER ID. See the note at the head of this section.

    WHAT THIS WRITES, IN THIS ORDER, BECAUSE THE ORDER IS LOAD-BEARING. The
    account first, at STATUS_INVITED, through create_user -- the one path in
    this codebase that makes a user, so there is one place that normalises an
    address, refuses a duplicate and records that somebody was created. Then the
    role, through grant_role, so the audit chain names the admin who chose it at
    the moment they chose it. Then the invitation, carrying sha256 of a token
    this call returns and never stores.

    THE ROLE IS GRANTED NOW AND IS INERT UNTIL ACCEPTANCE. permissions_for_user
    ignores an account that is not active, so the grant exists, is visible on
    the admin screen, and confers nothing until a password is set. That is why
    there is still no role column on Invitation: holding the token never decides
    authority, it only decides whether the authority somebody already granted
    starts working.

    NO ttl ARGUMENT. The life of a credential link is INVITE_PROVISION_TTL_HOURS
    and nothing outside models.py can lengthen it.

    THE REFUSALS RUN IN A DELIBERATE ORDER. Everything about the caller and the
    role is answered before anything about the address, so somebody who may not
    grant a role cannot use this call to find out which addresses have accounts
    here. Every refusal writes nothing but an audit row: there is no path that
    leaves an account without an invitation or an invitation without an account.
    """
    _require_scope(company_id)
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    moment = _require_aware(now or _utcnow(), "now")

    admin, refusal = _provisioning_actor(session, company_id, actor, moment)
    if refusal is not None:
        return refusal
    who = f"person:{admin.email}"

    policy_switches = invite_policy(session, company_id=company_id)
    if not policy_switches.enabled.value:
        # The same switch the handoff path asks, asked in the same place. A
        # tenant that has turned invites off has turned off the emailed
        # credential link too; accounts can still be made through create_user,
        # which is the path that was always for that and involves no link.
        return _provision_refused(
            session,
            company_id,
            code=INV_DISABLED,
            text=(
                "invites are switched off for this tenant, so no login can be "
                f"provisioned. {policy_switches.enabled.announcement}"
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    if not email or not str(email).strip():
        return _provision_refused(
            session,
            company_id,
            code=INV_NO_EMAIL,
            text=(
                f"no address was given for {display_name or 'this person'}. An "
                "address built out of a name is a guess, and this is the one "
                "place a guess would email a credential link to a stranger."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )
    try:
        cleaned = normalise_email(str(email))
    except ValueError as error:
        return _provision_refused(
            session,
            company_id,
            code=INV_EMAIL_MALFORMED,
            text=(
                f"{error}. An address nothing can read is refused rather than "
                "repaired: a repaired address is a guess at who was meant."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    if not display_name or not display_name.strip():
        return _provision_refused(
            session,
            company_id,
            code=INV_NO_NAME,
            text=(
                f"no name was given for {cleaned}. An account nobody can name "
                "in a review is not attributable, and a name read off the local "
                "part of an address is a guess."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    # --- the role, and the ceiling on it, before anything about the address ---
    if not role or role_for_company(session, company_id, role) is None:
        return _provision_refused(
            session,
            company_id,
            code=INV_NO_ROLE,
            text=(
                f"{role!r} is not a role this company can grant. A login with "
                "no role decided is a login nobody has said the purpose of, and "
                "picking the narrowest one here would write a decision nobody "
                f"made. This company can grant: "
                f"{', '.join(r.name for r in roles_for_company(session, company_id))}."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    excess = _grant_ceiling(
        session, company_id, actor_user_id=admin.id, role_name=role
    )
    if excess:
        return _provision_refused(
            session,
            company_id,
            code=INV_GRANT_EXCEEDS,
            text=(
                f"{admin.display_name} cannot grant {role}: it carries "
                f"{', '.join(excess)}, which they do not hold. An inviter may "
                "never grant more than they hold, or provisioning a login is a "
                "way to reach authority nobody gave them."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    # --- now the address ---
    existing = user_by_email(session, company_id, cleaned)
    if existing is not None:
        return _provision_refused(
            session,
            company_id,
            code=INV_ALREADY_A_USER,
            text=(
                f"{cleaned} already has an account here "
                f"({existing.display_name}, {existing.status}). Grant that "
                "account another role, or reinstate it -- both are decisions "
                "about a person who is already here, and a second account would "
                "make two people out of one."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            user_id=existing.id,
        )

    outstanding = open_invitation_for_email(
        session, company_id, email=cleaned, now=moment
    )
    if outstanding is not None:
        # Reachable only through a queued handoff, which writes no account. Left
        # in anyway: the invariant this module keeps is one live token per
        # person, and a check that is currently redundant is cheaper than
        # discovering it was not.
        return _provision_refused(
            session,
            company_id,
            code=INV_ALREADY_LIVE,
            text=(
                f"{cleaned} already has a live invitation ({outstanding.id}, "
                f"{outstanding.status}). A second one would put two acceptance "
                "links in one inbox."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
        )

    person = create_user(
        session,
        company_id,
        email=cleaned,
        display_name=display_name.strip(),
        # 32 random bytes nobody keeps. An invited account is not one with a
        # blank password waiting to be guessed; it is one with an unguessable
        # password and a status that cannot sign in, and acceptance replaces
        # both.
        password=secrets.token_urlsafe(32),
        actor=who,
        status=STATUS_INVITED,
        created_at=moment,
    )
    # Through the one door, not straight to identity.grant_role. The ceiling was
    # already answered above, before any account existed, which is where a
    # refusal belongs on this path -- so this call is a backstop and comes back
    # empty. It is here anyway because the check above and the grant below are
    # forty lines apart, and forty lines is how far the handoff path drifted
    # from the same rule. A backstop that is currently redundant is cheaper than
    # discovering it was not.
    _grant_within_ceiling(
        session,
        company_id,
        user_id=person.id,
        role_name=role,
        granted_by_user_id=admin.id,
        kind=INVITE_KIND_PROVISION,
        actor=who,
        granted_at=moment,
    )

    invitation, token = _mint_provision_invitation(
        session,
        company_id,
        email=cleaned,
        admin=admin,
        person_id=person.id,
        moment=moment,
    )
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_INVITE_CREATED,
        subject_type="invitation",
        subject_id=invitation.id,
        # The address, the role and the expiry. NEVER the token: a tamper-evident
        # log is read by more people than this table is, and a secret written
        # into an append-only record cannot be taken out again.
        reason=(
            f"{admin.display_name} provisioned {display_name.strip()} "
            f"<{cleaned}> as {role}; the link runs out "
            f"{invitation.expires_at.isoformat()}"
        ),
        occurred_at=moment,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()

    return ProvisionOutcome(
        reason_code=INV_OK,
        reason_text=(
            f"{cleaned} can set a password until "
            f"{invitation.expires_at.isoformat()} and will hold {role}. "
            "Nothing here sent the mail."
        ),
        invitation=invitation,
        token=token,
        user_id=person.id,
    )


def _mint_provision_invitation(
    session: Session,
    company_id: str,
    *,
    email: str,
    admin: User,
    person_id: str,
    moment: datetime,
) -> tuple[Invitation, str]:
    """One provision invitation and its raw token. The only place either is made.

    Both provision_login and resend come through here, so there is one statement
    of how long a credential link lives and one of how its token is stored. Two
    would be two numbers to change on the day somebody shortens it, and in
    practice only one of them gets changed.
    """
    require_justification(
        kind=INVITE_KIND_PROVISION, subject_type=None, subject_id=None
    )
    token = new_invite_token()
    invitation = Invitation(
        id=_new_invitation_id(session),
        company_id=company_id,
        email=email,
        invited_by_user_id=admin.id,
        invited_at=moment,
        token_hash=hash_invite_token(token),
        expires_at=moment + timedelta(hours=INVITE_PROVISION_TTL_HOURS),
        accepted_at=None,
        status=INVITE_PENDING,
        approved_by_user_id=None,
        kind=INVITE_KIND_PROVISION,
        # No item, and that is the whole point. The justification is
        # invited_by_user_id, which is not nullable.
        subject_type=None,
        subject_id=None,
        invited_user_id=person_id,
    )
    session.add(invitation)
    session.flush()
    return invitation, token


def _live_invitations_to(
    session: Session, company_id: str, email: str
) -> list[Invitation]:
    """Every invitation to this address that has not reached a terminal word."""
    return [
        row
        for row in _invitations_to(session, company_id, email)
        if row.status
        not in (INVITE_ACCEPTED, INVITE_REVOKED, INVITE_EXPIRED, INVITE_SUPERSEDED)
    ]


def resend(
    session: Session,
    *,
    company_id: str,
    actor: str,
    invitation_id: str,
    now: datetime | None = None,
) -> ProvisionOutcome:
    """Send them a fresh link. A NEW token; the old one dies immediately.

    actor IS THE ADMIN'S USER ID. See the note at the head of this section.

    THIS IS THE SECURITY REQUIREMENT AND NOT A CONVENIENCE. "After that they
    need a fresh invite" is why nothing here touches expires_at on the standing
    row. Pushing that date forward would leave every copy of the first link --
    in the recipient's inbox, in the helpdesk ticket where they pasted it, in
    whatever archives the mail on the way past -- working for another day, and
    another, every time anybody pressed the button. So a resend WRITES A NEW ROW
    and moves the old one to INVITE_SUPERSEDED, and the old token stops working
    at that instant rather than at its own expiry.

    IT SUPERSEDES EVERY LIVE INVITATION TO THAT ADDRESS, not only the one named.
    The invariant is one live token per person, and making it depend on the
    caller naming the right row is making it depend on the caller. Each one
    superseded gets its own audit row, so the count in the log is the count of
    links that stopped working.

    AN EXPIRED INVITATION IS STILL RESENDABLE, AND IS STILL NOT EXTENDED: it is
    replaced by a new row with a new token and a fresh day. That is what "they
    need a fresh invite" means, and it is why an expired row is not refused here
    the way a withdrawn one is.

    WHAT IT REFUSES. An accepted invitation, because this is not a password
    reset -- handing a credential-setting link to a live account would be a way
    to take one over without knowing its password. A withdrawn one, because
    somebody decided that and quietly undoing it is not a resend. A handoff,
    because its item has to be re-checked before anybody is re-admitted to it.
    """
    _require_scope(company_id)
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    moment = _require_aware(now or _utcnow(), "now")

    admin, refusal = _provisioning_actor(session, company_id, actor, moment)
    if refusal is not None:
        return refusal
    who = f"person:{admin.email}"

    # THE SWITCH, ASKED HERE TOO, and it was missing until 2026-08-04. Minting a
    # provision link asks it; reissuing one did not, so turning invites off
    # stopped new credential links and left the resend button minting them. A
    # resend is a new invitation with a new token (see above), which is exactly
    # the thing the switch exists to stop.
    policy_switches = invite_policy(session, company_id=company_id)
    if not policy_switches.enabled.value:
        return _provision_refused(
            session,
            company_id,
            code=INV_DISABLED,
            text=(
                "invites are switched off for this tenant, so a replacement "
                f"link cannot be minted. {policy_switches.enabled.announcement}"
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation_id,
        )

    # The permission is checked BEFORE the id is resolved, so somebody who may
    # not do this learns nothing about which invitation ids exist.
    invitation = invitation_for_company(session, company_id, invitation_id)
    if invitation is None:
        # One message for "not here" and "not yours". Which of the two it is
        # would itself tell a caller that another tenant holds that id.
        return ProvisionOutcome(
            reason_code=INV_UNKNOWN,
            reason_text=f"no invitation {invitation_id!r} for this company",
        )

    if invitation.kind != INVITE_KIND_PROVISION:
        return _provision_refused(
            session,
            company_id,
            code=INV_NOT_PROVISION,
            text=(
                f"{invitation.id} is a {invitation.kind} invitation onto "
                f"{invitation.subject_type} {invitation.subject_id}. Whether "
                "that item still needs somebody has to be re-derived before "
                "anybody is re-admitted to it, so the fix is to withdraw this "
                "one and call invite_owner_for_escalation or "
                "invite_owner_for_obligation again."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
        )

    if invitation.status == INVITE_ACCEPTED:
        return _provision_refused(
            session,
            company_id,
            code=INV_ALREADY_ACCEPTED,
            text=(
                f"{invitation.email} accepted on "
                f"{invitation.accepted_at.isoformat()}. A credential-setting "
                "link is not a password reset, and sending one to a live account "
                "would be a way to take it over without knowing its password."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
        )
    if invitation.status == INVITE_REVOKED:
        return _provision_refused(
            session,
            company_id,
            code=INV_WAS_REVOKED,
            text=(
                f"{invitation.id} was withdrawn. Sending another link would undo "
                "that decision without anybody revisiting it; provision the "
                "login again if that is what is meant."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
        )

    # The account this invitation made must still be waiting. A person who
    # became active through some other route is past this door.
    person = (
        user_for_company(session, company_id, invitation.invited_user_id)
        if invitation.invited_user_id
        else None
    )
    if person is None:
        return _provision_refused(
            session,
            company_id,
            code=INV_UNKNOWN,
            text=(
                f"{invitation.id} names no account in this company, so there is "
                "nobody for a fresh link to belong to."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
        )
    if person.status == STATUS_ACTIVE:
        return _provision_refused(
            session,
            company_id,
            code=INV_ALREADY_ACCEPTED,
            text=(
                f"{person.email} is already an active account. A "
                "credential-setting link is not a password reset."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
            user_id=person.id,
        )
    if person.status != STATUS_INVITED:
        return _provision_refused(
            session,
            company_id,
            code=INV_ALREADY_A_USER,
            text=(
                f"{person.email} is {person.status}. Reinstating an account is a "
                "decision about a person, not a fresh link."
            ),
            actor=who,
            actor_user_id=admin.id,
            moment=moment,
            subject_id=invitation.id,
            user_id=person.id,
        )

    # Collected BEFORE the new row exists, or the new row would supersede itself.
    #
    # The named row is normally in here. It is not when some earlier resend
    # already replaced it -- somebody pressing a stale button twice -- and that
    # case is deliberately allowed rather than refused: the invariant this keeps
    # is one live token per ADDRESS, so a resend from a stale row still leaves
    # exactly one, and refusing it would only send the admin back to find a row
    # id they have no reason to care about.
    dying = _live_invitations_to(session, company_id, invitation.email)
    died = [row.id for row in dying]
    for row in dying:
        row.status = INVITE_SUPERSEDED
    session.flush()

    fresh, token = _mint_provision_invitation(
        session,
        company_id,
        email=invitation.email,
        # The admin who pressed resend, not the one who first provisioned. This
        # row records this act.
        admin=admin,
        person_id=person.id,
        moment=moment,
    )

    for row in dying:
        record_event(
            session,
            company_id=company_id,
            actor=who,
            action=ACTION_INVITE_SUPERSEDED,
            subject_type="invitation",
            subject_id=row.id,
            reason=(
                f"{row.id} is {INVITE_SUPERSEDED}: {admin.display_name} issued "
                f"{fresh.id} to {row.email}, so the earlier link stopped working "
                f"now rather than at {row.expires_at.isoformat()}"
            ),
            occurred_at=moment,
            actor_user_id=admin.id,
            actor_kind=ACTOR_USER,
        )
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_INVITE_CREATED,
        subject_type="invitation",
        subject_id=fresh.id,
        reason=(
            f"{admin.display_name} reissued the link for {person.display_name} "
            f"<{fresh.email}>; it runs out {fresh.expires_at.isoformat()}. "
            f"{len(dying)} earlier link(s) stopped working."
        ),
        occurred_at=moment,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()

    return ProvisionOutcome(
        reason_code=INV_OK,
        reason_text=(
            f"{fresh.email} has a new link until "
            f"{fresh.expires_at.isoformat()}. Every earlier link is dead."
        ),
        invitation=fresh,
        token=token,
        user_id=person.id,
        # What actually died, preferring the row the caller named.
        superseded_invitation_id=(
            invitation.id
            if invitation.id in died
            else (died[0] if died else None)
        ),
    )


# ---------------------------------------------------------------------------
# The admin screen's read
# ---------------------------------------------------------------------------


def accounts_for_company(
    session: Session, *, company_id: str, now: datetime | None = None
) -> list[AccountRow]:
    """Every account here, with its status, roles, last login and open invite.

    The one read the admin screen makes. Four queries whatever the size of the
    company, rather than four per account, because this is the screen that
    exists to show many.

    last_login_at COMES BACK AS None WHEN THERE HAS NEVER BEEN ONE. See
    AccountRow. Nothing on the way to the screen may turn that into a date or a
    string.

    WHAT IT DOES NOT SHOW, STATED RATHER THAN DISCOVERED: an invitation with no
    account behind it. A cross-domain handoff waits for a holder of user.manage
    and writes no User, so it appears in invitations_awaiting_approval and not
    here. This read is a list of accounts; the approval queue is a list of
    invitations, and merging them would make a screen that cannot say which of
    the two a row is.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")

    people = users_for_company(session, company_id)
    ids = tuple(person.id for person in people)
    held = _role_names(session, company_id, ids)

    # Newest invitation per account. Ascending, so the last write for an id wins
    # and the dict ends up holding the newest -- the same ordering
    # invitation_for_invited_user uses, spelt once for the whole company.
    newest: dict[str, Invitation] = {}
    if ids:
        for row in (
            session.query(Invitation)
            .filter(Invitation.company_id == company_id)
            .filter(Invitation.invited_user_id.in_(ids))
            .order_by(Invitation.invited_at, Invitation.id)
            .all()
        ):
            newest[row.invited_user_id] = row

    rows: list[AccountRow] = []
    for person in people:
        invitation = newest.get(person.id)
        rows.append(
            AccountRow(
                user_id=person.id,
                email=person.email,
                display_name=person.display_name,
                status=person.status,
                roles=held.get(person.id, ()),
                # Handed straight through. The absence is the fact.
                last_login_at=person.last_login_at,
                invitation_id=invitation.id if invitation else None,
                invitation_status=invitation.status if invitation else None,
                invitation_kind=invitation.kind if invitation else None,
                invitation_expires_at=invitation.expires_at if invitation else None,
                invitation_live=invitation_is_live(invitation, moment),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def accept(
    session: Session,
    *,
    token: str,
    password: str,
    now: datetime | None = None,
    ip: str = "",
) -> AcceptOutcome:
    """Set a password and become a user. Single use, for both kinds.

    ONE ACCEPTANCE PATH, NOT TWO, and that is the reason provisioning did not
    get its own. A second one would be a second place that decides whether a
    token is live, a second call to scrypt with its own idea of the cost
    parameters, and a second set of expiry comparisons to keep in step. The two
    kinds differ in three lines near the end and nowhere else.

    NO COMPANY ARGUMENT, AND WHY. This is the read that ESTABLISHES the scope,
    like app/auth/sessions.py::resolve_session. The token is the credential; the
    company comes out of the row and every read after it is scoped by that.

    THE PASSWORD GOES THROUGH identity.hash_password AND NOWHERE ELSE. There is
    one scrypt path in this codebase and a second one would be a second set of
    cost parameters to raise, which in practice means one of them never gets
    raised.

    SINGLE USE. Acceptance sets accepted_at and moves the row to
    INVITE_ACCEPTED, and invitation_is_live refuses both, so the second use of a
    token gets the same sentence as a token nobody ever issued.

    NO ROLE COMES OFF THE ROW, ON EITHER KIND. A handoff grants
    obligation_owner, named in code here, because Invitation has no role column
    and deliberately nowhere to put one. A provision grants NOTHING here -- the
    admin already granted the role when they made the decision, and this call
    only stops it being inert. Either way, holding a token never decides
    authority.
    """
    moment = _require_aware(now or _utcnow(), "now")
    dead = AcceptOutcome(reason_code=ACCEPT_REFUSED, reason_text=ACCEPT_UNAVAILABLE)
    if not token or not isinstance(token, str):
        return dead

    # Two steps, and the second decides. The query is an index probe on a hash;
    # session_token_matches then compares in constant time, which is the rule
    # this codebase holds everywhere: no `==` decides whether a secret is right.
    invitation = (
        session.query(Invitation)
        .filter(Invitation.token_hash == hash_invite_token(token))
        .one_or_none()
    )
    if invitation is None or not invite_token_matches(token, invitation.token_hash):
        return dead

    if not invitation_is_live(invitation, moment) or not invitation.invited_user_id:
        # The company IS known here, so the reason goes in the chain even
        # though it never reaches the caller. "Priya clicked the withdrawn
        # link on Tuesday" is exactly what an admin needs and exactly what the
        # person holding a dead token must not be told.
        record_event(
            session,
            company_id=invitation.company_id,
            actor=f"invitation:{invitation.id}",
            action=ACTION_INVITE_REFUSED,
            subject_type="invitation",
            subject_id=invitation.id,
            reason=(
                f"acceptance refused: status {invitation.status}, expires "
                f"{invitation.expires_at.isoformat()}, account "
                f"{invitation.invited_user_id or 'not created'}"
            ),
            occurred_at=moment,
            ip=ip or None,
        )
        session.flush()
        return dead

    person = user_for_company(
        session, invitation.company_id, invitation.invited_user_id
    )
    if person is None or person.status != STATUS_INVITED:
        return dead

    try:
        digest, salt, params = hash_password(password)
    except ValueError as error:
        # A DIFFERENT CODE FROM ACCEPT_REFUSED, on purpose. The invitation is
        # fine and the person is entitled to be told what to fix; saying "this
        # invitation is not available" for a short password would send somebody
        # back to the sender for a link that works.
        return AcceptOutcome(
            reason_code=ACCEPT_PASSWORD,
            reason_text=str(error),
            company_id=invitation.company_id,
            invitation_id=invitation.id,
        )

    person.password_hash = digest
    person.password_salt = salt
    person.kdf_params = params
    person.status = STATUS_ACTIVE
    # Deliberately NOT identity.set_user_status: that writes user.reinstated,
    # which asserts this account was suspended and let back in. It never was.
    # A false word in an append-only log cannot be taken out again.
    session.flush()

    beyond: tuple[str, ...] = ()
    if invitation.kind == INVITE_KIND_HANDOFF:
        # THE CEILING IS ANSWERED AGAINST WHOEVER RELEASED THIS, not against the
        # person clicking the link, who holds nothing yet. An approved
        # invitation was released by the admin who approved it; a direct one by
        # the inviter. Both are on the row, both are accounts rather than
        # display strings, and that is the whole reason a ceiling is computable
        # here and is not in identity.py::grant_role (ADR-64).
        #
        # This grant is expected to exceed the ceiling almost every time, and
        # that is the declared position rather than an oversight -- see
        # CEILING_WAIVED_BY_KIND. What the call adds is that the excess comes
        # back, gets written into the chain, and reaches the screen, instead of
        # a docstring three hundred lines up claiming a control that was never
        # on this path.
        beyond = _grant_within_ceiling(
            session,
            invitation.company_id,
            user_id=person.id,
            role_name=ROLE_OBLIGATION_OWNER,
            granted_by_user_id=(
                invitation.approved_by_user_id or invitation.invited_by_user_id
            ),
            kind=invitation.kind,
            actor=f"person:{person.email}",
            granted_at=moment,
        )
        granted = (ROLE_OBLIGATION_OWNER,)
        landing = f"landing on {invitation.subject_type} {invitation.subject_id}"
        settled = (
            f"{person.display_name} is now an obligation owner in "
            f"{invitation.company_id}."
        )
        if beyond:
            settled += (
                f" That role carries {', '.join(beyond)}, which whoever released "
                "this invitation does not hold themselves. The grant is on the "
                "record as invite.ceiling_waived."
            )
    else:
        # NOTHING IS GRANTED HERE. The admin granted the role at provision time,
        # through grant_role, where the chain names them. Reading it back rather
        # than restating it means this cannot report a role the account does not
        # actually hold -- and if an admin revoked it while the invitation sat,
        # the honest answer is the empty tuple.
        granted = _role_names(session, invitation.company_id, (person.id,)).get(
            person.id, ()
        )
        landing = "provisioned login, no item"
        settled = (
            f"{person.display_name} can sign in to {invitation.company_id} and "
            f"holds {', '.join(granted) if granted else 'no role'}."
        )

    invitation.status = INVITE_ACCEPTED
    invitation.accepted_at = moment
    session.flush()

    record_event(
        session,
        company_id=invitation.company_id,
        actor=f"person:{person.email}",
        action=ACTION_INVITE_ACCEPTED,
        subject_type="invitation",
        subject_id=invitation.id,
        reason=(
            f"{person.display_name} accepted and holds "
            f"{', '.join(granted) if granted else 'no role'}; {landing}"
        ),
        occurred_at=moment,
        actor_user_id=person.id,
        actor_kind=ACTOR_USER,
        ip=ip or None,
    )
    session.flush()

    return AcceptOutcome(
        reason_code=ACCEPT_OK,
        reason_text=settled,
        company_id=invitation.company_id,
        user_id=person.id,
        invitation_id=invitation.id,
        subject_type=invitation.subject_type,
        subject_id=invitation.subject_id,
        granted_roles=granted,
        granted_beyond_ceiling=beyond,
    )


def accept_invitation(
    session: Session,
    *,
    token: str,
    password: str,
    now: datetime | None = None,
    ip: str = "",
) -> AcceptOutcome:
    """The handoff spelling of accept(). Kept so its callers do not move.

    One function with two names, rather than two functions: this delegates and
    holds no logic of its own, so there is nowhere for the two to drift apart.
    New code should call accept(), which is what both kinds go through.
    """
    return accept(session, token=token, password=password, now=now, ip=ip)


def pending_handoff(
    session: Session, company_id: str, *, user_id: str, now: datetime | None = None
) -> Invitation | None:
    """The live invitation an account is waiting on, or None.

    A thin read over routing's predicate, here so a view asking "why is this
    person's name on my screen in grey" has one call to make.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")
    invitation = invitation_for_invited_user(session, company_id, user_id=user_id)
    return invitation if invitation_is_live(invitation, moment) else None


__all__ = [
    "ACCEPT_OK",
    "ACCEPT_PASSWORD",
    "ACCEPT_REASON_CODES",
    "ACCEPT_REFUSED",
    "ACCEPT_UNAVAILABLE",
    "ACTION_INVITE_ACCEPTED",
    "ACTION_INVITE_APPROVED",
    "ACTION_INVITE_CEILING_WAIVED",
    "ACTION_INVITE_CREATED",
    "ACTION_INVITE_QUEUED",
    "ACTION_INVITE_REFUSED",
    "ACTION_INVITE_REVOKED",
    "ACTION_INVITE_SUPERSEDED",
    "CEILING_WAIVED_BY_KIND",
    "AcceptOutcome",
    "AccountRow",
    "CompanyDomain",
    "GrantExceedsCeiling",
    "DOMAIN_FROM_ACCOUNTS",
    "DOMAIN_FROM_CALLER",
    "DOMAIN_UNDECIDED_NONE",
    "DOMAIN_UNDECIDED_SEVERAL",
    "ENV_INVITES_ENABLED",
    "ENV_INVITES_NEED_APPROVAL",
    "INVITE_ACTIONS",
    "INV_ALREADY_ACCEPTED",
    "INV_ALREADY_A_USER",
    "INV_ALREADY_LIVE",
    "INV_DISABLED",
    "INV_EMAIL_MALFORMED",
    "INV_EXPIRED",
    "INV_GRANT_EXCEEDS",
    "INV_NOT_A_GAP",
    "INV_NOT_PERMITTED",
    "INV_NOT_PROVISION",
    "INV_NOT_QUEUED",
    "INV_NO_EMAIL",
    "INV_NO_INVITER",
    "INV_NO_NAME",
    "INV_NO_ROLE",
    "INV_NO_SUBJECT",
    "INV_OBLIGATIONS_AMBIGUOUS",
    "INV_OK",
    "INV_QUEUED",
    "INV_REASON_CODES",
    "INV_REVOKED_OK",
    "INV_UNKNOWN",
    "INV_WAS_REVOKED",
    "NO_DOMAIN",
    "InviteOutcome",
    "InvitePolicy",
    "ProvisionOutcome",
    "SETTING_INVITES_ENABLED",
    "SETTING_INVITES_NEED_APPROVAL",
    "SOURCE_DEFAULT",
    "SOURCE_ENVIRONMENT",
    "SOURCE_TENANT",
    "Switch",
    "accept",
    "accept_invitation",
    "accounts_for_company",
    "approve_invitation",
    "company_domain",
    "grantable_roles",
    "hash_invite_token",
    "invitation_for_company",
    "invitations_awaiting_approval",
    "invitations_for_company",
    "invite_owner_for_escalation",
    "invite_owner_for_obligation",
    "invite_policy",
    "invite_token_matches",
    "live_invitation_for_email",
    "new_invite_token",
    "open_invitation_for_email",
    "pending_handoff",
    "provision_login",
    "require_justification",
    "resend",
    "revoke_invitation",
    "revoked_invitation_for_email",
    "same_organisation",
]
