"""Share links: one claim or one change, readable by somebody with no account.

WHAT THIS EXISTS FOR. An analyst works out which obligations a change touches
and then has to chase the person who can approve acting on it -- and that person
sits in Legal, or Rates, or Engineering, and does not have an account here.
Today the artifact they send is a quote pasted into an email, which the
recipient has to take on faith. A share link sends the claim with its evidence
attached and re-checks the evidence in front of them.

WHAT REPLACES THE SESSION GUARD. Every other read in this codebase starts from a
signed-in person; app/web/deps.py refuses an anonymous request before any view
runs. This path deliberately steps outside that, so five things stand in the
guard's place and each of them is a row in the schema rather than a convention:

  * an unguessable token -- at least 32 random bytes, stored only as a SHA-256
    digest, so reading share_links hands nobody a working link;
  * one artifact -- one claim or one change, never a project, never a
    proceeding, never a list, enforced by the vocabulary rather than by a
    caller's good manners;
  * an expiry -- seven days by default, thirty at the most, never absent;
  * a revocation -- by the sharer or by a holder of user.manage, at any time;
  * a full audit -- every creation, every open with its address and verdict,
    every revocation, in the same hash-chained log as everything else.

THE TENANT COMES OUT OF THE ROW. A visitor on /s/<token> carries nothing, so the
lookup is by token digest alone. company_id then comes off the link, and every
read after that is scoped by it exactly like any other read. A share view that
reached past its own link's company_id would be a cross-tenant read with no
session behind it, which is the worst shape this defect can take.

VERIFICATION RE-RUNS AT OPEN TIME, EVERY TIME. Nothing rendered is stored and
served back. open_share() calls verified_claims(), which re-reads the stored
source at the cited offsets, so a source edited after the link went out takes
the statement off the page on the very next open. A recipient watching a claim
withdraw itself is the best demonstration this product has, and it costs one
read per open to keep honest.

WHAT THIS MODULE DOES NOT DO, STATED PLAINLY.

  * It does not rate-limit. A token is 32 random bytes and guessing one is not a
    realistic attack, but a flood of opens against a real token is not slowed
    here by anything.
  * It cannot tell a recipient from anybody they forwarded the link to. A bearer
    secret is a bearer secret; ShareOpen records that somebody holding it opened
    it from that address, and reading that as attendance is a stronger claim
    than the mechanism supports.
  * It does not check whether the sharer still works there at open time. The
    link outlives the session that made it, on purpose -- that is what makes it
    sendable -- and revocation and expiry are the two controls that bound it.
  * It requires only the read permission for the artifact. Reading is free and a
    share is a read handed onward, so any analyst can mint one. The controls on
    that are the expiry, the audit and the tenant switch, not a paid permission
    -- and that is a deliberate choice about the go-to-market, not an oversight.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

# The MODULE, not the names on it. tests/test_policy.py reloads app.auth.policy
# to prove the approval mode is read at import, and a reload rebinds every class
# in that module -- so a `from ... import PermissionDenied` here would hold the
# class from before the reload while the raise inside policy.require used the one
# from after. The two would not be the same class, and `except PermissionDenied`
# would stop catching a refusal. Reaching through the module resolves the name at
# call time, which is the only spelling a reload cannot split in two.
from app.auth import policy
from app.state.audit import ACTION_ACCESS_DENIED, ACTOR_SYSTEM, ACTOR_USER, record_event
from app.state.claims import (
    VerifiedClaim,
    WithheldClaim,
    change_for_company,
    verified_claims,
)
from app.state.identity import user_for_company
from app.state.models import (
    SHARE_DEFAULT_TTL_DAYS,
    SHARE_MAX_TTL_DAYS,
    SHARE_SUBJECT_CHANGE,
    SHARE_SUBJECT_CLAIM,
    SHARE_SUBJECT_TYPES,
    SHARE_TOKEN_BYTES,
    Change,
    Claim,
    ShareLink,
    ShareOpen,
)

# Imported, not copied. Same guard, same failure, one place to audit -- the
# reasoning app/state/claims.py gives for doing exactly this.
from app.state.queries import _require_scope, versions_for_company

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE THREE BELONG IN app/state/audit.py BESIDE EVERY OTHER ACTION_ CONSTANT,
# AND THEY ARE HERE ONLY BECAUSE THAT FILE IS BEING WRITTEN BY SOMEBODY ELSE
# THIS HOUR AND THIS BRANCH DOES NOT OWN IT. That file's own comment names this
# exact shape as a failure: app/web/views/review.py declared its own copies of
# two action codes and the vocabulary ended up with two homes. Whoever picks
# this up must MOVE these three lines rather than restate them, and this module
# must then import them. Two spellings of one action code is a row that verifies
# perfectly and that no query for that action ever returns.
# ---------------------------------------------------------------------------

ACTION_SHARE_CREATED = "share.created"
ACTION_SHARE_OPENED = "share.opened"
ACTION_SHARE_REVOKED = "share.revoked"

# The subject_type written on those rows. A share link, never the claim behind
# it: "what happened to this link" and "what happened to this claim" are
# different questions and a reader must be able to ask the first on its own.
SUBJECT_SHARE = "share_link"

# ---------------------------------------------------------------------------
# The public path
# ---------------------------------------------------------------------------

# Short on purpose: this URL is pasted into an email and read aloud on calls.
# app/web/views/share.py mounts exactly this and nothing else, and
# app/web/deps.py must list it as public or the feature does not exist in the
# assembled application.
SHARE_PATH_PREFIX = "/s"


def share_url(token: str) -> str:
    """Where a token is opened. One definition, used by the mint and the tests."""
    return f"{SHARE_PATH_PREFIX}/{token}"


# ---------------------------------------------------------------------------
# The one message a dead link gets
#
# Expired, revoked and unknown all end here. Three different sentences would be
# a probe: a caller holding a guess could learn which tokens are real, and a
# caller holding a withdrawn link could learn that it was withdrawn rather than
# never issued. The page says what a person needs -- ask the sender -- and
# nothing about which of the three happened.
# ---------------------------------------------------------------------------

SHARE_UNAVAILABLE = "This link is not available"
SHARE_UNAVAILABLE_TEXT = (
    "Shared links expire, and the person who sent this one can withdraw it at "
    "any time. Ask them for a new one."
)

# What the audit row says when a live link points at something that can no
# longer be read. The page shows a refusal, exactly as every other surface does
# when a source cannot be read.
SUBJECT_GONE = "the shared item could not be read for this company"


# ---------------------------------------------------------------------------
# The tenant switch, and the fallback that announces itself
# ---------------------------------------------------------------------------

SETTING_SOURCE_DEFAULT = "default"
SETTING_SOURCE_TENANT = "tenant"

# What a tenant that has never been asked gets. On, because a product nobody can
# share is not the product this is -- and the note below says so out loud rather
# than letting a permissive default pass for a decision.
SHARING_ON_BY_DEFAULT = True

_DEFAULT_NOTE = (
    "Sharing is on by default. Nobody has set it for this company: there is no "
    "settings table yet, so this is the product's default rather than a "
    "decision anybody made."
)


@dataclass(frozen=True, slots=True)
class Setting:
    """A switch, and where its value came from.

    The value alone is not enough and that is the whole point of this type. A
    missing row is not "off" and not "on", it is nobody having decided, and an
    interface that renders an unqualified "on" for a default has hidden the
    difference. Section 26 of docs/best-practices.html is about exactly this,
    and a security switch that silently defaults to permissive is its worst
    instance.
    """

    value: bool
    source: str
    note: str

    def __bool__(self) -> bool:
        return self.value


def sharing_enabled(session: Session, *, company_id: str) -> Setting:
    """Whether this company may share at all, and on whose authority.

    THE ONE FUNCTION EVERY SHARING PATH ASKS. Minting a link asks it, and so
    does opening one -- a switch that only stops new links would leave every
    link already in somebody's inbox working, which is not what "sharing is off"
    means to the person who turned it off.

    It answers from a default today, and says so in the value it returns. When
    company_settings lands (the proposal is at the foot of app/state/models.py)
    this function reads the row and returns SETTING_SOURCE_TENANT; nothing else
    changes, because there is no literal True at any call site to find.
    """
    _require_scope(company_id)
    return Setting(
        value=SHARING_ON_BY_DEFAULT,
        source=SETTING_SOURCE_DEFAULT,
        note=_DEFAULT_NOTE,
    )


# ---------------------------------------------------------------------------
# Tokens. The same scheme as a session token, for the same reasons.
# ---------------------------------------------------------------------------


def new_share_token() -> str:
    """A bearer secret for one link. Shown once, to the sharer, never stored."""
    return secrets.token_urlsafe(SHARE_TOKEN_BYTES)


def hash_share_token(token: str) -> str:
    """SHA-256 of a share token, which is what ShareLink.token_hash holds.

    A plain digest rather than a KDF, for the reason app/state/identity.py gives
    for session tokens: the input is 32 random bytes this process generated, so
    there is no dictionary to run against it and a slow hash would only tax
    every open. Never use this on a password.
    """
    if not token or not isinstance(token, str):
        raise ValueError("a share token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def share_token_matches(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored digest."""
    if not token or not token_hash:
        return False
    return secrets.compare_digest(hash_share_token(token), token_hash)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    """A random id, not a counter.

    models.py illustrates these as SHL-0001. A counter needs a read of the
    table's tail and a write, and two links minted in the same second would race
    for one number -- so this follows the convention app/state/projects.py and
    app/state/identity.py already use. The id is not a secret and never opens
    anything; the token does that.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Resolving a subject
#
# A claim is read here rather than in app/state/claims.py because that file has
# no single-claim read and this branch does not own it. The join and the double
# company filter are copied from claims_for_change deliberately -- a claim row
# carries its own company_id, and a write bug that stamped the wrong one would
# otherwise hand this unauthenticated page another tenant's claim through the
# very column meant to prevent it. WHOEVER OWNS app/state/claims.py should take
# claim_for_company() there and this module should import it.
# ---------------------------------------------------------------------------


def claim_for_company(session: Session, company_id: str, claim_id: str) -> Claim | None:
    """One claim, or None. Another company's claim id resolves to None."""
    _require_scope(company_id)
    return (
        session.query(Claim)
        .join(Change, Claim.change_id == Change.id)
        .filter(Claim.company_id == company_id)
        .filter(Change.company_id == company_id)
        .filter(Claim.id == claim_id)
        .one_or_none()
    )


# Which permission a subject type needs before it can be handed onward. Reading
# is free and both of these are free codes; the pricing boundary is not crossed
# by sharing, and nothing on the shared page can act.
_SUBJECT_PERMISSION = {
    SHARE_SUBJECT_CLAIM: "claim.read",
    SHARE_SUBJECT_CHANGE: "change.read",
}

# The permission an administrator holds. Revoking somebody else's link is an
# administrative act over an account's output, which is what this code means.
ADMIN_PERMISSION = "user.manage"


def _require_subject_type(subject_type: str) -> str:
    if subject_type not in SHARE_SUBJECT_TYPES:
        raise ValueError(
            f"a share is one claim or one change; {subject_type!r} is neither. "
            f"The vocabulary is {', '.join(SHARE_SUBJECT_TYPES)}. A link whose "
            "scope is a collection cannot state what it is showing, and the "
            "recipient cannot tell what was left out of it."
        )
    return subject_type


def _subject_exists(session: Session, company_id: str, subject_type: str, subject_id: str) -> bool:
    if subject_type == SHARE_SUBJECT_CLAIM:
        return claim_for_company(session, company_id, subject_id) is not None
    return change_for_company(session, company_id, subject_id) is not None


def _ttl(ttl_days: int | None) -> timedelta:
    """Turn a requested life into a span, or refuse it.

    Refuses zero and refuses anything past the maximum, in both cases rather
    than clamping. A link that quietly came back with a different expiry from
    the one asked for is a fallback that does not announce itself, and the
    person who asked for sixty days needs to be told thirty is the ceiling.
    """
    days = SHARE_DEFAULT_TTL_DAYS if ttl_days is None else ttl_days
    if not isinstance(days, int) or isinstance(days, bool):
        raise ValueError(f"ttl_days must be a whole number of days; got {days!r}")
    if days < 1:
        raise ValueError(
            f"a share link needs a life; {days} days is not one. The shortest "
            "link this product issues lasts a day."
        )
    if days > SHARE_MAX_TTL_DAYS:
        raise ValueError(
            f"{days} days is past the {SHARE_MAX_TTL_DAYS} day maximum. An "
            "unauthenticated link that outlives the question it answered is a "
            "standing hole nobody remembers opening."
        )
    return timedelta(days=days)


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MintedShare:
    """A new link, and the only time its token is ever readable.

    The token is here because the caller has to show it to the person who made
    the link. It is not on the row, it is not in the audit entry, and there is
    no way to get it back afterwards -- a lost link is revoked and re-minted.
    """

    share_id: str
    token: str
    url: str
    expires_at: datetime
    subject_type: str
    subject_id: str
    setting: Setting


def create_share_link(
    session: Session,
    *,
    company_id: str,
    user_id: str,
    subject_type: str,
    subject_id: str,
    ttl_days: int | None = None,
    now: datetime | None = None,
) -> MintedShare:
    """Mint a link to one artifact. Refuses everything it cannot check.

    THE ORDER OF THE CHECKS IS PART OF THE DESIGN. The shape of the request
    first -- scope, life, timestamp, vocabulary -- because none of those answers
    say anything about this company's data. Then the tenant switch. Then the
    read permission. The existence of the subject comes LAST, so a caller who
    may not read claims never learns which claim ids exist by the difference
    between "denied" and "no such claim".

    The subject check is not a nicety. A link minted against an id that is not
    this company's would be an unauthenticated door onto a 404 at best and, if
    that id later existed here, a door onto a row nobody meant to share.
    """
    _require_scope(company_id)
    if not user_id:
        raise ValueError("a share link records who made it; user_id is required")

    life = _ttl(ttl_days)
    stamp = now or _utcnow()
    if stamp.tzinfo is None:
        raise ValueError("now must carry a timezone; a naive timestamp is a defect")
    _require_subject_type(subject_type)

    switch = sharing_enabled(session, company_id=company_id)
    if not switch.value:
        raise ValueError(f"sharing is off for this company ({switch.note})")

    policy.require(session, company_id, user_id, _SUBJECT_PERMISSION[subject_type])

    if not _subject_exists(session, company_id, subject_type, subject_id):
        raise ValueError(
            f"no {subject_type} with id {subject_id!r} for this company; "
            "refusing to mint a link onto nothing"
        )

    token = new_share_token()
    link = ShareLink(
        id=_new_id("SHL"),
        company_id=company_id,
        token_hash=hash_share_token(token),
        subject_type=subject_type,
        subject_id=subject_id,
        created_by_user_id=user_id,
        created_at=stamp,
        expires_at=stamp + life,
        revoked_at=None,
        revoked_by_user_id=None,
        open_count=0,
        last_opened_at=None,
    )
    session.add(link)

    holder = user_for_company(session, company_id, user_id)
    record_event(
        session,
        company_id=company_id,
        actor=holder.email if holder is not None else user_id,
        action=ACTION_SHARE_CREATED,
        subject_type=SUBJECT_SHARE,
        subject_id=link.id,
        # The reason names the artifact and the expiry, and never the token.
        # app/state/audit.py: never pass a secret to this function.
        reason=(
            f"shared {subject_type} {subject_id} until "
            f"{link.expires_at.isoformat()} ({switch.source} setting)"
        ),
        occurred_at=stamp,
        actor_user_id=user_id,
        actor_kind=ACTOR_USER,
    )
    session.flush()

    return MintedShare(
        share_id=link.id,
        token=token,
        url=share_url(token),
        expires_at=link.expires_at,
        subject_type=subject_type,
        subject_id=subject_id,
        setting=switch,
    )


# ---------------------------------------------------------------------------
# Revoking
# ---------------------------------------------------------------------------


def revoke_share_link(
    session: Session,
    *,
    company_id: str,
    share_id: str,
    user_id: str,
    now: datetime | None = None,
) -> ShareLink:
    """Withdraw a link. The sharer, or a holder of user.manage. Nobody else.

    Deletes nothing: the row stays so the opens recorded under it stay attached
    to something, and so that "who withdrew this, and when" has an answer.

    Revoking an already revoked link is a no-op that writes nothing. A second
    click is not a second decision, and a second audit row would make one look
    like two.
    """
    _require_scope(company_id)
    link = (
        session.query(ShareLink)
        .filter(ShareLink.company_id == company_id)
        .filter(ShareLink.id == share_id)
        .one_or_none()
    )
    if link is None:
        # Same answer for "not here" and "not yours". Which of the two it is
        # would itself tell a caller that another tenant holds that id.
        raise ValueError(f"no share link {share_id!r} for this company")

    if link.created_by_user_id != user_id and not policy.has(
        session, company_id, user_id, ADMIN_PERMISSION
    ):
        holder = user_for_company(session, company_id, user_id)
        reason = (
            f"{holder.email if holder else user_id} neither created share "
            f"{share_id} nor holds {ADMIN_PERMISSION}"
        )
        record_event(
            session,
            company_id=company_id,
            actor=holder.email if holder is not None else user_id,
            action=ACTION_ACCESS_DENIED,
            subject_type=SUBJECT_SHARE,
            subject_id=share_id,
            reason=reason,
            actor_user_id=user_id or None,
            actor_kind=ACTOR_USER,
        )
        raise policy.PermissionDenied(company_id, user_id, ADMIN_PERMISSION, reason)

    if link.revoked_at is not None:
        return link

    stamp = now or _utcnow()
    link.revoked_at = stamp
    link.revoked_by_user_id = user_id

    holder = user_for_company(session, company_id, user_id)
    record_event(
        session,
        company_id=company_id,
        actor=holder.email if holder is not None else user_id,
        action=ACTION_SHARE_REVOKED,
        subject_type=SUBJECT_SHARE,
        subject_id=link.id,
        reason=(
            f"withdrew the link to {link.subject_type} {link.subject_id} "
            f"after {link.open_count} open(s)"
        ),
        occurred_at=stamp,
        actor_user_id=user_id,
        actor_kind=ACTOR_USER,
    )
    session.flush()
    return link


# ---------------------------------------------------------------------------
# Reading the registry. Both scoped, both joining share_links for the tenant.
# ---------------------------------------------------------------------------


def share_links_for_subject(
    session: Session, *, company_id: str, subject_type: str, subject_id: str
) -> list[ShareLink]:
    """What has been shared of this artifact. The read behind a revoke button."""
    _require_scope(company_id)
    _require_subject_type(subject_type)
    return (
        session.query(ShareLink)
        .filter(ShareLink.company_id == company_id)
        .filter(ShareLink.subject_type == subject_type)
        .filter(ShareLink.subject_id == subject_id)
        .order_by(ShareLink.created_at, ShareLink.id)
        .all()
    )


def opens_for_share(
    session: Session, *, company_id: str, share_id: str
) -> list[ShareOpen]:
    """One link's opens, oldest first. Tenancy comes from the join, not the row.

    share_opens carries no company_id on purpose, so this join is the only thing
    scoping it -- the same arrangement passages_for_company relies on. A query
    over this table that does not join share_links is an unscoped read of who
    opened what.
    """
    _require_scope(company_id)
    return (
        session.query(ShareOpen)
        .join(ShareLink, ShareOpen.share_id == ShareLink.id)
        .filter(ShareLink.company_id == company_id)
        .filter(ShareOpen.share_id == share_id)
        .order_by(ShareOpen.id)
        .all()
    )


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharedSource:
    """A stored source the shared page needs in order to show a quote in place."""

    version_id: str
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class OpenedShare:
    """What one open of one link found, at that moment.

    Carries VerifiedClaim and WithheldClaim straight through from
    app/state/claims.py rather than re-wrapping them. That is deliberate:
    WithheldClaim has no statement field and, being slotted, cannot be given
    one, and copying its fields into a new type here would be the one place that
    property could be lost. A template cannot render what the object does not
    have.

    `verified` is the single verdict recorded against this open, and it is true
    only when the page asserted something AND nothing on it was withheld. Two
    consequences worth stating, because one boolean has to stand for a page:

      * a change carrying one good claim and one refusal records false. What
        the recipient was shown included a refusal, and the pessimistic reading
        is the honest one;
      * a change nobody wrote a claim about also records false. The recipient
        was shown no verified statement, which is what false means here.
        Recording true for a page that asserted nothing would file an absence
        as a measurement.
    """

    share_id: str
    company_id: str
    subject_type: str
    subject_id: str
    shared_by: str
    shared_by_email: str
    shared_at: datetime
    expires_at: datetime
    opened_at: datetime
    verified: bool
    heading: str
    source_label: str
    verified_claims: tuple[VerifiedClaim, ...]
    withheld_claims: tuple[WithheldClaim, ...]
    sources: dict[str, SharedSource]


def _live(link: ShareLink, now: datetime) -> bool:
    return link.revoked_at is None and link.expires_at > now


def open_share(
    session: Session, *, token: str, ip: str = "", now: datetime | None = None
) -> OpenedShare | None:
    """Resolve a token and re-verify what it points at. None for a dead link.

    ONE ANSWER FOR THREE QUESTIONS. Expired, revoked, unknown -- and a tenant
    that has switched sharing off -- all return None, and the caller renders one
    response for all of them. A caller that could tell them apart would hold a
    probe for which tokens are real.

    THE VERDICT IS COMPUTED HERE AND NOW. verified_claims() re-reads the stored
    source at the cited offsets during this call. Nothing rendered is cached and
    there is no stored verdict to go stale, which is what makes a source edited
    after the link went out take the statement off the page on the next open.

    WHAT IS WRITTEN. A ShareOpen row recording what this open showed, the two
    convenience counters on the link, and one audit entry. A read that writes is
    normally a defect; here the record of who was shown what is the feature, and
    it is the reason this function is not on a read-only path.

    A DEAD LINK WRITES NO ShareOpen ROW, and that is a decision rather than an
    omission. ShareOpen.verified records whether the check passed at that open;
    a dead link never reaches the check, so False there would file "never ran"
    as "ran and failed" -- the exact conflation the column's NOT NULL exists to
    stop. The refusal goes into the audit chain as access.denied, where there is
    a word for it. An unknown token writes nothing anywhere, because there is no
    tenant to attribute it to and record_event refuses an unscoped entry: a
    fabricated company in a tamper-evident log is worse than a gap.
    """
    stamp = now or _utcnow()
    if stamp.tzinfo is None:
        # An expiry compared against a naive clock is a comparison that either
        # raises or, worse, silently compares two different timelines. A link
        # that opens after it died is the failure this refuses.
        raise ValueError("now must carry a timezone; a naive timestamp is a defect")
    if not token or not isinstance(token, str):
        return None

    link = (
        session.query(ShareLink)
        .filter(ShareLink.token_hash == hash_share_token(token))
        .one_or_none()
    )
    # The digest is unique, so this resolves to one row or none. The comparison
    # is made again in constant time against the row that came back, exactly as
    # resolve_session does: a SQL equality on a digest is not a comparison this
    # module should be seen to rely on alone.
    if link is None or not share_token_matches(token, link.token_hash):
        return None

    company_id = link.company_id

    if not _live(link, stamp) or not sharing_enabled(
        session, company_id=company_id
    ).value:
        record_event(
            session,
            company_id=company_id,
            actor="share recipient",
            action=ACTION_ACCESS_DENIED,
            subject_type=SUBJECT_SHARE,
            subject_id=link.id,
            reason="opened a link that is expired, withdrawn, or off for this company",
            occurred_at=stamp,
            actor_kind=ACTOR_SYSTEM,
            ip=ip or None,
        )
        session.flush()
        return None

    opened = _read_subject(session, link, stamp)

    # Incremented in SQL rather than in Python. Two opens landing together
    # would otherwise both read the same number and both write it plus one,
    # and the count would drift below the rows in share_opens. It is only a
    # convenience -- share_opens is the evidence -- but a counter that is
    # quietly wrong is worse than one that is obviously derived.
    link.open_count = ShareLink.open_count + 1
    link.last_opened_at = stamp
    session.add(
        ShareOpen(
            share_id=link.id,
            opened_at=stamp,
            ip=ip or "",
            verified=opened.verified,
        )
    )
    record_event(
        session,
        company_id=company_id,
        # A bearer token is not a person. The actor is the system answering a
        # request that carried no identity, and attributing it to the sharer
        # would be a false attribution in the one log that must not hold one.
        actor="share recipient",
        action=ACTION_SHARE_OPENED,
        subject_type=SUBJECT_SHARE,
        subject_id=link.id,
        reason=(
            "opened; every claim verified against the source at open time"
            if opened.verified
            else "opened; the statement was withheld, the refusal was shown"
        ),
        occurred_at=stamp,
        actor_kind=ACTOR_SYSTEM,
        ip=ip or None,
    )
    session.flush()
    return opened


def _read_subject(session: Session, link: ShareLink, stamp: datetime) -> OpenedShare:
    """Re-verify the artifact behind a live link. Scoped by the link's company.

    A subject that has gone -- deleted, or never this company's -- is treated
    the way claims.py treats a source it cannot read: as a withholding, with the
    reason shown and no statement. The link was live and somebody opened it, so
    the open is real and gets its row; there was simply nothing to assert.
    """
    company_id = link.company_id

    change_id = link.subject_id
    keep: str | None = None
    if link.subject_type == SHARE_SUBJECT_CLAIM:
        claim = claim_for_company(session, company_id, link.subject_id)
        if claim is None:
            return _nothing_to_show(session, link, stamp)
        change_id = claim.change_id
        keep = claim.id

    change = change_for_company(session, company_id, change_id)
    if change is None:
        return _nothing_to_show(session, link, stamp)

    good, bad = verified_claims(session, company_id, change_id)
    if keep is not None:
        # ONE CLAIM. The sibling claims on the same change never enter this
        # page, so no template mistake can put one there.
        good = [item for item in good if item.claim_id == keep]
        bad = [item for item in bad if item.claim_id == keep]

    verified, withheld = tuple(good), tuple(bad)

    versions = {
        version.id: version for version in versions_for_company(session, company_id)
    }

    # A verified claim whose source is not in this company's versions cannot
    # happen -- verified_claims built its verdict from exactly this read -- and
    # if it ever does, the page refuses whole rather than dropping the claim
    # out of the list. A claim that silently fails to render on a page whose
    # job is to show claims is the quietest failure this surface could have.
    if any(item.citation_version_id not in versions for item in verified):
        return _nothing_to_show(session, link, stamp)

    sources = {
        item.citation_version_id: SharedSource(
            version_id=item.citation_version_id,
            label=versions[item.citation_version_id].label,
            text=versions[item.citation_version_id].source_text,
        )
        for item in verified
    }

    cited = next((item.citation_version_id for item in verified + withheld), "")

    return OpenedShare(
        share_id=link.id,
        company_id=company_id,
        subject_type=link.subject_type,
        subject_id=link.subject_id,
        shared_by=_sharer_name(session, link),
        shared_by_email=_sharer_email(session, link),
        shared_at=link.created_at,
        expires_at=link.expires_at,
        opened_at=stamp,
        verified=bool(verified) and not withheld,
        heading=f"Section {change.section}" if change.section else "Cited passage",
        source_label=versions[cited].label if cited in versions else "",
        verified_claims=verified,
        withheld_claims=withheld,
        sources=sources,
    )


def _nothing_to_show(
    session: Session, link: ShareLink, stamp: datetime
) -> OpenedShare:
    """A live link onto an artifact that can no longer be read.

    Not the dead-link answer: the link is real and live, and the honest thing to
    show its holder is that the item behind it cannot be read now. Empty on both
    claim lists, so the page has a refusal to render and no statement to leak --
    and the watermark still names the sender, because the recipient's next move
    is to go back to them.
    """
    return OpenedShare(
        share_id=link.id,
        company_id=link.company_id,
        subject_type=link.subject_type,
        subject_id=link.subject_id,
        shared_by=_sharer_name(session, link),
        shared_by_email=_sharer_email(session, link),
        shared_at=link.created_at,
        expires_at=link.expires_at,
        opened_at=stamp,
        verified=False,
        heading=SUBJECT_GONE,
        source_label="",
        verified_claims=(),
        withheld_claims=(),
        sources={},
    )


def _sharer(session: Session, link: ShareLink):
    return user_for_company(session, link.company_id, link.created_by_user_id)


def _sharer_name(session: Session, link: ShareLink) -> str:
    """The name on the watermark. Never a blank, never an id dressed as a name."""
    holder = _sharer(session, link)
    return holder.display_name if holder is not None else "a colleague"


def _sharer_email(session: Session, link: ShareLink) -> str:
    holder = _sharer(session, link)
    return holder.email if holder is not None else ""


__all__ = [
    "ACTION_SHARE_CREATED",
    "ACTION_SHARE_OPENED",
    "ACTION_SHARE_REVOKED",
    "ADMIN_PERMISSION",
    "MintedShare",
    "OpenedShare",
    "SETTING_SOURCE_DEFAULT",
    "SETTING_SOURCE_TENANT",
    "SHARE_PATH_PREFIX",
    "SHARE_UNAVAILABLE",
    "SHARE_UNAVAILABLE_TEXT",
    "SUBJECT_GONE",
    "SUBJECT_SHARE",
    "SharedSource",
    "Setting",
    "claim_for_company",
    "create_share_link",
    "hash_share_token",
    "new_share_token",
    "open_share",
    "opens_for_share",
    "revoke_share_link",
    "share_links_for_subject",
    "share_token_matches",
    "share_url",
    "sharing_enabled",
]
