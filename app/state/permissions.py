"""Any permission to any person, and every departure from the grid on the record.

Three roles are too few, and the evidence is in this repository rather than in
an opinion. data/real/ holds 102 filings by people whose titles the product
cannot express -- Analyst II Regulatory Affairs, Manager Regulatory Affairs,
Deputy Director, VP Pricing and Planning, Indiana Regulatory Counsel, Legal
Assistant -- and those are different approval authorities, not decoration. The
shortage has already produced a defect: scripts/seed_route.py has a step called
"Legal review" and a step called "Officer signs the filing" and sends BOTH to
role:admin, because no legal role and no officer role exist. So the person who
draws the route is the person who answers it, which is the exact hole the
permission grid's own comment warns about. The route is a lie told by a
vocabulary shortage, and this module is the vocabulary.

**A company that composes its own set names it.** SYSTEM_ROLE_PERMISSIONS is the
DEFAULT a tenant starts from, not a ceiling. A tenant that wants something else
composes a Role, gives it a name its own people recognise, and the register shows
what it holds. What a tenant may never do is redefine "analyst" -- a role called
analyst that has been edited into something else is worse than no role at all,
because every screen, every ADR and docs/security.html still say what analyst
means while half the company no longer holds it. create_role refuses a system
name, edit_role refuses a system role outright, and neither forks behind your
back: an implicit copy-on-write is a fallback that does not announce itself, and
the copy would need a name the product invented.

**CONFLICTS ARE REPORTED, NEVER REFUSED, and this is the judgement the whole
module rests on.** If one person holds action.propose and action.approve, that is
the company's decision. In a four-person regulatory team the administrator may
legitimately be the approver; in a large utility they must not be, and the
product cannot know which. Prior restraint would make it unusable for the small
team; silence would make it useless to the large one. So a conflict is named,
listed, written into the audit chain at the moment it is created, and readable by
the next auditor who asks. This codebase argues the same way everywhere else:
absence is denial, a fallback announces itself, and a control that cannot be
enforced is disclosed rather than implied -- app/auth/policy.py takes exactly
this shape with DEMO_SELF_APPROVAL.

**NOBODY MAY GRANT A PERMISSION THEY DO NOT THEMSELVES HOLD.** Not through a
direct grant, not by composing a role, not by editing one. An administrator holds
user.manage, and that lets them ARRANGE authority that already exists in the
company; it never mints new authority. Without this rule an admin is every role
in one click and the grid is decoration -- the admin role deliberately holds no
approval permission, and an admin who could hand action.approve to an account
they control would have handed it to themselves. invites.py::_grant_ceiling makes
the same argument for provisioned logins; this is that rule applied to the three
paths that would otherwise go round it.

**HOW A NEW COMPANY EVER COMPOSES AN APPROVING ROLE, because the ceiling looks
like a deadlock and is not.** A tenant's first administrator holds the admin
grid, which carries no approval, so they cannot compose "Certifying officer" or
hand anybody action.approve. What they can do is grant the SYSTEM role
obligation_owner, which every tenant is seeded with and which carries approval.
That person then holds action.approve; give them user.manage as a direct grant --
which an admin may, because an admin holds user.manage -- and they can compose
the approving roles the company actually needs. Authority enters a tenant through
the three roles that ship with the product, and everything after that is arranged
by somebody who already holds what they are arranging. The two steps are the
point rather than an inconvenience.

**WHERE THE CEILING DOES NOT REACH, said plainly.**
identity.py::grant_role has no ceiling and cannot have one as it stands: its
`actor` is a display string, not an account, so there is nothing to compute a
ceiling against. An admin can therefore still grant an EXISTING role -- including
obligation_owner -- to themselves or anyone else. That hole predates this module,
identity.py concedes it in its own docstring, SEGREGATION_CONFLICTS names it as
the user.manage/action.approve pair, and conflicts() is what makes it visible.
Closing it needs grant_role to take a user id and a second approver on privilege
changes, which is not built. Every function here takes a user id for its actor
precisely so this module is not a second instance of the same gap.

**Revoking, never deleting.** A revoked direct grant keeps its row and gains a
revoked_at and the account that revoked it, written together. Who could do what
in March is the question an investigation asks, and a DELETE is what makes it
unanswerable. The same argument refuses to delete a role anybody has ever held.

**What is NOT here.** No expiry: a grant for holiday cover is live until somebody
revokes it, and a register listing a grant from March is telling the truth about
a hole nobody closed. No archive flag on a role, so a role that has been held can
be edited and renamed but never removed from the list. No second approver on a
permission change -- the chain records it, which makes it visible afterwards and
prevents nothing.
"""

import uuid
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased

# THE MODULE, NOT THE NAMES ON IT, for the reason app/state/sharing.py and
# app/state/invites.py both give: tests/test_policy.py reloads app.auth.policy to
# prove the approval mode is read at import, and a reload rebinds every class in
# it. A `from ... import PermissionDenied` here would hold the class from before
# the reload while the raise inside policy.require used the one from after, and
# `except PermissionDenied` would quietly stop catching a refusal.
from app.auth import policy

# The module again rather than the names, so the four action codes below can
# prefer audit.py's spelling the moment it declares them. See ACTION CODES.
from app.state import audit
from app.state.audit import ACTION_ACCESS_DENIED, ACTOR_USER, record_event
from app.state.identity import (
    permissions_for_user,
    role_for_company,
    user_for_company,
    users_for_company,
)
from app.state.models import (
    PERMISSION_CODES,
    STATUS_ACTIVE,
    SYSTEM_ROLE_NAMES,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
    UserRole,
    conflicts_in,
    role_is_system,
)

# Imported, never copied. Same guard, same failure, one place to audit.
# row_for_company comes with it: a primary-key fetch that refuses to hand back a
# row belonging to somebody else, so the check cannot be written a fifth subtly
# different way. See _editable_role, which was the fifth site.
from app.state.queries import CrossTenantRow, _require_scope, row_for_company

# ---------------------------------------------------------------------------
# ACTION CODES
#
# Imported, not defined. app/state/audit.py owns the audited vocabulary and this
# module must not hold a second spelling of any of it: a misspelt action code
# does not fail, it writes a row that verifies perfectly and that no query for
# that action will ever return, which is worse than a missing row.
#
# These five were parked here behind getattr for the hour audit.py did not yet
# declare them, and that parking is now deleted rather than left to rot beside a
# note saying it should be. tests/test_permissions.py no longer pins the two
# together, because there is no longer a second place that could drift.
# ---------------------------------------------------------------------------

from app.state.audit import (  # noqa: E402
    ACTION_PERMISSION_GRANTED,
    ACTION_PERMISSION_REVOKED,
    ACTION_ROLE_CREATED,
    ACTION_ROLE_DELETED,
    ACTION_ROLE_EDITED,
)


# ---------------------------------------------------------------------------
# The permission this module runs on
#
# NO NEW PERMISSION CODE, and models.py argues it at length: managing permissions
# IS user.manage. A permission.grant code beside it would be a second answer to
# the question that column already answers, and the day the two disagree the call
# site that asked the wrong one wins.
# ---------------------------------------------------------------------------

MANAGE = "user.manage"

if MANAGE not in PERMISSION_CODES:
    # The same argument app/auth/policy.py makes about action.approve. Renaming
    # the code in models.py without changing it here would leave a module nobody
    # can pass and no error to say why.
    raise ValueError(
        f"{MANAGE!r} is not in PERMISSION_CODES. Every write in this module "
        "gates on a permission the product no longer defines."
    )

# How a code reached a person. Two values, named rather than spelt inline,
# because they end up on a screen and in an export.
VIA_ROLE = "role"
VIA_DIRECT = "direct"


class Grant(NamedTuple):
    """One code, and where it came from. The only question an auditor asks.

    A person holding action.approve through a role and again through a direct
    grant produces TWO of these, on purpose. Collapsing them would mean revoking
    the direct grant looked like it removed the permission when the role still
    carries it -- a failure that reads as fixed.

    reason is the justification written on a direct grant and is empty on a role
    grant, where the role's own description is the explanation. granted_by is a
    display string in both cases: the account's address for a direct grant, and
    whatever UserRole.granted_by holds for a role grant, which is a name somebody
    typed and not an identity.
    """

    code: str
    via: str
    role_name: str | None
    granted_by: str
    granted_at: datetime
    reason: str

    def sentence(self) -> str:
        """One line for a screen or an audit reason. Never a parsed value."""
        if self.via == VIA_ROLE:
            return f"{self.code} through the role {self.role_name}"
        return f"{self.code} directly ({self.reason})"


class Conflict(NamedTuple):
    """One person holding both halves of a separation, and how they got each.

    NOTHING REFUSES ON THE STRENGTH OF THIS. It is a disclosure. The pairs and
    the sentences come from SEGREGATION_CONFLICTS in app/state/models.py, which
    is the only place they are declared; a second list here would be free to
    disagree with the first, and a screen would show whichever it imported.
    """

    user_id: str
    email: str
    display_name: str
    left: str
    right: str
    why: str
    left_grants: tuple[Grant, ...]
    right_grants: tuple[Grant, ...]


# ---------------------------------------------------------------------------
# Small guards
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    """A random id, not a counter. The convention identity.py already uses."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _require_aware(moment: datetime, field: str) -> datetime:
    if not isinstance(moment, datetime):
        raise ValueError(f"{field} must be a datetime; got {moment!r}")
    if moment.tzinfo is None:
        raise ValueError(
            f"{field} must carry a timezone; a naive timestamp is a defect"
        )
    return moment


def _require_actor(actor: str) -> str:
    """The acting account's USER ID. Not a display string, and it says so.

    identity.py::grant_role takes a name here and this module cannot: it has to
    resolve the actor to an account to check what they hold, to write
    granted_by_user_id, and to attribute the audit row to an identity rather than
    to a string two people can share.

    An address is refused BY NAME rather than left to fail as a permission
    problem. Both `actor` and `user_id` are opaque user ids, so a call site that
    passes an email would otherwise get "no user 'ada@mep.example' in this
    company" -- a sentence that reads as an access-control fault and sends
    somebody looking in the wrong file.
    """
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    if "@" in actor:
        raise ValueError(
            f"actor must be a user id, not a display string; got {actor!r}. "
            "This module resolves the actor to an account to check what they "
            "hold and to attribute the audit row to an identity."
        )
    return actor


def _require_reason(reason: str, what: str) -> str:
    """Non-empty, not merely non-NULL. The column cannot check this and must.

    Every row this module writes is an exception to the grid a reviewer reads in
    SYSTEM_ROLE_PERMISSIONS. An exception nobody explained cannot be reviewed --
    it can only be counted.
    """
    if not reason or not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"a reason is required to {what}. A permission change with nothing "
            "recorded about why is the audit hole this product cannot have."
        )
    return reason.strip()


def _require_code(code: str) -> str:
    """Membership of PERMISSION_CODES, checked before the insert.

    The foreign key from user_permissions.code into permissions documents the
    join and grows with the vocabulary on every ensure_system_roles call, but
    nothing in this codebase issues PRAGMA foreign_keys=ON, so on SQLite it
    enforces nothing. This is the check that actually holds.
    """
    if not code or not isinstance(code, str) or code not in PERMISSION_CODES:
        raise ValueError(
            f"{code!r} is not a permission this product defines. The codes are "
            f"{', '.join(PERMISSION_CODES)}."
        )
    return code


def _require_codes(codes) -> tuple[str, ...]:
    """A role's permission set: unique, sorted, non-empty, all known.

    EMPTY IS REFUSED. A role that carries no permission grants nothing while
    looking like authority on every screen that lists it, which is the same lie
    as a role called analyst that is not analyst. Say which codes it holds, or do
    not compose it yet.
    """
    if codes is None:
        raise ValueError(
            "a role must say which permissions it carries; a role that carries "
            "none grants nothing and looks like authority"
        )
    wanted = tuple(sorted({_require_code(code) for code in codes}))
    if not wanted:
        raise ValueError(
            "a role must say which permissions it carries; a role that carries "
            "none grants nothing and looks like authority"
        )
    return wanted


def _require_role_name(name: str) -> str:
    """A name the company chose, and never one of the three the product ships.

    THE SHADOW IS THE QUIET EDIT. identity.py::role_for_company resolves a name
    to the company's own row first, so (company_id='MEP', name='analyst') would
    silently redefine what every grant of "analyst" means in that tenant with
    nothing on any screen to say so. models.py carries a CHECK constraint saying
    the same thing, and states plainly that the live database does not have it
    and will not until its roles table is rebuilt. This is the half that binds
    everywhere.
    """
    if not name or not isinstance(name, str) or not name.strip():
        raise ValueError(
            "a role needs a name; an unnamed permission set cannot be reviewed"
        )
    label = name.strip()
    if len(label) > 64:
        raise ValueError(
            f"a role name must be 64 characters or fewer; got {len(label)}"
        )
    if label.casefold() in {system.casefold() for system in SYSTEM_ROLE_NAMES}:
        # CASE-INSENSITIVE, AND STRICTER THAN THE CHECK CONSTRAINT ON PURPOSE.
        # SQLite compares text case-sensitively, so "Analyst" would satisfy the
        # constraint in models.py and still sit next to "analyst" on every role
        # list, one of them meaning what docs/security.html says and the other
        # meaning whatever a tenant chose. Two roles a reader cannot tell apart
        # is the same failure as one role redefined, reached by a different door.
        raise ValueError(
            f"{label!r} is a system role that ships with the product and no "
            "tenant may redefine it, in any capitalisation. Every document "
            "describes what it means, so a company-scoped role of that name "
            "would silently change what every existing grant of it confers. "
            "Compose a role with your own name for it, or fork this one."
        )
    return label


def _acting_admin(session: Session, company_id: str, actor: str) -> User:
    """Resolve the actor and check user.manage. Denial is audited by policy.

    THE GATE IS app/auth/policy.py AND NOT A ROLE TEST WRITTEN HERE. That module
    defines a denial and appends the ACCESS_DENIED row itself, so nothing is
    recorded twice -- two rows for one denial would double every count of who was
    turned away, and the second would be this module's opinion of the first.

    An unknown account, another tenant's account and a suspended one all fail the
    same way, which is the intended behaviour of an authorisation read.
    """
    policy.require(session, company_id, actor, MANAGE)
    person = user_for_company(session, company_id, actor)
    if person is None:  # pragma: no cover - policy.require refuses first
        raise ValueError(f"no user {actor!r} for this company")
    return person


def _ceiling(session: Session, company_id: str, admin: User, codes) -> tuple[str, ...]:
    """The codes being handed out that the person handing them out does not hold.

    Empty means they may. NOBODY MAY GRANT A PERMISSION THEY DO NOT THEMSELVES
    HOLD -- see the module docstring for why this is the whole security of the
    feature. It reads permissions_for_user, so a code the admin holds through a
    DIRECT grant counts: a deputy given user.manage and holding action.approve
    through their role can hand approval on, which is the case this exists for.
    """
    held = permissions_for_user(session, company_id, admin.id)
    return tuple(sorted(set(codes) - held))


def _refuse(
    session: Session,
    company_id: str,
    admin: User,
    *,
    subject_type: str,
    subject_id: str,
    code: str,
    reason: str,
):
    """Write the refusal into the existing chain, then raise. One log, not two."""
    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_ACCESS_DENIED,
        subject_type=subject_type,
        subject_id=subject_id,
        reason=reason,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    raise policy.PermissionDenied(company_id, admin.id, code, reason)


def _refuse_ceiling(
    session: Session,
    company_id: str,
    admin: User,
    missing: tuple[str, ...],
    *,
    subject_type: str,
    subject_id: str,
    what: str,
):
    reason = (
        f"{admin.email} may not {what}: they do not hold "
        f"{', '.join(missing)} themselves. user.manage arranges the authority an "
        "account already has; it never mints new authority. Nobody may grant a "
        "permission they do not hold."
    )
    _refuse(
        session,
        company_id,
        admin,
        subject_type=subject_type,
        subject_id=subject_id,
        code=missing[0],
        reason=reason,
    )


def _conflict_note(pairs: tuple[tuple[str, str, str], ...]) -> str:
    """The disclosure sentence appended to an audit reason. Empty when clean.

    Written INTO THE CHAIN at the moment the conflict is created, not only onto
    a register somebody has to open. The chain is the artefact a regulator is
    shown, and a conflict that exists only on a screen is a conflict nobody
    recorded.
    """
    if not pairs:
        return ""
    return " SEGREGATION CONFLICT: " + " ".join(
        f"holds {left} and {right} -- {why}" for left, right, why in pairs
    )


# ---------------------------------------------------------------------------
# Direct grants
# ---------------------------------------------------------------------------


def _live_direct(
    session: Session, company_id: str, user_id: str, code: str | None = None
) -> list[UserPermission]:
    """Every live direct grant for one person, optionally for one code.

    A list rather than a row even though uq_one_live_user_permission makes it at
    most one. The same argument identity.py::_active_grants makes: a direct
    write or a database built before the index could leave two, and revoking one
    would take the permission away in the interface and leave it in place in the
    check.

    The company is filtered on BOTH the user row and the permission row. Each was
    written by a different call, and a bug in either is what hands somebody
    another tenant's authority.
    """
    query = (
        session.query(UserPermission)
        .join(User, User.id == UserPermission.user_id)
        .filter(User.company_id == company_id)
        .filter(UserPermission.company_id == company_id)
        .filter(UserPermission.user_id == user_id)
        .filter(UserPermission.revoked_at.is_(None))
    )
    if code is not None:
        query = query.filter(UserPermission.code == code)
    return query.order_by(UserPermission.granted_at, UserPermission.id).all()


def direct_grants_for_company(
    session: Session,
    company_id: str,
    *,
    include_revoked: bool = False,
) -> list[UserPermission]:
    """Every exception to the grid in this tenant. The auditor's read.

    ix_user_permissions_company_code exists for this one. It is the answer to
    "who here holds action.approve outside their role", which is the question the
    exception register is built from and the first one asked in a review.

    IT DOES NOT FILTER User.status, AND THAT IS DELIBERATE. This is the record of
    what somebody granted, not a statement of what is in force. Whether a
    suspended person's grant counts is decided in one place --
    permissions_for_user -- for every call site at once. A register that hid a
    suspended person's exceptions would answer an auditor wrongly: the exception
    is still written down, and it takes effect again the day the account does.

    Live grants only by default, unlike the per-user read beside it, because this
    one answers "what is outstanding" rather than "what did this person hold".
    """
    _require_scope(company_id)
    query = (
        session.query(UserPermission)
        .join(User, User.id == UserPermission.user_id)
        .filter(User.company_id == company_id)
        .filter(UserPermission.company_id == company_id)
    )
    if not include_revoked:
        query = query.filter(UserPermission.revoked_at.is_(None))
    return query.order_by(
        UserPermission.user_id, UserPermission.code, UserPermission.id
    ).all()


def direct_grants_for_user(
    session: Session,
    company_id: str,
    user_id: str,
    *,
    include_revoked: bool = True,
) -> list[UserPermission]:
    """One person's direct grants, revoked ones included by default.

    The default matches identity.py::role_grants_for_user and for the same
    reason: the question this table answers is what somebody could do at the time
    they did something, and a list that quietly omits revoked rows answers a
    different, easier question.
    """
    _require_scope(company_id)
    if not user_id:
        return []
    query = (
        session.query(UserPermission)
        .join(User, User.id == UserPermission.user_id)
        .filter(User.company_id == company_id)
        .filter(UserPermission.company_id == company_id)
        .filter(UserPermission.user_id == user_id)
    )
    if not include_revoked:
        query = query.filter(UserPermission.revoked_at.is_(None))
    return query.order_by(UserPermission.granted_at, UserPermission.id).all()


def grant(
    session: Session,
    company_id: str,
    *,
    actor: str,
    user_id: str,
    code: str,
    reason: str,
    granted_at: datetime | None = None,
) -> UserPermission:
    """Give one person one permission outside their role, and say why.

    actor IS A USER ID. So is user_id. See _require_actor.

    THE ORDER OF THE CHECKS IS DELIBERATE. Everything about the caller is settled
    before anything about the subject, so somebody who may not grant cannot use
    this call to find out which user ids exist here -- the same ordering
    invites.py::provision_login uses.

    GRANTING A CODE THE PERSON ALREADY HOLDS DIRECTLY, WITH THE SAME REASON,
    writes nothing and returns the standing row, like grant_role. A double click
    is not a decision.

    WITH A DIFFERENT REASON IT RAISES, and the difference matters. The standing
    row's sentence is the one that actually granted the authority, and keeping it
    while reporting success would drop the new justification silently -- a
    fallback that does not announce itself, in the one field this table exists to
    carry. Revoke and grant again: both rows and both sentences then survive, and
    the history says when the reason changed and who changed it.

    A CONFLICT DOES NOT REFUSE. Where the grant leaves somebody holding both
    halves of a declared separation, the pairs and the reasons why go into the
    audit row beside the justification. That is the whole judgement of this
    module: show it, do not forbid it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    admin = _acting_admin(session, company_id, who)
    wanted = _require_code(code)
    why = _require_reason(reason, f"grant {wanted}")

    missing = _ceiling(session, company_id, admin, (wanted,))
    if missing:
        _refuse_ceiling(
            session,
            company_id,
            admin,
            missing,
            subject_type="permission",
            subject_id=wanted,
            what=f"grant {wanted}",
        )

    holder = user_for_company(session, company_id, user_id)
    if holder is None:
        raise ValueError(f"no user {user_id!r} for this company")

    standing = _live_direct(session, company_id, holder.id, wanted)
    if standing:
        if standing[0].reason.strip() == why:
            return standing[0]
        raise ValueError(
            f"{holder.email} already holds a direct grant of {wanted}, given for "
            f"a different reason: {standing[0].reason!r}. Nothing was written, "
            "because keeping the standing sentence while reporting success would "
            "drop the new one. Revoke that grant and make this one, so the "
            "history carries both."
        )

    stamp = _require_aware(granted_at or _utcnow(), "granted_at")
    row = UserPermission(
        company_id=company_id,
        user_id=holder.id,
        code=wanted,
        granted_by_user_id=admin.id,
        granted_at=stamp,
        reason=why,
        revoked_at=None,
        revoked_by_user_id=None,
    )
    session.add(row)
    session.flush()

    note = _conflict_note(
        conflicts_in(permissions_for_user(session, company_id, holder.id))
    )
    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_PERMISSION_GRANTED,
        subject_type="user",
        subject_id=holder.id,
        reason=f"granted {wanted} to {holder.email} directly: {why}.{note}",
        occurred_at=stamp,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()
    return row


def revoke(
    session: Session,
    company_id: str,
    *,
    actor: str,
    user_id: str,
    code: str,
    reason: str,
    revoked_at: datetime | None = None,
) -> UserPermission:
    """Take a direct permission away. The row stays and gains who and when.

    NO CEILING HERE, AND THAT IS DELIBERATE. Requiring the revoker to hold the
    code would mean an administrator who may not hold action.approve could not
    take it off somebody who should not have it either -- a rule that leaves
    authority in place is not a safety rule. Handing authority out is the only
    direction that escalates.

    revoked_at and revoked_by_user_id are written together. The check constraint
    on the table enforces the pair; this says so as well, because a revocation
    time with nobody behind it describes an event nobody can name.

    REVOKING A CODE THE PERSON ALSO HOLDS THROUGH A ROLE announces itself: the
    audit reason names the roles that still carry it. Otherwise the screen shows
    a revocation that changed nothing, which is the worst kind of fallback.

    Revoking a code held only through a role raises rather than passing quietly.
    The caller believed something about this person's authority that was not
    true, and the error names the role that actually carries it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    admin = _acting_admin(session, company_id, who)
    wanted = _require_code(code)
    why = _require_reason(reason, f"revoke {wanted}")

    holder = user_for_company(session, company_id, user_id)
    if holder is None:
        raise ValueError(f"no user {user_id!r} for this company")

    live = _live_direct(session, company_id, holder.id, wanted)
    if not live:
        through = [
            row.role_name
            for row in effective_permissions(session, company_id, holder.id)
            if row.code == wanted and row.via == VIA_ROLE
        ]
        detail = (
            f" They hold it through the role {', '.join(sorted(set(through)))}; "
            "revoke the role, or compose one that does not carry it."
            if through
            else ""
        )
        raise ValueError(
            f"{holder.email} holds no direct grant of {wanted}.{detail}"
        )

    stamp = _require_aware(revoked_at or _utcnow(), "revoked_at")
    for row in live:
        row.revoked_at = stamp
        row.revoked_by_user_id = admin.id
    session.flush()

    still = [
        entry.role_name
        for entry in effective_permissions(session, company_id, holder.id)
        if entry.code == wanted and entry.via == VIA_ROLE
    ]
    note = (
        f" NOTE: {holder.email} still holds {wanted} through the role "
        f"{', '.join(sorted(set(still)))}, so this revocation did not take the "
        "permission away."
        if still
        else ""
    )
    if holder.status != STATUS_ACTIVE:
        # effective_permissions reports nothing for an account that is not
        # active, so the note above cannot be computed for one. Saying which
        # account this was beats leaving a silence that reads as "held nothing
        # else" -- the roles are still granted and start conferring again the
        # moment somebody is reinstated.
        note = (
            f" NOTE: {holder.email} is {holder.status}, not active, so what "
            "else they hold is not reported here; their role grants stand and "
            "confer again on reinstatement."
        )
    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_PERMISSION_REVOKED,
        subject_type="user",
        subject_id=holder.id,
        reason=(
            f"revoked the direct grant of {wanted} from {holder.email}: "
            f"{why}.{note}"
        ),
        occurred_at=stamp,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()
    return live[0]


# ---------------------------------------------------------------------------
# A company's own roles
# ---------------------------------------------------------------------------


def composed_roles(session: Session, company_id: str) -> list[Role]:
    """The roles this company composed for itself. Never the system three.

    There is no is_custom column and there must not be: Role.company_id already
    answers it, and a boolean beside it would be free to disagree with the column
    it restates. So the filter here IS the definition, and role_is_system() is
    its read-time counterpart -- every row this returns is one that predicate
    calls false. identity.py::roles_for_company is the other list, the one a
    grant picker wants, and it includes the system three.
    """
    _require_scope(company_id)
    return (
        session.query(Role)
        .filter(Role.company_id == company_id)
        .order_by(Role.name, Role.id)
        .all()
    )


def _company_role(session: Session, company_id: str, name: str) -> Role | None:
    return (
        session.query(Role)
        .filter(Role.company_id == company_id)
        .filter(Role.name == name)
        .one_or_none()
    )


def _confusable_role(session: Session, company_id: str, name: str) -> Role | None:
    """A role this company already has whose name reads the same. None if clear.

    CASE-FOLDED, because the unique index cannot be. SQLite compares text
    case-sensitively, so "Legal Reviewer" and "legal reviewer" are two rows to
    the database and one name to everybody looking at the list -- and the two
    would carry different permissions. The same argument normalise_email makes
    about one person holding two accounts.
    """
    folded = name.casefold()
    for role in composed_roles(session, company_id):
        if role.name.casefold() == folded:
            return role
    return None


# One sentence, whichever door the caller came through. Two wordings of one
# refusal is two things to keep in step, and this is the refusal a screen shows.
_SYSTEM_ROLE_REFUSAL = (
    "{label!r} is a system role and is not editable. It does not fork behind "
    "your back either: a copy the product named would be a role with a familiar "
    "name and unfamiliar permissions. Use fork_role to start a new role from it, "
    "which asks you for a name and records where the permission set came from."
)


def _codes_on_role(session: Session, role_id: str) -> frozenset[str]:
    rows = (
        session.query(RolePermission.permission_id)
        .filter(RolePermission.role_id == role_id)
        .all()
    )
    return frozenset(code for (code,) in rows)


def _editable_role(
    session: Session,
    company_id: str,
    name: str | None = None,
    *,
    role_id: str | None = None,
) -> Role:
    """The company's own role, by name or by id, or a refusal that explains itself.

    EITHER SELECTOR, NEVER BOTH, and the id is there because a form posts an id.
    A screen that had to send the name would break the moment a company renamed a
    role, and would have to look the name up to send it. Passing both is a caller
    that has not decided which it means, so it raises rather than picking one.

    A SYSTEM ROLE IS REFUSED OUTRIGHT AND DOES NOT SILENTLY FORK. An implicit
    copy-on-write is a fallback that does not announce itself: the admin presses
    save on analyst, the product writes "analyst (copy)", and now two roles
    exist, some people hold one and some the other, and every screen and document
    still says what analyst means. Worse, the copy needs a name, and a name the
    product invented is exactly the role-called-analyst-that-is-not-analyst this
    whole feature refuses. Forking is a separate act -- fork_role -- which asks
    the company for a name and records the origin.

    Another tenant's role is not found here at all, which is the intended
    behaviour of every scoped read in this codebase.

    THE ID PATH USED TO BE A FIFTH FETCH-THEN-CHECK SITE, and it is on
    row_for_company now (ADR-70). It read the row by primary key with a bare
    `Role.id == role_id`, then compared company_id afterwards -- the exact shape
    that let app/state/routing.py::ensure_obligation hand back another tenant's
    obligation, written again in a module about who may do what.

    THE ORDER OF THE TWO REFUSALS WAS ALSO WRONG, AND THAT ONE LEAKED. The
    system-role refusal interpolates the role's name, and it ran BEFORE the
    tenant comparison. Role names are not a fixed vocabulary -- a company composes
    them and calls them what it likes -- so any row the primary key found could
    reach that message. Posting another tenant's role id at a form returned a
    sentence carrying their name for it, which is precisely the fact the scope
    exists to withhold. The tenant question is settled first now, by the
    chokepoint, and the only name this function can still print is a SYSTEM
    role's: those carry company_id IS NULL, they ship with the product, and every
    company sees the same three.
    """
    if name and role_id:
        raise ValueError(
            "name a role by its name or by its id, not both; two selectors that "
            "could disagree is a caller that has not decided which it means"
        )

    if role_id:
        if not isinstance(role_id, str):
            raise ValueError(f"a role id must be a string; got {role_id!r}")
        # Whose row is it: asked first, and asked by the one call that cannot
        # forget to ask. row_for_company raises CrossTenantRow -- a ValueError --
        # when the id belongs to somebody else, and that refusal is reworded here
        # only so the sentence matches the one the name path gives. Nothing about
        # the holder crosses, from either wording.
        try:
            found = row_for_company(session, company_id, Role, role_id)
        except CrossTenantRow:
            found = None
        if found is not None:
            return found

        # No row of this company's carries that id. Either a system role does --
        # row_for_company reads their NULL scope as nobody's and refuses them
        # along with everybody else's, which is right for a chokepoint about
        # tenancy and not the answer this caller needs -- or nothing does. The
        # lookup is restricted to the NULL scope, so the row it can find is a
        # system role and no other kind, which is what makes printing its name
        # safe.
        shared = (
            session.query(Role)
            .filter(Role.id == role_id)
            .filter(Role.company_id.is_(None))
            .one_or_none()
        )
        if shared is not None and role_is_system(shared):
            raise ValueError(_SYSTEM_ROLE_REFUSAL.format(label=shared.name))

        # One sentence for "not here" and "not yours". Which of the two it is
        # would itself tell a caller that another tenant holds that id.
        raise ValueError(f"no role {role_id!r} composed by this company")

    if not name or not isinstance(name, str) or not name.strip():
        raise ValueError("a role needs a name or an id to be found by")
    label = name.strip()
    role = _company_role(session, company_id, label)
    if role is not None:
        return role
    system = role_for_company(session, company_id, label)
    if system is not None and role_is_system(system):
        raise ValueError(_SYSTEM_ROLE_REFUSAL.format(label=label))
    raise ValueError(f"no role {label!r} composed by this company")


def _origin_role(
    session: Session,
    company_id: str,
    derived_from: str | None,
    derived_from_role_id: str | None,
) -> Role | None:
    """The role a new one was started from: by name, by id, or nothing.

    NONE MEANS COMPOSED FROM SCRATCH AND NEVER "UNKNOWN". A fork that reached
    here with neither selector would write NULL into derived_from_role_id, which
    is a false statement about where a permission set came from -- so fork_role
    resolves the origin itself and never relies on a caller remembering.

    A role from another tenant resolves to nothing, like every other scoped read.
    A system role is a legitimate origin: forking obligation_owner is the case
    this column exists for.
    """
    if derived_from and derived_from_role_id:
        raise ValueError(
            "name the role this one started from by its name or by its id, not "
            "both"
        )
    if derived_from_role_id:
        found = (
            session.query(Role)
            .filter(Role.id == derived_from_role_id)
            .filter(or_(Role.company_id.is_(None), Role.company_id == company_id))
            .one_or_none()
        )
        if found is None:
            raise ValueError(
                f"no role {derived_from_role_id!r} available to this company to "
                "start from"
            )
        return found
    if derived_from:
        found = role_for_company(session, company_id, derived_from)
        if found is None:
            raise ValueError(
                f"no role {derived_from!r} available to this company to start from"
            )
        return found
    return None


def create_role(
    session: Session,
    company_id: str,
    *,
    actor: str,
    name: str,
    codes,
    reason: str,
    description: str = "",
    derived_from: str | None = None,
    derived_from_role_id: str | None = None,
    created_at: datetime | None = None,
) -> Role:
    """Compose a role this company names, holding exactly the codes it names.

    TWO SENTENCES, AND THEY ARE NOT THE SAME ONE. `description` goes on the row
    and says what the role IS, for everyone who reads the register later.
    `reason` goes into the audit chain and says why it was composed on that day,
    which is the question asked afterwards. Neither substitutes for the other.

    derived_from is a role name and is normally left alone: NULL in
    derived_from_role_id means COMPOSED FROM SCRATCH and never "unknown". Use
    fork_role, which cannot leave it empty by accident -- a fork that claims to
    have been built from nothing is a false statement about where a permission
    set came from.

    The ceiling applies to the whole set, because the whole set is new authority
    being written down. A duplicate name is refused before the write rather than
    left to the unique index: an IntegrityError at flush aborts the transaction
    and would discard the audit rows alongside the role.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    admin = _acting_admin(session, company_id, who)
    label = _require_role_name(name)
    why = _require_reason(reason, f"compose the role {label!r}")
    wanted = _require_codes(codes)

    missing = _ceiling(session, company_id, admin, wanted)
    if missing:
        _refuse_ceiling(
            session,
            company_id,
            admin,
            missing,
            subject_type="role",
            subject_id=label,
            what=f"compose a role holding {', '.join(wanted)}",
        )

    clash = _confusable_role(session, company_id, label)
    if clash is not None:
        raise ValueError(
            f"this company already has a role called {clash.name!r}. Two roles "
            "whose names read the same carry two permission sets nobody can tell "
            "apart on a list. Edit that one, or pick another name."
        )

    origin = _origin_role(session, company_id, derived_from, derived_from_role_id)

    stamp = _require_aware(created_at or _utcnow(), "created_at")
    role = Role(
        id=_new_id("role"),
        company_id=company_id,
        name=label,
        description=description or "",
        created_by_user_id=admin.id,
        created_at=stamp,
        derived_from_role_id=origin.id if origin is not None else None,
    )
    session.add(role)
    session.flush()
    for code in wanted:
        session.add(RolePermission(role_id=role.id, permission_id=code))
    session.flush()

    lineage = (
        f" Started from {origin.name}."
        if origin is not None
        else " Composed from scratch."
    )
    note = _conflict_note(conflicts_in(wanted))
    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_ROLE_CREATED,
        subject_type="role",
        subject_id=role.id,
        reason=(
            f"composed the role {label} holding {', '.join(wanted)}: {why}."
            f"{lineage}{note}"
        ),
        occurred_at=stamp,
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()
    return role


def fork_role(
    session: Session,
    company_id: str,
    *,
    actor: str,
    source_role: str,
    name: str,
    reason: str,
    codes=None,
    description: str = "",
) -> Role:
    """Start a new role FROM an existing one, and record that it did.

    This is the act a screen offers when somebody presses edit on a system role.
    It asks the company for a name, copies the source's codes unless it is given
    others, and always sets derived_from_role_id -- "our Legal Reviewer began as
    obligation_owner and dropped audit.read" is the first question an auditor
    comparing a custom set against the defaults asks, and without the column the
    answer is a diff somebody has to do by eye.

    THE CEILING STILL APPLIES. Forking obligation_owner needs an account that
    holds action.approve. Copying a permission set is granting it.
    """
    _require_scope(company_id)
    if not source_role or not isinstance(source_role, str):
        raise ValueError("a fork needs the role it started from")
    origin = role_for_company(session, company_id, source_role)
    if origin is None:
        raise ValueError(
            f"no role {source_role!r} available to this company to start from"
        )
    return create_role(
        session,
        company_id,
        actor=actor,
        name=name,
        codes=_codes_on_role(session, origin.id) if codes is None else codes,
        reason=reason,
        description=description,
        derived_from=origin.name,
    )


def edit_role(
    session: Session,
    company_id: str,
    *,
    actor: str,
    reason: str,
    name: str | None = None,
    role_id: str | None = None,
    codes=None,
    description: str | None = None,
) -> Role:
    """Change what a composed role carries, or what it says about itself.

    The role is named EITHER by name or by role_id -- a form posts an id, a
    script reads better with a name, and _editable_role refuses both at once.

    THE SET IS ASSERTED WHOLE, not appended to -- principle 27 in
    docs/best-practices.html, the same rule ensure_system_roles follows. A code
    dropped from the intended set is deleted from the role rather than left
    behind as a permission the product no longer believes it grants.

    THE CEILING APPLIES TO WHAT IS ADDED, not to what the role already held.
    Removing a permission is not escalation, and a rule that stopped an
    administrator tidying a role they may not hold would leave more authority in
    place, not less. What an admin still cannot do is put a code into a role that
    they do not hold themselves -- which is the path that would otherwise make
    the ceiling on grant() decorative.

    An edit that changes nothing writes nothing. Re-stating a fact is not a
    decision, and a chain full of them is a chain nobody reads.

    IT DOES NOT RENAME, and that gap is named rather than half-built. A rename
    has to re-run the system-name refusal and the confusable-name check, and it
    changes the word every screen and every export uses for a set of people's
    authority. It is a separate act with its own audit line, and it is not built:
    compose the role you meant and revoke the grants of the old one.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    admin = _acting_admin(session, company_id, who)
    why = _require_reason(reason, "edit a role")
    role = _editable_role(session, company_id, name, role_id=role_id)

    held = _codes_on_role(session, role.id)
    wanted = held if codes is None else frozenset(_require_codes(codes))
    added = tuple(sorted(wanted - held))
    removed = tuple(sorted(held - wanted))
    reworded = description is not None and description != role.description

    if not added and not removed and not reworded:
        return role

    if added:
        missing = _ceiling(session, company_id, admin, added)
        if missing:
            _refuse_ceiling(
                session,
                company_id,
                admin,
                missing,
                subject_type="role",
                subject_id=role.id,
                what=f"add {', '.join(added)} to the role {role.name}",
            )

    for row in (
        session.query(RolePermission)
        .filter(RolePermission.role_id == role.id)
        .filter(RolePermission.permission_id.in_(removed))
        .all()
    ):
        session.delete(row)
    for code in added:
        session.add(RolePermission(role_id=role.id, permission_id=code))
    if description is not None:
        role.description = description
    session.flush()

    changes = []
    if added:
        changes.append(f"added {', '.join(added)}")
    if removed:
        changes.append(f"removed {', '.join(removed)}")
    if reworded:
        changes.append("rewrote its description")
    note = _conflict_note(conflicts_in(wanted))
    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_ROLE_EDITED,
        subject_type="role",
        subject_id=role.id,
        reason=(
            f"edited the role {role.name}: {'; '.join(changes)}. {why}. "
            f"It now holds {', '.join(sorted(wanted))}.{note}"
        ),
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()
    return role


def delete_role(
    session: Session,
    company_id: str,
    *,
    actor: str,
    reason: str,
    name: str | None = None,
    role_id: str | None = None,
) -> None:
    """Remove a composed role NOBODY HAS EVER HELD. Anything else is refused.

    A role somebody once held is the only record of what that grant conferred.
    Deleting it leaves every UserRole row -- live or revoked -- pointing at
    nothing, and "what could this person approve in March" stops being
    answerable. That is the question the whole revoked-never-deleted rule exists
    to keep answerable, so the honest answer is that a used role stays.

    A role another role was forked from stays too, for the same reason:
    derived_from_role_id would become a pointer to nothing, and NULL there means
    composed from scratch, so the pointer cannot simply be cleared without making
    the surviving role lie about its origin.

    THE GAP THIS LEAVES, NAMED RATHER THAN HIDDEN. There is no archive flag on a
    role, so a role that has been used can be edited and renamed but never taken
    out of the picker. An archived_at column and a filter on the list are the
    fix; they are not built, and a column nothing enforces would be worse.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    admin = _acting_admin(session, company_id, who)
    why = _require_reason(reason, "delete a role")
    role = _editable_role(session, company_id, name, role_id=role_id)

    # COUNTED BY role_id ALONE, WITHOUT THE COMPANY FILTER EVERY OTHER READ HERE
    # CARRIES, and deliberately. This is not a tenant read: it asks whether any
    # row anywhere points at the row about to be destroyed. A company filter
    # would narrow that question, and the only rows it could hide are exactly the
    # ones that should stop the delete.
    grants = session.query(UserRole).filter(UserRole.role_id == role.id).count()
    if grants:
        raise ValueError(
            f"{role.name!r} has been held by {grants} account(s) and is not "
            "deletable. The grant rows -- including the revoked ones -- are the "
            "record of what those people could do, and they are readable only "
            "while this role exists. Edit it, or revoke the grants and leave it."
        )
    derived = (
        session.query(Role).filter(Role.derived_from_role_id == role.id).count()
    )
    if derived:
        raise ValueError(
            f"{role.name!r} is where {derived} other role(s) started, and "
            "deleting it would leave them claiming an origin that no longer "
            "exists."
        )

    for row in (
        session.query(RolePermission).filter(RolePermission.role_id == role.id).all()
    ):
        session.delete(row)
    # Read off the row BEFORE it goes: the audit entry has to name what was
    # deleted, and an expired ORM object cannot be asked afterwards. Named apart
    # from the role_id parameter above rather than reusing it, so nothing later
    # can read one meaning of the word and get the other.
    deleted_id, label = role.id, role.name
    session.delete(role)
    session.flush()

    record_event(
        session,
        company_id=company_id,
        actor=admin.email,
        action=ACTION_ROLE_DELETED,
        subject_type="role",
        subject_id=deleted_id,
        reason=f"deleted the role {label}, which nobody had ever been granted: {why}",
        actor_user_id=admin.id,
        actor_kind=ACTOR_USER,
    )
    session.flush()


# ---------------------------------------------------------------------------
# The reads a register is built from
# ---------------------------------------------------------------------------


def effective_permissions(
    session: Session, company_id: str, user_id: str
) -> tuple[Grant, ...]:
    """Everything this person holds, AND WHERE EACH ONE CAME FROM.

    A screen that cannot say whether somebody has action.approve through a role
    or directly cannot answer the only question an auditor asks. So this returns
    a Grant per source rather than a set of codes: one code held two ways appears
    twice.

    The code set here and the frozenset from identity.py::permissions_for_user
    are two queries over the same rows and must agree. tests/test_permissions.py
    pins them together; if they ever differ, the gate is the one to believe and
    this one is the bug.

    An unknown user, another tenant's user, a suspended user and a user with
    nothing all return the empty tuple, exactly as permissions_for_user does.
    There is one safe direction for an authorisation read to fail in.
    """
    _require_scope(company_id)
    if not user_id:
        return ()
    holder = user_for_company(session, company_id, user_id)
    if holder is None or holder.status != STATUS_ACTIVE:
        return ()

    role_rows = (
        session.query(
            Permission.code, Role.name, UserRole.granted_by, UserRole.granted_at
        )
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .join(User, User.id == UserRole.user_id)
        .filter(User.company_id == company_id)
        .filter(User.id == user_id)
        .filter(User.status == STATUS_ACTIVE)
        .filter(UserRole.company_id == company_id)
        .filter(UserRole.revoked_at.is_(None))
        .filter(or_(Role.company_id.is_(None), Role.company_id == company_id))
        .all()
    )

    # An alias, because users appears twice in this query: the holder and the
    # account that granted. An OUTER join on the granter so a grant is never
    # dropped from a register because the account behind it cannot be resolved --
    # absence is reported, not silently removed.
    granter = aliased(User)
    direct_rows = (
        session.query(
            UserPermission.code,
            UserPermission.reason,
            UserPermission.granted_at,
            granter.email,
            UserPermission.granted_by_user_id,
        )
        .join(User, User.id == UserPermission.user_id)
        .join(Permission, Permission.id == UserPermission.code)
        .outerjoin(granter, granter.id == UserPermission.granted_by_user_id)
        .filter(User.company_id == company_id)
        .filter(User.id == user_id)
        .filter(User.status == STATUS_ACTIVE)
        .filter(UserPermission.company_id == company_id)
        .filter(UserPermission.revoked_at.is_(None))
        .all()
    )

    grants = [
        Grant(
            code=code,
            via=VIA_ROLE,
            role_name=role_name,
            granted_by=granted_by,
            granted_at=granted_at,
            reason="",
        )
        for code, role_name, granted_by, granted_at in role_rows
    ]
    grants.extend(
        Grant(
            code=code,
            via=VIA_DIRECT,
            role_name=None,
            granted_by=email or f"unknown account {granted_by_user_id}",
            granted_at=granted_at,
            reason=reason,
        )
        for code, reason, granted_at, email, granted_by_user_id in direct_rows
    )
    return tuple(
        sorted(grants, key=lambda row: (row.code, row.via, row.role_name or ""))
    )


def conflicts_for_user(
    session: Session, company_id: str, user_id: str
) -> tuple[Conflict, ...]:
    """The declared separations this one person holds both halves of.

    NOTHING IS REFUSED ON THE STRENGTH OF THIS. A small team holds conflicts
    legitimately and must still be able to work; the product's job is to say so
    plainly enough that the next auditor reads it without being told where to
    look.
    """
    _require_scope(company_id)
    holder = user_for_company(session, company_id, user_id) if user_id else None
    if holder is None or holder.status != STATUS_ACTIVE:
        return ()
    grants = effective_permissions(session, company_id, holder.id)
    if not grants:
        return ()
    return tuple(
        Conflict(
            user_id=holder.id,
            email=holder.email,
            display_name=holder.display_name,
            left=left,
            right=right,
            why=why,
            left_grants=tuple(row for row in grants if row.code == left),
            right_grants=tuple(row for row in grants if row.code == right),
        )
        for left, right, why in conflicts_in({row.code for row in grants})
    )


def conflicts(session: Session, company_id: str) -> tuple[Conflict, ...]:
    """The company's exception register: everybody holding both sides of a pair.

    Ordered by the person, because that is how it is read and how it is
    exported. A suspended account cannot appear -- it holds nothing, which is
    what suspension means here -- so the register is a statement about now, and
    reinstating somebody can put a row back into it.

    A read, so it audits nothing. The disclosure that lands in the chain is
    written at the moment the conflict is created, by grant(), create_role() and
    edit_role().

    ONE QUERY PER PERSON RATHER THAN ONE FOR THE COMPANY, and the cost is
    deliberate: this walks a tenant's user list, which is tens of rows, and it
    reuses effective_permissions so the register and the person-level screen
    cannot disagree about how somebody got a code. A single clever query would be
    a second implementation of the same rule.
    """
    _require_scope(company_id)
    found: list[Conflict] = []
    for person in users_for_company(session, company_id):
        if person.status != STATUS_ACTIVE:
            continue
        found.extend(conflicts_for_user(session, company_id, person.id))
    return tuple(found)


__all__ = [
    "ACTION_PERMISSION_GRANTED",
    "ACTION_PERMISSION_REVOKED",
    "ACTION_ROLE_CREATED",
    "ACTION_ROLE_DELETED",
    "ACTION_ROLE_EDITED",
    "MANAGE",
    "VIA_DIRECT",
    "VIA_ROLE",
    "Conflict",
    "Grant",
    "composed_roles",
    "conflicts",
    "conflicts_for_user",
    "create_role",
    "delete_role",
    "direct_grants_for_company",
    "direct_grants_for_user",
    "edit_role",
    "effective_permissions",
    "fork_role",
    "grant",
    "revoke",
]
