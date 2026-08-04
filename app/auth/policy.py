"""Authorisation, and the one control the approval model rests on.

docs/security.html states it in a sentence: the analyst who interprets a change
is never the person who approves the action that follows. Everything in this
file exists to make that sentence checkable by a machine rather than asserted by
a document.

**A permission is necessary and never sufficient.** `require` answers the
ordinary question -- does this person hold this code. `can_approve` answers the
harder one, and holding `action.approve` only gets a user past its first gate.
The second gate asks whether this user produced, authored or last touched the
claim underneath the action, and refuses if they did, whatever their permissions
say. Without that gate, approval is a form somebody fills in about their own
work. `require` therefore refuses to gate on `action.approve` at all, rather
than leaving a shorter call that looks like it would do: the shortcut is the way
the control gets retired by accident.

**Authorship comes from the audit chain.** There is no `authored_by` column, and
adding one would be a second record of who did what, kept beside the first, free
to disagree with it. The chain in app/state/audit.py already records every write
with an actor, it is hash-linked so an entry cannot be edited to move the blame,
and it is the artefact a regulator would be shown. So it is what this file
reads. Reads are not audited, only decisions, so any user-attributed row against
the claim is somebody having acted on it.

**Denial is the default, everywhere.** An unknown user, a suspended user, a user
from another tenant, an id that resolves to nothing, a permission code the
product does not define -- none of them pass. The last one raises rather than
returning False, because a code outside the vocabulary is a defect in the
calling code, and answering it either way is a guess dressed as a decision.

**The demo downgrade announces itself.** STRATA_APPROVAL_MODE=DEMO_SELF_APPROVAL
lets a single operator play both roles for a demonstration, which is what the
48-hour build actually needs. It never permits silently: the verdict carries a
reason naming the downgrade and stating what SEGREGATED would have decided, and
a row lands in the audit chain saying the separation was waived. An unrecognised
value raises at import. A security control that fails open because of a typo in
an environment variable is not a control, so a misspelt mode stops the process
at start rather than quietly running the product without its main safeguard.

**What this does NOT protect against.**

- It reads authorship from what the audit chain was told. A stolen session
  writes rows naming its victim, and both this file and the chain will believe
  them.
- It sees only acts that name the claim, the change or an escalation on that
  claim. An analyst who shaped a claim indirectly -- through a steer directive,
  a knowledge item, a conversation -- leaves no row this check can find, and
  will not be refused. Widening the net is future work, not a property of this
  code.
- An admin holds `user.manage`, so an admin can grant themselves the obligation
  owner role. The grant is in the chain, so it is visible afterwards; nothing
  here stops it. Preventing it needs a second approver on privilege changes,
  which is not built.
- One person with two accounts in the same company is two people to this check.
  They author under one address and approve under the other, and every row is
  correct. The schema allows the second account (one row per address, not one
  per human), and closing it needs identity from outside the product -- an
  enterprise directory, which docs/security.html already names as the thing
  production requires.
- Every refusal is written through the caller's session. A caller that rolls
  back loses the refusal record along with its own work.
- Nothing here rate-limits. A caller can ask the same question a thousand times,
  and in the demo mode a thousand waiver rows is what the chain will hold.
"""

import os
from typing import NamedTuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

# Imported, never restated. Two spellings of one action code drift, and the row
# written under the wrong one is invisible to every query for the right one.
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTION_ACTION_APPROVED,
    ACTION_ACTION_REJECTED,
    ACTION_APPROVAL_WAIVED,
    ACTOR_USER,
    record_event,
)
from app.state.identity import permissions_for_user, user_for_company
from app.state.models import (
    PERMISSION_CODES,
    STATUS_ACTIVE,
    AuditEvent,
    Claim,
    Escalation,
)

# The same tenant guard as every other read in the codebase, imported rather
# than copied. Two copies of a scope guard drift, and the copy nobody audited is
# the one that leaks.
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# The approval mode
# ---------------------------------------------------------------------------

ENV_APPROVAL_MODE = "STRATA_APPROVAL_MODE"

# Duties are separate. This is the product.
SEGREGATED = "SEGREGATED"
# One operator may approve their own work, loudly. This is the demonstration.
DEMO_SELF_APPROVAL = "DEMO_SELF_APPROVAL"
APPROVAL_MODES = (SEGREGATED, DEMO_SELF_APPROVAL)

# The permission the approval gate begins with. Holding it is where the check
# starts, not where it ends.
APPROVE = "action.approve"

if APPROVE not in PERMISSION_CODES:
    # Renaming the code in models.py without changing it here would leave a
    # gate nobody can pass and no error to say why: permissions_for_user would
    # simply never return the old spelling, every approval would be refused,
    # and the product would look broken rather than misconfigured. Loud at
    # import beats quiet for ever.
    raise ValueError(
        f"{APPROVE!r} is not in PERMISSION_CODES. The approval gate is checking "
        "a permission the product no longer defines."
    )


class PermissionDenied(Exception):
    """Raised by require() when a user may not do the thing they asked to do.

    Carries the company, the user and the code, so a caller that logs it does
    not have to parse the message. It does not carry the reason as structured
    data beyond that: the reason is prose meant for a person, and a caller
    branching on it would be branching on a sentence.

    Not a subclass of the builtin PermissionError, which inherits from OSError:
    an authorisation refusal is not a file system error, and `except OSError`
    around unrelated code should not swallow it.
    """

    def __init__(self, company_id: str, user_id: str, code: str, reason: str):
        super().__init__(reason)
        self.company_id = company_id
        self.user_id = user_id
        self.code = code
        self.reason = reason


class UnknownPermission(ValueError):
    """Raised when a check names a code the product does not define.

    A ValueError rather than a denial, because it is not a fact about the user.
    Someone wrote a code that does not exist, and both answers to "may they do
    it" are wrong: True hands out authority nobody granted, False hides a typo
    behind a locked door that looks correct from the outside.
    """


def _read_mode(raw: str | None) -> str:
    """Turn the environment variable into a mode, or refuse to start.

    Unset and empty both mean SEGREGATED: the safe value is what you get for
    doing nothing, and switching the control off has to be deliberate.

    Surrounding whitespace is trimmed, because a value read out of a .env file
    or a shell heredoc arrives with a newline on it often enough to matter, and
    that is a spelling of the same word. Case is not touched: `segregated` is
    not accepted for `SEGREGATED`. Accepting near-misses is how a typo becomes a
    silent downgrade, and the only near-miss that could be fatal here is the one
    that reads as the safe mode while meaning nothing.
    """
    if raw is None:
        return SEGREGATED
    value = raw.strip()
    if not value:
        return SEGREGATED
    if value not in APPROVAL_MODES:
        raise ValueError(
            f"{ENV_APPROVAL_MODE}={raw!r} is not a mode. Use one of "
            f"{', '.join(APPROVAL_MODES)}. This raises rather than falling back "
            "to the safe value: a control that a typo can switch off, in either "
            "direction, is not a control, and the operator has to know which of "
            "the two they are running."
        )
    return value


# Read once, at import. A process cannot change its approval model half way
# through a run, and a mode read per call would let a badly ordered test or a
# stray os.environ write move the boundary under a request that already started.
APPROVAL_MODE = _read_mode(os.environ.get(ENV_APPROVAL_MODE))


def approval_mode() -> str:
    """The mode this process is running under. For a UI banner, not for a gate.

    A caller must not branch on this to decide whether to allow something.
    can_approve has already applied the mode; asking again and acting on the
    answer is how the second copy of a rule ends up disagreeing with the first.
    """
    return APPROVAL_MODE


# ---------------------------------------------------------------------------
# What a check returns
# ---------------------------------------------------------------------------


class Verdict(NamedTuple):
    """An answer and the sentence behind it. Both are for display.

    __bool__ is overridden on purpose. A plain tuple is always truthy, so
    `if can_approve(...):` would pass for a refusal -- a security check that
    fails open when somebody forgets to unpack it. Here the truth value is the
    verdict, so the careless spelling and the careful one agree.
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


class Touch(NamedTuple):
    """One audit row showing this user acted on the claim. The evidence, cited.

    Returned rather than a bare True so the refusal can name the row a reviewer
    should read -- "audit seq 7" is checkable, "you worked on this" is not.

    matched_on says how the row was tied to the person: `user id` is the
    attribution recorded in the chain, `actor name` is the weaker match
    described in _authoring_touch.
    """

    seq: int
    action: str
    subject_type: str
    subject_id: str
    matched_on: str


# ---------------------------------------------------------------------------
# Ordinary permission checks
# ---------------------------------------------------------------------------


def _known_code(code: str) -> str:
    if not code or not isinstance(code, str):
        raise UnknownPermission(
            "a permission code is required; refusing an empty check"
        )
    if code not in PERMISSION_CODES:
        raise UnknownPermission(
            f"{code!r} is not a permission this product defines. The codes are "
            f"{', '.join(PERMISSION_CODES)}. A check against a code nobody "
            "grants would pass or fail by accident rather than by design."
        )
    return code


def _actor_label(holder, user_id: str) -> str:
    """The human-readable name for an audit row. Never an identity on its own."""
    if holder is not None:
        return holder.email
    return f"unknown user {user_id}" if user_id else "unknown user"


def _audit_refusal(
    session: Session,
    company_id: str,
    holder,
    user_id: str,
    *,
    subject_type: str,
    subject_id: str,
    reason: str,
) -> None:
    """Write the refusal into the existing chain. One log, not two.

    actor_user_id carries the id the caller presented, even when it resolves to
    nobody in this company. The honest record of a refused request is that
    somebody claiming to be that id was turned away; dropping the id would leave
    a row that cannot be searched for by the account being probed. The id is not
    evidence a person exists, and app/state/audit.py says the same thing about
    every other row: attribution records what the writing process was told.
    """
    record_event(
        session,
        company_id=company_id,
        actor=_actor_label(holder, user_id),
        action=ACTION_ACCESS_DENIED,
        subject_type=subject_type,
        subject_id=subject_id or "",
        reason=reason,
        actor_user_id=user_id or None,
        actor_kind=ACTOR_USER,
    )


def require(
    session: Session, company_id: str, user_id: str, permission_code: str
) -> None:
    """Let the caller through, or refuse and say so in the log. Returns nothing.

    This is the gate. It is called at the point where something is about to
    happen, not while deciding what to draw -- see has() for that.

    Every refusal appends an audit event before the exception is raised, so a
    denial is a fact in the record rather than a line in a log file that rotates
    away. The write goes through the caller's session; a caller that rolls back
    loses it, which is the same trade every other write in this codebase makes.

    An unknown permission code raises UnknownPermission and writes nothing. It
    is not a decision about the user -- the check never ran -- and a row saying
    somebody was denied something the product does not define would be a false
    statement in an append-only table.

    action.approve cannot be checked here at all, and that refusal is deliberate
    rather than an omission. It is the one code in the vocabulary whose holder
    may still be forbidden the act, so a gate that answered "yes, they hold it"
    would be the footgun that quietly retires segregation of duties. The error
    names can_approve. Rendering an approval control is a different question and
    has() answers it.
    """
    _require_scope(company_id)
    code = _known_code(permission_code)
    if code == APPROVE:
        raise ValueError(
            f"{APPROVE} is never sufficient on its own, so require() will not "
            "gate on it. Call can_approve(session, company_id, user_id, "
            "action_or_claim_id), which asks the further question this control "
            "exists for: did this person already act on the claim underneath "
            f"the action. Use has() if you only want to know who holds {APPROVE}."
        )

    holder = user_for_company(session, company_id, user_id) if user_id else None
    held = (
        permissions_for_user(session, company_id, user_id) if user_id else frozenset()
    )
    if code in held:
        return

    if holder is None:
        reason = f"no user {user_id!r} in this company, so {code} is not held"
    elif holder.status != STATUS_ACTIVE:
        reason = f"{holder.email} is {holder.status}, not active, so {code} is not held"
    else:
        reason = f"{holder.email} does not hold {code}"

    _audit_refusal(
        session,
        company_id,
        holder,
        user_id,
        subject_type="permission",
        subject_id=code,
        reason=reason,
    )
    raise PermissionDenied(company_id, user_id, code, reason)


def has(session: Session, company_id: str, user_id: str, code: str) -> bool:
    """Does this user hold this code. For rendering decisions only.

    Use it to decide whether to draw a button. Never use it as the gate in front
    of what the button does: a check made when the page was drawn is a statement
    about the past, and the grant can be revoked between the render and the
    click. require() is the gate.

    Writes nothing to the audit log. Auditing every hidden button would fill the
    chain with rows nobody decided, and a chain of noise is a chain nobody
    reads. The refusal that matters is the one require() writes.

    An unknown code raises, exactly as it does in require(), rather than hiding
    the button forever and looking correct while doing it.
    """
    _require_scope(company_id)
    _known_code(code)
    if not user_id:
        return False
    return code in permissions_for_user(session, company_id, user_id)


# ---------------------------------------------------------------------------
# Segregation of duties
# ---------------------------------------------------------------------------


# Acts that do not make somebody an author of the claim they touched.
#
# Approving or rejecting is the decision this control governs, not a
# contribution to the claim, so an approver does not become an author by
# approving. Without this, the second approval a person ever makes on a claim
# would be refused because of their first.
#
# The two authorisation codes are here for a sharper reason: they are what this
# module itself writes. Counting them would mean a user refused once could never
# be allowed again -- the refusal would become the evidence of authorship, and
# granting them the missing role afterwards would fix nothing. A control that
# feeds on its own output is not a control.
#
# Everything else that names the claim counts, including codes added after this
# was written. An allowlist would have quietly stopped seeing new kinds of
# authorship as the product grew; this way a new act is treated as authorship
# until somebody argues otherwise in this comment.
_NOT_AUTHORSHIP = (
    ACTION_ACTION_APPROVED,
    ACTION_ACTION_REJECTED,
    ACTION_ACCESS_DENIED,
    ACTION_APPROVAL_WAIVED,
)


class _Target(NamedTuple):
    """The claim an id resolves to, and every subject an author could have touched."""

    claim_id: str
    surface: tuple[tuple[str, str], ...]


def _resolve_target(
    session: Session, company_id: str, target_id: str
) -> _Target | None:
    """Find the claim under an id, and the subjects an author could have touched.

    The id may name a claim or an escalation raised against one, because a
    caller approving something holds whichever id its screen was built from. It
    resolves to None when it names neither in THIS company -- another tenant's
    claim id is indistinguishable here from an id that was never issued, which
    is the intended behaviour of every scoped read in this codebase.

    The surface is three kinds of row: the claim, the change it interprets, and
    every escalation raised against it. The claim is the interpretation, the
    escalations are its disposition, and the change is the input a materiality
    judgement would be recorded against. All three are places a person's
    judgement can be written down, and touching any of them makes them a party
    to the claim.

    These reads are private to this module rather than added to
    app/state/claims.py on purpose. That module gates claim text behind
    verified_claims() so nothing renders an unverified statement; a general
    `claim_for_company` there would be a second, easier way to reach the row.
    Here the rows never leave the function -- only ids do.
    """
    claim = (
        session.query(Claim)
        .filter(Claim.company_id == company_id)
        .filter(Claim.id == target_id)
        .one_or_none()
    )
    if claim is None:
        claim = (
            session.query(Claim)
            .join(Escalation, Escalation.claim_id == Claim.id)
            .filter(Escalation.company_id == company_id)
            .filter(Claim.company_id == company_id)
            .filter(Escalation.id == target_id)
            .one_or_none()
        )
    if claim is None:
        return None

    escalations = (
        session.query(Escalation.id)
        .filter(Escalation.company_id == company_id)
        .filter(Escalation.claim_id == claim.id)
        .order_by(Escalation.id)
        .all()
    )
    surface = [("claim", claim.id), ("change", claim.change_id)]
    surface.extend(("escalation", row_id) for (row_id,) in escalations)
    return _Target(claim.id, tuple(surface))


def _authoring_touch(
    session: Session, company_id: str, holder, surface: tuple[tuple[str, str], ...]
) -> Touch | None:
    """The earliest audit row showing this user acted on the claim, or None.

    Earliest rather than latest, because the question is who produced the thing,
    and the first act is the one that answers it.

    TWO WAYS A ROW CAN NAME A PERSON, and the weaker one is deliberate. Rows
    written since attribution exists carry actor_user_id, which is an identity.
    Rows written before it carry only `actor`, a display string somebody typed
    -- and app/state/models.py says plainly that a display string is not an
    identity, since two people can share a mailbox and a person can be renamed.
    So the name match runs only on rows with no recorded id, and it widens the
    net rather than proving anything.

    Widening is safe in exactly one direction: this function can only cause a
    refusal, never an approval. A false match refuses somebody who could have
    approved, and they can say so. A missed match approves somebody who should
    not have, and nobody finds out. Those are not the same mistake.
    """
    conditions = [
        and_(AuditEvent.subject_type == kind, AuditEvent.subject_id == subject_id)
        for kind, subject_id in surface
    ]
    rows = (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .filter(or_(*conditions))
        .filter(AuditEvent.action.notin_(_NOT_AUTHORSHIP))
        .order_by(AuditEvent.seq)
        .all()
    )

    names = {
        (holder.email or "").strip().lower(),
        (holder.display_name or "").strip().lower(),
    }
    names.discard("")

    for row in rows:
        if row.actor_user_id:
            if row.actor_user_id == holder.id:
                return Touch(
                    row.seq, row.action, row.subject_type, row.subject_id, "user id"
                )
            # An id that names somebody else settles the row. Falling through to
            # the name match here would let a shared display name override a
            # recorded identity, which is the weaker evidence beating the
            # stronger one.
            continue
        if (row.actor or "").strip().lower() in names:
            return Touch(
                row.seq, row.action, row.subject_type, row.subject_id, "actor name"
            )
    return None


def can_approve(
    session: Session, company_id: str, user_id: str, action_or_claim_id: str
) -> Verdict:
    """May this user approve the action that follows from this claim.

    Four gates, in this order, and each one refuses on its own:

    1. The user exists in this company and is active.
    2. They hold action.approve.
    3. The id names a claim or an escalation in this company.
    4. No audit row shows them acting on that claim, the change beneath it, or
       an escalation raised against it.

    Existence is checked after permission so the answer cannot be used to find
    out which claim ids exist. Somebody without action.approve learns the same
    thing for a real id and an invented one.

    Gate 4 is the control. A user can hold every permission the product defines
    and still be refused here, and that refusal is the point of the whole role
    design: approval by the person who wrote the thing is not review.

    Under DEMO_SELF_APPROVAL, gate 4 still runs and still reaches its verdict --
    the returned reason states what SEGREGATED would have decided -- but the
    verdict is overridden to allowed and a row saying the separation was waived
    goes into the chain. The other three gates are not touched by the mode: the
    downgrade waives who may approve their own work, never whether the account
    exists or holds the permission.

    Returns a Verdict, which unpacks as (allowed, reason) and is falsy when the
    answer is no. Both refusals and waived approvals are audited. A clean
    approval is not: nothing has happened yet, and the approval itself is the
    event worth recording when the caller writes it.
    """
    _require_scope(company_id)
    target_id = action_or_claim_id or ""
    holder = user_for_company(session, company_id, user_id) if user_id else None

    def refuse(reason: str) -> Verdict:
        _audit_refusal(
            session,
            company_id,
            holder,
            user_id,
            subject_type="approval",
            subject_id=target_id,
            reason=reason,
        )
        return Verdict(False, reason)

    if holder is None:
        return refuse(f"no user {user_id!r} in this company, so nothing is approvable")
    if holder.status != STATUS_ACTIVE:
        return refuse(f"{holder.email} is {holder.status}, not active")

    if APPROVE not in permissions_for_user(session, company_id, user_id):
        return refuse(f"{holder.email} does not hold {APPROVE}")

    target = _resolve_target(session, company_id, target_id)
    if target is None:
        return refuse(f"no claim or escalation {target_id!r} in this company")

    touch = _authoring_touch(session, company_id, holder, target.surface)
    if touch is None:
        return Verdict(
            True,
            f"{holder.email} holds {APPROVE} and no audit record shows them "
            f"acting on claim {target.claim_id}",
        )

    segregation = (
        f"{holder.email} acted on claim {target.claim_id} already: audit seq "
        f"{touch.seq} records {touch.action} on {touch.subject_type} "
        f"{touch.subject_id}, matched on {touch.matched_on}. The person who "
        "interprets a change does not approve the action that follows from it."
    )

    # Written as "waive only for the one mode that waives", not as "refuse only
    # under SEGREGATED". A third mode added later then refuses until somebody
    # teaches this line about it, instead of falling into the waiver branch and
    # switching the control off on the way past.
    if APPROVAL_MODE != DEMO_SELF_APPROVAL:
        return refuse(segregation)

    waived = (
        f"SEPARATION OF DUTIES WAIVED by {ENV_APPROVAL_MODE}={DEMO_SELF_APPROVAL}. "
        f"Under {SEGREGATED} this would be refused: {segregation}"
    )
    record_event(
        session,
        company_id=company_id,
        actor=_actor_label(holder, user_id),
        action=ACTION_APPROVAL_WAIVED,
        subject_type="approval",
        subject_id=target_id,
        reason=waived,
        actor_user_id=holder.id,
        actor_kind=ACTOR_USER,
    )
    return Verdict(True, waived)


__all__ = [
    "APPROVAL_MODE",
    "APPROVAL_MODES",
    "APPROVE",
    "DEMO_SELF_APPROVAL",
    "ENV_APPROVAL_MODE",
    "SEGREGATED",
    "PermissionDenied",
    "Touch",
    "UnknownPermission",
    "Verdict",
    "approval_mode",
    "can_approve",
    "has",
    "require",
]
