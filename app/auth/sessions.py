"""Login, logout, and the one question every request asks: who is holding this.

app/state/identity.py stores accounts and hashes passwords. It deliberately has
no login path, and its docstring says so: until something writes
User.failed_attempts and User.locked_until, an attacker gets unlimited guesses.
This module is that something.

Four rules run through it.

**A refusal says one thing.** LOGIN_REFUSED is the only sentence that reaches a
caller, whatever went wrong -- unknown address, wrong password, suspended
account, locked account. A message that distinguishes them turns the form into
an account oracle: an attacker learns which addresses exist and which are worth
coming back to, without ever guessing a password. The audit row records which
of the four it was, because the person reading the log is on the other side of
the boundary from the person typing at the form.

**A refusal costs the same as a success.** verify_password runs on every
attempt, including one against an address with no account, using a dummy hash
built at import. Without that, an unknown address returns in microseconds and a
known one in tens of milliseconds, and the difference is measurable across a
network. The equalisation is not perfect -- a lookup that misses the index and
a lookup that hits it differ slightly -- and it is a great deal better than the
early return it replaces.

**Every attempt lands in the audit chain, and the chain has one home.** Success
and failure both call record_event in app/state/audit.py. There is no security
log beside it. A failed attempt names the address that was tried and never the
password tried against it -- see _attempted_address for the one case where even
the address is withheld.

**A caller must commit a refusal.** login() raises AuthFailed *after* writing
the failure to the log and incrementing the counter. Code shaped like

    with session_scope() as session:          # rolls back on any exception
        sessions.login(session, ...)          # raises AuthFailed

throws the audit row and the counter away with the exception, which is the
throttle silently not existing. Catch AuthFailed INSIDE the transaction.
app/web/views/auth.py does, and tests/test_login.py asserts through HTTP that
the row survives, because that is the only place the mistake is visible.

**What this does NOT protect against.** There is no second factor, no device
binding, and no CSRF token on the login form -- an attacker can still make a
victim's browser sign in as the attacker, and SameSite=Lax on the cookie is
what limits the damage rather than a token that is not built. The lockout is
per account, so an attacker spreading guesses across many addresses is
throttled by nothing here; that belongs at the edge, and this build has no
edge. Nothing binds a session to an address: the ip on the row is evidence for
an investigation, never an input to a decision, because mobile networks move
people between addresses far more often than thieves steal cookies.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.state.audit import (
    ACTION_LOGIN_FAILED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_LOGOUT,
    ACTION_SESSION_EXPIRED,
    ACTOR_USER,
    record_event,
)
from app.state.identity import (
    hash_password,
    hash_session_token,
    new_session_token,
    normalise_email,
    session_token_matches,
    user_by_email,
    user_for_company,
    verify_password,
)
from app.state.models import STATUS_ACTIVE, LoginSession, User
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# The numbers, and why each is that number
# ---------------------------------------------------------------------------

# A working day plus the evening somebody spends finishing a filing. Long enough
# that a regulatory analyst is not signed out mid-review, short enough that a
# laptop left on a train is a problem for one day rather than one month. Written
# onto the row at creation and never extended, so a stolen token has a ceiling
# it cannot be talked past.
SESSION_TTL = timedelta(hours=12)

# Five wrong passwords, then fifteen minutes. Five is above the number of times
# a person mistypes a password they know and far below the number an offline
# guessing run needs; fifteen minutes turns a thousand guesses an hour into
# twenty, which is the whole of what a lockout buys.
#
# The lock is not extended by attempts made while it holds. An attacker who
# waits it out gets five more guesses, and that is the accepted cost of not
# handing anyone a way to keep a real person locked out for ever by guessing at
# their address once every fourteen minutes.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

# last_seen_at moves at most once a minute. Writing it on every request would
# put a database write behind every page view for a field nobody reads to the
# second, and on SQLite that is a write lock per request.
TOUCH_INTERVAL = timedelta(minutes=1)

# The only sentence a caller may show. Not "unknown email", not "wrong
# password", not "your account is locked" -- each of those is a fact about
# somebody else's account, handed to whoever asked.
LOGIN_REFUSED = "email or password is incorrect"

# What the audit row says instead. These never reach the browser.
REASON_NO_ACCOUNT = "no account with that address"
REASON_WRONG_PASSWORD = "password did not match"
REASON_LOCKED = "account locked; attempt refused without checking the password"
REASON_NOT_ACTIVE = "account is not active"

# Written into revoked_reason. Plain words, because a revoked session with no
# reason cannot be told apart from a bug.
REVOKED_BY_LOGOUT = "user signed out"
REVOKED_BY_EXPIRY = "expired"

# Stored in place of an address that does not parse as one. Somebody typing
# their password into the email box is common, and this log is append-only: a
# secret written into it cannot be taken out again. An address without an @ is
# not an address, and the fact worth keeping is that an attempt happened.
UNPARSEABLE_ADDRESS = "(unparseable address)"


class AuthFailed(Exception):
    """A login was refused. Carries the generic message and nothing else.

    Deliberately holds no reason code and no account state. A caller that
    cannot tell the four refusals apart cannot leak the difference, however the
    error is rendered later. The reason is in the audit row, where the reader
    is trusted.
    """


# A hash of a password nobody knows, made once per process. verify_password
# runs against this when the address has no account, so the answer costs the
# same either way. Random per process, so it is not a constant an attacker can
# recognise in a stolen database, and unequal to any real password.
_DUMMY_HASH, _DUMMY_SALT, _DUMMY_PARAMS = hash_password(secrets.token_urlsafe(32))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{field} must carry a timezone; a naive timestamp is a defect")
    return moment


def _new_id() -> str:
    """A session id. Not the token, and not a secret.

    It goes into audit rows so an investigation can tie decisions to one
    sitting. Holding it grants nothing: only the token, which is never stored,
    resolves a session.
    """
    return f"sess-{uuid.uuid4().hex[:16]}"


def _attempted_address(email: str) -> str:
    """The address to record for an attempt, or a marker when it is not one.

    Returns the normalised address when the input looks like one. Anything else
    -- a blank box, a stray paste, a password typed into the wrong field --
    records the marker instead. The alternative is writing whatever was typed
    into an append-only table, and the day that string is somebody's password
    there is no way to take it back out.
    """
    try:
        return normalise_email(email)[:320]
    except (ValueError, TypeError):
        return UNPARSEABLE_ADDRESS


def _record_failure(
    session: Session,
    *,
    company_id: str,
    attempted: str,
    user: User | None,
    reason: str,
    ip: str,
    occurred_at: datetime,
) -> None:
    """Write the failed attempt, then let the caller raise.

    actor_kind is `user` even when no account matched: a person tried, and the
    row that says a machine tried would be false. actor_user_id stays None
    there, which record_event allows and which reads as "we could not say who".
    """
    record_event(
        session,
        company_id=company_id,
        actor=f"person:{attempted}",
        action=ACTION_LOGIN_FAILED,
        subject_type="user",
        subject_id=user.id if user is not None else attempted,
        reason=reason,
        occurred_at=occurred_at,
        actor_user_id=user.id if user is not None else None,
        actor_kind=ACTOR_USER,
        ip=ip or None,
    )


def login(
    session: Session,
    company_id: str,
    *,
    email: str,
    password: str,
    ip: str = "",
    user_agent: str = "",
    now: datetime | None = None,
    ttl: timedelta = SESSION_TTL,
) -> tuple[LoginSession, str]:
    """Sign a person in. Returns the session row and the raw token, once.

    The token is returned and never stored. What goes in the database is
    sha256(token), so somebody who reads the table holds hashes rather than
    working sessions -- and, because the token has 256 bits of entropy behind
    it, those hashes are not worth attacking.

    Order of checks, and it matters. The lock is tested before the password, so
    an account under lockout is refused even when the password is right; a
    lockout that a correct password walks through is a lockout that stops
    exactly the attacker who has already succeeded. The password is verified
    before any of it, against a real hash or a dummy one, so the ordering of
    the checks does not show up in the response time.

    Raises AuthFailed for every refusal, with one message. The failure is
    written to the audit chain and the attempt counter is incremented BEFORE
    the raise, so a caller that lets the exception escape its transaction
    discards both -- read the module docstring.

    A stored row whose kdf parameters cannot be read raises ValueError out of
    here rather than becoming a failed login. That is a corrupt table, not a
    wrong password, and reporting it as a wrong password would hide it behind a
    stream of people insisting their password works.
    """
    _require_scope(company_id)
    moment = _require_aware(now or _utcnow(), "now")
    attempted = _attempted_address(email)
    user = user_by_email(session, company_id, email)

    # Always one derivation, whatever happens next.
    material = (
        (user.password_hash, user.password_salt, user.kdf_params)
        if user is not None
        else (_DUMMY_HASH, _DUMMY_SALT, _DUMMY_PARAMS)
    )
    password_ok = verify_password(password, *material)

    if user is not None and user.locked_until is not None and user.locked_until > moment:
        _record_failure(
            session,
            company_id=company_id,
            attempted=attempted,
            user=user,
            reason=f"{REASON_LOCKED}; lock holds until {user.locked_until.isoformat()}",
            ip=ip,
            occurred_at=moment,
        )
        session.flush()
        raise AuthFailed(LOGIN_REFUSED)

    if user is None:
        _record_failure(
            session,
            company_id=company_id,
            attempted=attempted,
            user=None,
            reason=REASON_NO_ACCOUNT,
            ip=ip,
            occurred_at=moment,
        )
        session.flush()
        raise AuthFailed(LOGIN_REFUSED)

    if user.status != STATUS_ACTIVE:
        # The status is in the reason because the log reader needs it. It is
        # not in the exception, because the person at the form does not.
        _record_failure(
            session,
            company_id=company_id,
            attempted=attempted,
            user=user,
            reason=f"{REASON_NOT_ACTIVE}: {user.status}",
            ip=ip,
            occurred_at=moment,
        )
        session.flush()
        raise AuthFailed(LOGIN_REFUSED)

    if not password_ok:
        user.failed_attempts = (user.failed_attempts or 0) + 1
        reason = f"{REASON_WRONG_PASSWORD}; {user.failed_attempts} in a row"
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = moment + LOCKOUT
            reason = f"{reason}; locked until {user.locked_until.isoformat()}"
        _record_failure(
            session,
            company_id=company_id,
            attempted=attempted,
            user=user,
            reason=reason,
            ip=ip,
            occurred_at=moment,
        )
        session.flush()
        raise AuthFailed(LOGIN_REFUSED)

    # From here it succeeded. Clearing the counter is what makes the lockout
    # count CONSECUTIVE failures rather than failures ever, which is the
    # difference between throttling an attacker and locking out a person who
    # has mistyped their password five times over three months.
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = moment

    token = new_session_token()
    live = LoginSession(
        id=_new_id(),
        user_id=user.id,
        company_id=company_id,
        token_hash=hash_session_token(token),
        created_at=moment,
        expires_at=moment + ttl,
        last_seen_at=moment,
        ip=ip or "",
        user_agent=(user_agent or "")[:512],
        revoked_at=None,
        revoked_reason=None,
    )
    session.add(live)

    record_event(
        session,
        company_id=company_id,
        actor=f"person:{user.email}",
        action=ACTION_LOGIN_SUCCEEDED,
        subject_type="user",
        subject_id=user.id,
        reason=f"signed in; session expires {live.expires_at.isoformat()}",
        occurred_at=moment,
        actor_user_id=user.id,
        actor_kind=ACTOR_USER,
        session_id=live.id,
        ip=ip or None,
    )
    session.flush()
    return live, token


def resolve_session(
    session: Session, token: str, *, now: datetime | None = None
) -> tuple[User, LoginSession] | None:
    """The person holding this token, or None. Five different Nones, one answer.

    Unknown token, revoked session, expired session, deleted user, suspended
    user -- all None, and the caller cannot tell which. That is deliberate: a
    surface that answers differently for a token that once existed is a surface
    that tells an attacker their stolen cookie was real but stale, which is
    worth knowing.

    Expiry is enforced here rather than by a sweep, so a session that outlives
    its window stops working at the next request whether or not any cleanup job
    ran. The first request to notice also closes the row and records
    session.expired once; later requests with the same token find it revoked
    and say nothing. That keeps the log bounded -- an unauthenticated caller
    replaying one dead token cannot write a row per request.

    NO SCOPE ARGUMENT, AND WHY. Every other read in this codebase takes a
    company_id. This one cannot: it is the read that ESTABLISHES the scope, and
    the token is the credential that does it. What it does instead is take the
    scope from the session row and read the user through the ordinary scoped
    chokepoint, so a session row pointing at another company's user resolves to
    None rather than handing that user's company back to the caller.

    What this does NOT check. Nothing here compares the ip or the user agent on
    the row with the caller's. Both are evidence, not authorisation; see the
    module docstring.
    """
    if not token or not isinstance(token, str):
        return None
    moment = _require_aware(now or _utcnow(), "now")

    # Two steps, and the second is the one that decides. The query is an index
    # probe on a hash -- it finds a candidate row, and a SQL engine has no
    # constant-time comparison to offer. session_token_matches then compares the
    # presented token against the stored hash with secrets.compare_digest, which
    # is the rule this codebase holds everywhere: no `==` decides whether a
    # secret is the right one.
    live = (
        session.query(LoginSession)
        .filter(LoginSession.token_hash == hash_session_token(token))
        .one_or_none()
    )
    if live is None or not session_token_matches(token, live.token_hash):
        return None
    if live.revoked_at is not None:
        return None

    if live.expires_at <= moment:
        live.revoked_at = moment
        live.revoked_reason = REVOKED_BY_EXPIRY
        record_event(
            session,
            company_id=live.company_id,
            actor=f"user:{live.user_id}",
            action=ACTION_SESSION_EXPIRED,
            subject_type="session",
            subject_id=live.id,
            reason=f"session expired at {live.expires_at.isoformat()}",
            occurred_at=moment,
            actor_user_id=live.user_id,
            actor_kind=ACTOR_USER,
            session_id=live.id,
        )
        session.flush()
        return None

    # Read through the same chokepoint every other read in this codebase uses,
    # scoped by the company on the session row. A get() by primary key would
    # return the row whatever company it belonged to, and the check that the
    # two agree would then be one more line somebody could delete.
    user = user_for_company(session, live.company_id, live.user_id)
    if user is None or user.status != STATUS_ACTIVE:
        return None

    if live.last_seen_at is None or moment - live.last_seen_at >= TOUCH_INTERVAL:
        live.last_seen_at = moment

    return user, live


def logout(
    session: Session,
    login_session_id: str,
    actor: str,
    *,
    company_id: str,
    now: datetime | None = None,
    reason: str = REVOKED_BY_LOGOUT,
) -> LoginSession | None:
    """End one session. The row stays and gains a revoked_at.

    company_id is keyword-only and required, which is one more argument than
    "log this person out" seems to need. It is here for the same reason every
    read in app/state/queries.py takes it: a session id is a short string, and
    a function that revokes by id alone lets one tenant end another tenant's
    sessions by guessing. The id is looked up inside the scope, so another
    company's id resolves to None -- the same answer an unknown id gets.

    Revoking an already-revoked session writes nothing and appends no second
    event. A double-clicked sign-out is not two decisions.

    The row is never deleted. "Who was signed in when this was approved" is a
    question asked after the fact, and a deleted row answers it with silence.
    """
    _require_scope(company_id)
    if not actor:
        raise ValueError("actor is required; an unattributed write is not auditable")
    if not login_session_id:
        return None
    moment = _require_aware(now or _utcnow(), "now")

    live = (
        session.query(LoginSession)
        .filter(LoginSession.company_id == company_id)
        .filter(LoginSession.id == login_session_id)
        .one_or_none()
    )
    if live is None or live.revoked_at is not None:
        return live

    live.revoked_at = moment
    live.revoked_reason = reason
    record_event(
        session,
        company_id=company_id,
        actor=actor,
        action=ACTION_LOGOUT,
        subject_type="session",
        subject_id=live.id,
        reason=reason,
        occurred_at=moment,
        actor_user_id=live.user_id,
        actor_kind=ACTOR_USER,
        session_id=live.id,
    )
    session.flush()
    return live


def revoke_all_for_user(
    session: Session,
    company_id: str,
    user_id: str,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> int:
    """End every live session a person holds. Returns how many were closed.

    This is the control behind "change the password", "suspend the account" and
    "we think that laptop was stolen". Suspending an account without it leaves
    every cookie already issued working until it expires, which is a suspension
    in the user table and nowhere else.

    One audit row per session rather than one for the lot: each session is a
    separate thing that stopped, with its own id, and a single summary row
    would make "when did session X end" unanswerable.

    Returns 0 when the person held nothing, which is not a failure. Nothing is
    written in that case, so calling this on every password change is safe.
    """
    _require_scope(company_id)
    if not actor:
        raise ValueError("actor is required; an unattributed write is not auditable")
    if not user_id:
        return 0
    moment = _require_aware(now or _utcnow(), "now")

    live_rows = (
        session.query(LoginSession)
        .filter(LoginSession.company_id == company_id)
        .filter(LoginSession.user_id == user_id)
        .filter(LoginSession.revoked_at.is_(None))
        .order_by(LoginSession.created_at, LoginSession.id)
        .all()
    )
    for live in live_rows:
        live.revoked_at = moment
        live.revoked_reason = reason
        record_event(
            session,
            company_id=company_id,
            actor=actor,
            action=ACTION_LOGOUT,
            subject_type="session",
            subject_id=live.id,
            reason=reason,
            occurred_at=moment,
            actor_user_id=live.user_id,
            actor_kind=ACTOR_USER,
            session_id=live.id,
        )
    if live_rows:
        session.flush()
    return len(live_rows)


__all__ = [
    "AuthFailed",
    "LOCKOUT",
    "LOGIN_REFUSED",
    "MAX_FAILED_ATTEMPTS",
    "REASON_LOCKED",
    "REASON_NOT_ACTIVE",
    "REASON_NO_ACCOUNT",
    "REASON_WRONG_PASSWORD",
    "REVOKED_BY_EXPIRY",
    "REVOKED_BY_LOGOUT",
    "SESSION_TTL",
    "TOUCH_INTERVAL",
    "UNPARSEABLE_ADDRESS",
    "login",
    "logout",
    "resolve_session",
    "revoke_all_for_user",
]
