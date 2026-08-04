"""Passwords, roles and permissions -- the grid that keeps duties separate.

docs/security.html rests the approval design on one control: the analyst who
interprets a change is never the person who approves the action that follows.
Written as prose that control is an assertion. Written here it is a set of rows
a reviewer can read, and the missing row -- analyst has no `action.approve` --
is the control itself.

Four rules govern this module.

**Tenancy, through the same chokepoint as everything else.** Every function
takes a company_id and refuses an unscoped call. The guard is imported from
app/state/queries.py rather than copied, because two copies of a tenant guard
drift. Two tables here carry no company_id -- Permission and RolePermission --
and the reason is in models.py: they hold vocabulary, not tenant facts. Every
path from that vocabulary into a person's authority runs through Role, UserRole
and User, and all three are filtered on the same company on every read below.

**Secrets are compared, never inspected.** Passwords go through
hashlib.scrypt -- OpenSSL-backed, memory-hard, a real key derivation function
rather than a fast digest wearing one's clothes. Every comparison of a secret
uses secrets.compare_digest. There is no `==` on a hash, a token or a password
anywhere in this file, and there must never be: `==` on a string returns as
soon as two bytes differ, which leaks how much of a guess was right to anyone
who can time the answer.

**Cost parameters travel with the hash.** hash_password writes n, r, p and
dklen onto the user row. verify_password reads them back and never assumes the
module's current values. Raising the cost later is then a change to one
constant, and every password already stored keeps verifying under the
parameters it was made with. The alternative -- a global constant -- forces a
choice between locking every existing user out and never raising the cost, and
in practice the cost never gets raised.

**Absence is denial.** A user who does not exist, a user belonging to another
company, a suspended user and a user with no roles all yield the same empty
permission set. An authorisation read has exactly one safe direction to fail
in, and it is not "probably fine".

**What this module does NOT protect against.** It has no login path, no
throttling and no session issuing. User.failed_attempts and User.locked_until
are storage this module never writes; app/auth/sessions.py writes them, and a
caller that verifies a password through this module directly, without going
through login(), gets unlimited guesses at whatever rate the transport allows.
It enforces a length floor on passwords and nothing else: no reuse
check against a breach corpus, no second factor, no rotation. And an admin
holds `user.manage`, so an admin can grant themselves the obligation owner
role and approve their own work. The audit chain records that grant, which
makes it visible afterwards; nothing here prevents it. Preventing it needs a
second approver on privilege changes, which is not built.
"""

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

# The action codes are imported, not restated. Two modules holding constants of
# the same name with different strings is the drift these constants exist to
# prevent: the row lands under one spelling and every query for the other misses
# it, silently, because a wrong action code is still a valid row.
from app.state.audit import (
    ACTION_ROLE_GRANTED,
    ACTION_ROLE_REVOKED,
    ACTION_USER_CREATED,
    ACTION_USER_REINSTATED,
    ACTION_USER_SUSPENDED,
    record_event,
)
from app.state.models import (
    PERMISSION_CODES,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_ACTIVE,
    SYSTEM_ROLE_NAMES,
    USER_STATUSES,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)

# Imported, not copied. Same guard, same failure, one place to audit.
from app.state.queries import _require_scope

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

# scrypt's work factors. n is the one that matters: memory and time both scale
# with it, which is what makes a GPU no better placed than a laptop. 2**14 with
# r=8 costs about 16 MiB and ~20 ms per hash here -- slow enough to make an
# offline guessing run expensive, fast enough that a login is not a wait.
#
# These are the values for hashes written from now on. They are NOT what
# verify_password uses; that reads the parameters stored on the row.
KDF_N = 2**14
KDF_R = 8
KDF_P = 1
KDF_DKLEN = 64
SALT_BYTES = 16

# Ceilings on what a stored parameter set may ask for. The values come from our
# own rows rather than from a request, so this is not defending against user
# input -- it is defending against a corrupt or edited row turning a login into
# a memory exhaustion. Refusing loudly beats allocating a gigabyte.
_MAX_KDF_N = 2**20
_MAX_KDF_R = 32
_MAX_KDF_P = 16
_MAX_KDF_MEMORY = 512 * 1024 * 1024

# Long enough that the length floor is not the weak part. NIST SP 800-63B asks
# for 8; a regulated buyer's security review asks for more, and a floor is the
# one password rule that does not push people toward `Passw0rd!`.
MIN_PASSWORD_LENGTH = 12

# 32 bytes of randomness, url-safe. A token this size needs no KDF: see the
# LoginSession docstring in models.py for why hashing it with plain SHA-256 is
# right there and would be wrong on a password.
SESSION_TOKEN_BYTES = 32


# ---------------------------------------------------------------------------
# The role grid
# ---------------------------------------------------------------------------

# Segregation of duties, as data. Read the analyst row for what is absent:
# action.approve and action.reject are not there, so the person who interprets
# a change cannot approve the action that follows from it. That is the control
# docs/security.html calls load-bearing, and it lives here rather than in a
# check somebody has to remember to write at each call site.
#
# The obligation owner is deliberately narrow. It approves and rejects and it
# reads the evidence needed to do that; it does not propose, does not resolve
# escalations and does not issue steers, because an approver who can also shape
# what reaches them for approval is not a second pair of eyes.
#
# The admin is narrower still, and does not approve anything. Admin manages
# people and sets the confidence threshold ADR-006 requires be configuration.
# Keeping approval out of it means the routine account-management role is not
# also a way into the decision path.
SYSTEM_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ROLE_ANALYST: (
        "proceeding.read",
        "change.read",
        "claim.read",
        "action.propose",
        "escalation.resolve",
        "steer.issue",
        "project.create",
        "knowledge.write",
    ),
    ROLE_OBLIGATION_OWNER: (
        "proceeding.read",
        "change.read",
        "claim.read",
        "action.approve",
        "action.reject",
        "audit.read",
    ),
    ROLE_ADMIN: (
        "proceeding.read",
        "change.read",
        "claim.read",
        "threshold.set",
        "user.manage",
        "audit.read",
    ),
}

SYSTEM_ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_ANALYST: (
        "Reads proceedings and changes, proposes actions, works escalations. "
        "Cannot approve the action that follows from its own interpretation."
    ),
    ROLE_OBLIGATION_OWNER: (
        "The named owner on the obligation. Approves or rejects the action "
        "routed to them; that approval is what lets it touch project state."
    ),
    ROLE_ADMIN: (
        "Manages accounts and sets the confidence threshold. Deliberately "
        "holds no approval permission."
    ),
}

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    "proceeding.read": "Read the proceedings this company tracks.",
    "change.read": "Read the typed differences between versions.",
    "claim.read": "Read claims and the citations behind them.",
    "action.propose": "Propose an action that follows from a change.",
    "action.approve": "Approve a proposed action so it can touch project state.",
    "action.reject": "Reject a proposed action and say why.",
    "escalation.resolve": "Resolve an escalation raised against a claim.",
    "steer.issue": "Issue a standing directive that shifts what research looks for.",
    "project.create": "Open a project.",
    "knowledge.write": "Write and supersede knowledge items.",
    "threshold.set": "Change the confidence threshold (ADR-006).",
    "user.manage": "Create users and grant or revoke their roles.",
    "audit.read": "Read the audit chain and verify it.",
}

# The action codes this module writes come from app/state/audit.py, which holds
# the whole audited vocabulary. They are imported at the top and are not
# re-exported below: one import path per string, so there is nowhere for a
# second spelling to appear.


# ---------------------------------------------------------------------------
# Small guards
# ---------------------------------------------------------------------------


def _require_actor(actor: str) -> str:
    """An unattributed write is not auditable, so it is refused up front.

    Defined here rather than imported from app/state/projects.py on purpose:
    identity is the lower layer, and the arrow between the two modules will run
    the other way once permission checks land in the workspace. Importing
    upward to save four lines is how a cycle gets built.
    """
    if not actor or not isinstance(actor, str):
        raise ValueError("actor is required; an unattributed write is not auditable")
    return actor


def _require_one_of(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _require_aware(moment: datetime, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(
            f"{field} must carry a timezone; a naive timestamp is a defect"
        )
    return moment


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def normalise_email(email: str) -> str:
    """Trim and lower-case. The one form the unique index and every lookup use.

    Applied at the write as well as the read, because the index in models.py is
    the only thing stopping one person holding two accounts and SQLite compares
    text case-sensitively. Normalising only on the way in would leave the index
    guarding a different string from the one anybody searches for.

    This is not address validation. It checks for an @ and for whitespace and
    stops there; anything stricter rejects real addresses, and this system is
    not the one that proves an address exists -- sending mail to it is.
    """
    if not email or not isinstance(email, str):
        raise ValueError("email is required")
    cleaned = email.strip().lower()
    if not cleaned:
        raise ValueError("email is required")
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise ValueError(f"{email!r} does not look like an email address")
    if any(character.isspace() for character in cleaned):
        raise ValueError("an email address cannot contain whitespace")
    return cleaned


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def _kdf_memory(n: int, r: int, p: int) -> int:
    """What scrypt will ask OpenSSL for, plus room for the working buffers.

    OpenSSL refuses to run when its maxmem is below what the parameters need,
    and its default cap is 32 MiB -- which the module defaults sit just under
    and a raised cost would sit above. Computing the figure from the parameters
    means raising KDF_N later does not silently start throwing.
    """
    return 128 * n * r + 128 * r * p + (1 << 20)


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> str:
    """The single call to scrypt. Both hashing and verifying come through here.

    One derivation path, so the two can never disagree about how a hash was
    produced -- the drift that makes a password stop verifying against itself
    after a refactor.
    """
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=_kdf_memory(n, r, p),
    ).hex()


def _checked_params(n: int, r: int, p: int, dklen: int) -> tuple[int, int, int, int]:
    """Refuse a parameter set that scrypt would reject or that would exhaust memory.

    Raises rather than falling back to the module defaults. A fallback here
    would verify the password against the wrong parameters, fail, and report
    itself as a wrong password -- principle 26 in docs/best-practices.html: a
    fallback that does not announce itself is worse than the failure it hides.
    """
    if n < 2 or n & (n - 1):
        raise ValueError(f"kdf n must be a power of two above 1; got {n}")
    if n > _MAX_KDF_N or r < 1 or r > _MAX_KDF_R or p < 1 or p > _MAX_KDF_P:
        raise ValueError(f"kdf parameters out of range: n={n} r={r} p={p}")
    if dklen < 32 or dklen > 256:
        raise ValueError(f"kdf dklen must be between 32 and 256; got {dklen}")
    if _kdf_memory(n, r, p) > _MAX_KDF_MEMORY:
        raise ValueError(f"kdf parameters would need too much memory: n={n} r={r}")
    return n, r, p, dklen


def hash_password(password: str) -> tuple[str, str, str]:
    """Hash a password with a fresh random salt. Returns (hash, salt, params).

    All three are hex or JSON text and all three belong on the user row: the
    hash means nothing without the salt, and neither means anything without the
    parameters that produced them.

    The salt is 16 random bytes per password, so two people who choose the same
    password store different hashes and one cracked hash tells an attacker
    nothing about the other. A shared or absent salt is what makes a stolen
    table worth precomputing against.
    """
    if not password or not isinstance(password, str):
        raise ValueError("a password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"a password must be at least {MIN_PASSWORD_LENGTH} characters"
        )

    n, r, p, dklen = _checked_params(KDF_N, KDF_R, KDF_P, KDF_DKLEN)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, n, r, p, dklen)
    params = json.dumps(
        {"n": n, "r": r, "p": p, "dklen": dklen},
        sort_keys=True,
        separators=(",", ":"),
    )
    return digest, salt.hex(), params


def verify_password(
    password: str, hash_hex: str, salt_hex: str, kdf_params_json: str
) -> bool:
    """True when the password reproduces the stored hash under its own parameters.

    The parameters are read from kdf_params_json every time. The module
    constants are not consulted, which is the whole point: raise KDF_N tomorrow
    and every hash written today still verifies.

    Wrong password returns False. A stored row that cannot be parsed raises,
    and the difference is deliberate. A malformed parameter set is a defect in
    the data, not a failed login, and returning False for it would hide a
    corrupted table behind a stream of people insisting their password works.
    Both outcomes deny access; only one of them is visible to whoever has to
    fix it.

    What this does NOT do. It does not rate-limit, so it will happily answer as
    fast as it is asked. It does not re-hash a password found to be stored
    under weaker parameters. And the time it takes is dominated by scrypt
    rather than by the comparison, so the compare_digest below closes a channel
    that is small next to the one an unthrottled endpoint leaves open.
    """
    if not password or not isinstance(password, str):
        return False
    if not hash_hex or not salt_hex or not kdf_params_json:
        return False

    try:
        raw = json.loads(kdf_params_json)
    except (TypeError, ValueError) as error:
        raise ValueError(f"stored kdf parameters are not readable JSON: {error}")
    if not isinstance(raw, dict):
        raise ValueError("stored kdf parameters must be a JSON object")

    try:
        n, r, p, dklen = _checked_params(
            int(raw["n"]), int(raw["r"]), int(raw["p"]), int(raw["dklen"])
        )
    except KeyError as error:
        raise ValueError(f"stored kdf parameters are missing {error}")
    except (TypeError, ValueError) as error:
        raise ValueError(f"stored kdf parameters are unusable: {error}")

    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        raise ValueError("stored salt is not hex")

    candidate = _derive(password, salt, n, r, p, dklen)
    return secrets.compare_digest(candidate, hash_hex)


def new_session_token() -> str:
    """A bearer token for one session. Shown once, never stored.

    Here rather than in the code that will issue sessions, so there is one
    definition of how a token is made and one of how it is looked up. Two
    definitions of a token scheme is how a comparison ends up using `==`.
    """
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """SHA-256 of a session token, which is what LoginSession.token_hash holds.

    A plain digest, on purpose, and the reasoning is on LoginSession in
    models.py: the token is 256 bits this process generated at random, so there
    is nothing to guess and a slow KDF would only tax every request. Never use
    this on a password.
    """
    if not token or not isinstance(token, str):
        raise ValueError("a session token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_token_matches(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    if not token or not token_hash:
        return False
    return secrets.compare_digest(hash_session_token(token), token_hash)


# ---------------------------------------------------------------------------
# Roles and permissions
# ---------------------------------------------------------------------------


def ensure_system_roles(session: Session) -> list[Role]:
    """Write the permission vocabulary and the three system roles. Idempotent.

    Takes no company_id and writes no tenant row, which is the one exemption
    from the scoping rule in this file. Permissions and system roles are
    definitions every tenant shares; giving them a company would mean each
    tenant needed its own copy of the word "approve".

    Nothing here is audited, and that is not an oversight. record_event refuses
    an unscoped entry, correctly, and inventing a company to satisfy it would
    put a fabricated row in a tamper-evident log. This function is closer to a
    migration than to a decision: it makes the schema match the grid in this
    module, and the grid is in version control where it can be diffed.

    The grid is asserted whole, not appended to. A system role's permissions
    are set to exactly SYSTEM_ROLE_PERMISSIONS on every call, so a code removed
    from the grid is removed from the role rather than lingering as a
    permission the product no longer believes it grants. That is principle 27
    from docs/best-practices.html applied to a small corpus: half-migrated, the
    grid answers every check plausibly and wrongly. Roles a tenant defined for
    itself are never touched.
    """
    unknown = sorted(
        {
            code
            for codes in SYSTEM_ROLE_PERMISSIONS.values()
            for code in codes
            if code not in PERMISSION_CODES
        }
    )
    if unknown:
        # SQLite does not enforce foreign keys unless asked, so writing a grid
        # row for a code with no permission behind it would succeed and then
        # vanish at the join in permissions_for_user. The role would look
        # granted and check as denied, and nothing would say why.
        raise ValueError(
            f"the role grid names permissions that do not exist: {unknown}. "
            "Add them to PERMISSION_CODES or correct the grid."
        )

    for code in PERMISSION_CODES:
        wording = PERMISSION_DESCRIPTIONS.get(code, "")
        existing = (
            session.query(Permission).filter(Permission.code == code).one_or_none()
        )
        if existing is None:
            session.add(Permission(id=code, code=code, description=wording))
        elif existing.description != wording:
            # The wording in this module is the definition. A row still
            # carrying last release's sentence describes a permission that no
            # longer means quite that, and it is the row a reviewer reads.
            existing.description = wording
    session.flush()

    roles: list[Role] = []
    for name in SYSTEM_ROLE_NAMES:
        wording = SYSTEM_ROLE_DESCRIPTIONS.get(name, "")
        role = (
            session.query(Role)
            .filter(Role.company_id.is_(None))
            .filter(Role.name == name)
            .one_or_none()
        )
        if role is None:
            role = Role(
                id=f"role-{name}", company_id=None, name=name, description=wording
            )
            session.add(role)
            session.flush()
        elif role.description != wording:
            role.description = wording
        roles.append(role)

        wanted = set(SYSTEM_ROLE_PERMISSIONS[name])
        held = (
            session.query(RolePermission)
            .filter(RolePermission.role_id == role.id)
            .all()
        )
        for row in held:
            if row.permission_id not in wanted:
                session.delete(row)
        for code in sorted(wanted - {row.permission_id for row in held}):
            session.add(RolePermission(role_id=role.id, permission_id=code))

    session.flush()
    return roles


def roles_for_company(session: Session, company_id: str) -> list[Role]:
    """Every role this company can grant: the system ones plus its own."""
    _require_scope(company_id)
    return (
        session.query(Role)
        .filter(or_(Role.company_id.is_(None), Role.company_id == company_id))
        .order_by(Role.name, Role.id)
        .all()
    )


def role_for_company(session: Session, company_id: str, name: str) -> Role | None:
    """Resolve a role name for one company. The company's own row wins.

    A tenant that defines its own `analyst` shadows the system one, and the
    shadowing is deliberate rather than an accident of ordering: a company
    narrowing a role for itself must not be silently overruled by the shared
    definition. Nothing in this task creates tenant roles, so today this always
    returns the system row -- the branch exists so the answer is decided here
    once rather than differently at each call site later.
    """
    _require_scope(company_id)
    if not name:
        raise ValueError("a role name is required")
    own = (
        session.query(Role)
        .filter(Role.company_id == company_id)
        .filter(Role.name == name)
        .one_or_none()
    )
    if own is not None:
        return own
    return (
        session.query(Role)
        .filter(Role.company_id.is_(None))
        .filter(Role.name == name)
        .one_or_none()
    )


def permissions_for_role(
    session: Session, company_id: str, name: str
) -> frozenset[str]:
    """The codes one role carries. An unknown role has none, rather than raising."""
    _require_scope(company_id)
    role = role_for_company(session, company_id, name)
    if role is None:
        return frozenset()
    codes = (
        session.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .filter(RolePermission.role_id == role.id)
        .all()
    )
    return frozenset(code for (code,) in codes)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def users_for_company(session: Session, company_id: str) -> list[User]:
    """This company's users, by email. Another company's users are not here."""
    _require_scope(company_id)
    return (
        session.query(User)
        .filter(User.company_id == company_id)
        .order_by(User.email)
        .all()
    )


def user_for_company(session: Session, company_id: str, user_id: str) -> User | None:
    """One user, or None. Another company's user id resolves to None.

    None rather than a raise, matching change_for_company and
    project_for_company: a caller resolving an id it was handed is the normal
    case and a 404 is the honest answer. What must never happen is the row
    coming back.
    """
    _require_scope(company_id)
    if not user_id:
        return None
    return (
        session.query(User)
        .filter(User.company_id == company_id)
        .filter(User.id == user_id)
        .one_or_none()
    )


def user_by_email(session: Session, company_id: str, email: str) -> User | None:
    """Look a user up by address, normalised the same way it was stored.

    A malformed address returns None rather than raising. This is the lookup a
    login path makes on whatever was typed into a form, and a login form that
    can be made to throw is a login form that tells an attacker the difference
    between a wrong address and an unknown one.
    """
    _require_scope(company_id)
    try:
        cleaned = normalise_email(email)
    except ValueError:
        return None
    return (
        session.query(User)
        .filter(User.company_id == company_id)
        .filter(User.email == cleaned)
        .one_or_none()
    )


def create_user(
    session: Session,
    company_id: str,
    *,
    email: str,
    display_name: str,
    password: str,
    actor: str,
    status: str = "active",
    user_id: str | None = None,
    created_at: datetime | None = None,
) -> User:
    """Create a user and record that somebody created them.

    The audit entry names the account and who made it. It never carries the
    password, the hash, the salt or the parameters: a tamper-evident log is
    read by more people than the users table is, and a secret written into an
    append-only record cannot be taken out again.

    A duplicate address is refused before the write rather than left to the
    unique index. An IntegrityError at flush time aborts the whole transaction,
    which on this path would discard the audit entry alongside the user -- so
    the check that keeps the message readable is also the one that keeps the
    failure from taking unrelated work with it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    _require_one_of(status, USER_STATUSES, "status")
    cleaned = normalise_email(email)
    if not display_name:
        raise ValueError(
            "a user needs a display name; an account nobody can name in a "
            "review is not attributable"
        )

    if user_by_email(session, company_id, cleaned) is not None:
        raise ValueError(f"{cleaned} already has an account in this company")

    digest, salt, params = hash_password(password)
    stamp = _require_aware(created_at or _utcnow(), "created_at")

    user = User(
        id=user_id or _new_id("usr"),
        company_id=company_id,
        email=cleaned,
        display_name=display_name,
        password_hash=digest,
        password_salt=salt,
        kdf_params=params,
        status=status,
        created_at=stamp,
        last_login_at=None,
        failed_attempts=0,
        locked_until=None,
    )
    session.add(user)
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_USER_CREATED,
        subject_type="user",
        subject_id=user.id,
        reason=f"created {cleaned} ({display_name}), status {status}",
        occurred_at=stamp,
    )
    session.flush()
    return user


def set_user_status(
    session: Session, company_id: str, user_id: str, status: str, actor: str
) -> User:
    """Suspend or reinstate an account. The row stays; the authority stops.

    Suspending is not deleting, and the difference is the point: the person's
    name is still on every decision they took, and the audit chain still
    resolves their id. permissions_for_user returns nothing for a user who is
    not active, so suspension takes effect everywhere at once rather than
    depending on each call site remembering to check.

    Setting the status the account already holds writes nothing and appends no
    event. Re-stating a fact is not a decision, and a chain full of them is a
    chain nobody reads.

    Losing authority and regaining it get separate action codes from the
    vocabulary in app/state/audit.py. One code with the direction buried in the
    reason text would make "who was let back in, and by whom" a question that
    needs prose parsed rather than rows filtered.
    """
    _require_scope(company_id)
    who = _require_actor(actor)
    _require_one_of(status, USER_STATUSES, "status")

    user = user_for_company(session, company_id, user_id)
    if user is None:
        raise ValueError(f"no user {user_id!r} for this company")

    was = user.status
    if was == status:
        return user
    user.status = status
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=(
            ACTION_USER_REINSTATED
            if status == STATUS_ACTIVE
            else ACTION_USER_SUSPENDED
        ),
        subject_type="user",
        subject_id=user.id,
        reason=f"status moved from {was} to {status}",
    )
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def role_grants_for_user(
    session: Session,
    company_id: str,
    user_id: str,
    *,
    include_revoked: bool = True,
) -> list[UserRole]:
    """Every grant this user has held, revoked ones included by default.

    The default is include_revoked=True because the question this table exists
    to answer is what a person could do at the time they did something, and a
    list that quietly omits revoked grants answers a different, easier question.
    """
    _require_scope(company_id)
    query = (
        session.query(UserRole)
        .join(User, UserRole.user_id == User.id)
        .filter(User.company_id == company_id)
        .filter(UserRole.company_id == company_id)
        .filter(UserRole.user_id == user_id)
    )
    if not include_revoked:
        query = query.filter(UserRole.revoked_at.is_(None))
    return query.order_by(UserRole.granted_at, UserRole.id).all()


def _active_grants(
    session: Session, company_id: str, user_id: str, role_id: str
) -> list[UserRole]:
    """Every live grant of one role to one user. Normally none or one.

    Returns a list rather than a row because the schema cannot make it one:
    there is no unique index over the pair, by design, so a direct write or a
    future bug could leave two. revoke_role closes all of them. Revoking the
    newest and leaving an older one would take the permission away in the
    interface and leave it in place in the check -- a failure that reads as
    fixed.
    """
    return (
        session.query(UserRole)
        .join(User, UserRole.user_id == User.id)
        .filter(User.company_id == company_id)
        .filter(UserRole.company_id == company_id)
        .filter(UserRole.user_id == user_id)
        .filter(UserRole.role_id == role_id)
        .filter(UserRole.revoked_at.is_(None))
        .order_by(UserRole.granted_at.desc(), UserRole.id.desc())
        .all()
    )


def grant_role(
    session: Session,
    company_id: str,
    *,
    user_id: str,
    role_name: str,
    actor: str,
    granted_at: datetime | None = None,
) -> UserRole:
    """Give a user a role, and record who gave it to them.

    Granting a role the user already holds returns the existing grant and
    writes nothing. A double click is not a decision, and recording it as one
    would put a second entry in the chain that a reader has to work out the
    meaning of -- the same rule attach_change follows.

    A role belonging to another company cannot be granted: role_for_company
    resolves names against the system roles and this company's own, so another
    tenant's role simply does not exist here.
    """
    _require_scope(company_id)
    who = _require_actor(actor)

    user = user_for_company(session, company_id, user_id)
    if user is None:
        raise ValueError(f"no user {user_id!r} for this company")
    role = role_for_company(session, company_id, role_name)
    if role is None:
        raise ValueError(f"no role {role_name!r} available to this company")

    existing = _active_grants(session, company_id, user_id, role.id)
    if existing:
        return existing[0]

    stamp = _require_aware(granted_at or _utcnow(), "granted_at")
    grant = UserRole(
        user_id=user.id,
        role_id=role.id,
        company_id=company_id,
        granted_by=who,
        granted_at=stamp,
        revoked_at=None,
    )
    session.add(grant)
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_ROLE_GRANTED,
        subject_type="user",
        subject_id=user.id,
        reason=f"granted role {role.name} to {user.email}",
        occurred_at=stamp,
    )
    session.flush()
    return grant


def revoke_role(
    session: Session,
    company_id: str,
    *,
    user_id: str,
    role_name: str,
    actor: str,
    revoked_at: datetime | None = None,
) -> UserRole:
    """Take a role away. The grant row stays and gains a revoked_at.

    Deleting the row would erase the fact that the person once held the role,
    which is exactly the question asked after an incident -- who could approve
    this in March. The revoked grant stays readable through
    role_grants_for_user and stops counting in permissions_for_user.

    Revoking a role the user does not hold raises rather than passing quietly.
    The caller believed something about this user's authority that was not
    true, and a silent no-op would let them carry on believing it.
    """
    _require_scope(company_id)
    who = _require_actor(actor)

    user = user_for_company(session, company_id, user_id)
    if user is None:
        raise ValueError(f"no user {user_id!r} for this company")
    role = role_for_company(session, company_id, role_name)
    if role is None:
        raise ValueError(f"no role {role_name!r} available to this company")

    grants = _active_grants(session, company_id, user_id, role.id)
    if not grants:
        raise ValueError(f"{user.email} does not hold the role {role.name!r}")

    stamp = _require_aware(revoked_at or _utcnow(), "revoked_at")
    for grant in grants:
        grant.revoked_at = stamp
    record_event(
        session,
        company_id=company_id,
        actor=who,
        action=ACTION_ROLE_REVOKED,
        subject_type="user",
        subject_id=user.id,
        reason=f"revoked role {role.name} from {user.email}",
        occurred_at=stamp,
    )
    session.flush()
    return grants[0]


def permissions_for_user(
    session: Session, company_id: str, user_id: str
) -> frozenset[str]:
    """Every permission code this user currently holds. The union of live grants.

    Four things return the empty set and are indistinguishable from each other
    here: an unknown user, a user belonging to another company, a user who is
    suspended or still invited, and a user with no roles. That is the intended
    behaviour of an authorisation read -- there is one safe direction to fail
    in, and a caller that needs to tell those cases apart is asking a different
    question and should call user_for_company.

    Status is part of the filter rather than left to callers. Suspending an
    account then takes effect at every call site at once, instead of at the
    ones that remembered to look.

    The scope is checked in three places on one query -- the user's company,
    the grant's company, and the role being either a system role or this
    company's own. Each column was written by a different call, and a bug in
    any one of them is what hands somebody another tenant's authority.
    """
    _require_scope(company_id)
    if not user_id:
        return frozenset()

    codes = (
        session.query(Permission.code)
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
    return frozenset(code for (code,) in codes)


def user_has_permission(
    session: Session, company_id: str, user_id: str, code: str
) -> bool:
    """One permission check. Reads the union, so it cannot answer differently."""
    _require_scope(company_id)
    if not code:
        raise ValueError("a permission code is required; refusing an empty check")
    return code in permissions_for_user(session, company_id, user_id)


# The action codes are deliberately absent. They belong to app/state/audit.py,
# which owns the vocabulary; re-exporting them here would give a caller two
# import paths to the same string and one of them would eventually move.
__all__ = [
    "KDF_DKLEN",
    "KDF_N",
    "KDF_P",
    "KDF_R",
    "MIN_PASSWORD_LENGTH",
    "PERMISSION_DESCRIPTIONS",
    "SYSTEM_ROLE_DESCRIPTIONS",
    "SYSTEM_ROLE_PERMISSIONS",
    "create_user",
    "ensure_system_roles",
    "grant_role",
    "hash_password",
    "hash_session_token",
    "new_session_token",
    "normalise_email",
    "permissions_for_role",
    "permissions_for_user",
    "revoke_role",
    "role_for_company",
    "role_grants_for_user",
    "roles_for_company",
    "session_token_matches",
    "set_user_status",
    "user_by_email",
    "user_for_company",
    "user_has_permission",
    "users_for_company",
    "verify_password",
]
