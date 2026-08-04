"""The audit trail, and the three mechanisms that keep it honest.

A history view that the writing process can edit proves nothing under dispute,
which is exactly the situation this product exists for: a regulator or an
auditor asking why the company said what it said, and when it knew. So the log
is append-only by refusal and tamper-evident by construction.

ONE LOG. Logins, permission grants, approvals and state changes all land here.
A second table for "security events" would drift from this one, and the log
nobody reads is the one that goes wrong first -- silently, because nothing
compares them. Attribution is columns on this row, not a parallel record.

TWO DIGEST SCHEMES, ON PURPOSE. Version 1 hashes the ten original fields.
Version 2 hashes those ten plus the four attribution fields. Rows written
before attribution existed keep their v1 hashes and verify under v1 forever;
nothing rewrites them. This is best-practices.html section 27 pointed the other
way round: a derived corpus migrates all at once or not at all, and here the
derived data IS the evidence, so the answer is "not at all". Re-hashing the old
rows under v2 would produce a chain that verifies and proves nothing, because
the process that verifies it would be the process that rewrote it. Each row
therefore states its own scheme in digest_version and verify_chain dispatches
per row. A chain with v1 rows followed by v2 rows verifies end to end: the
prev_hash linkage is a value copied forward and does not care which scheme
computed it.

What this does NOT claim. The hash chain detects a record that was altered or
removed after the fact. It does not prevent it. Anyone who can write the whole
database file can recompute every hash from the tampered record forward, and
nothing here would notice. Defeating that needs the chain head published
somewhere the same attacker cannot reach -- external write-once storage, or
periodic notarisation. That is a production requirement and it is not built.
docs/security.html says the same thing rather than implying otherwise.

Nor does attribution prove a person acted. actor_user_id records who the
writing process believed was acting. A stolen session token produces a row that
names its victim, correctly hashed, and nothing in this file can tell.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.orm import Session

from app.state.models import AuditEvent


class AuditTamperError(RuntimeError):
    """Raised when the log was altered, or when code tries to alter it."""


# ---------------------------------------------------------------------------
# Who acted
#
# Stored rather than guessed from the actor string. Whether a person, the
# pipeline or a model produced a row is the first thing anyone asks of it, and
# it is not recoverable later from a name.
# ---------------------------------------------------------------------------

ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_MODEL = "model"
ACTOR_KINDS = (ACTOR_USER, ACTOR_SYSTEM, ACTOR_MODEL)


# ---------------------------------------------------------------------------
# Digest schemes
#
# 1: the ten original fields. Frozen. Every row written before attribution
#    existed hashes under it, so changing it by a byte would report tampering
#    on records nobody touched.
# 2: those ten plus actor_user_id, actor_kind, session_id and ip.
# ---------------------------------------------------------------------------

DIGEST_V1 = 1
DIGEST_V2 = 2
CURRENT_DIGEST_VERSION = DIGEST_V2


# ---------------------------------------------------------------------------
# Audited actions
#
# Constants rather than string literals at the call site, because a typo in an
# action code does not fail: it writes a row that verifies perfectly and that no
# query for that action will ever return. The event is in the log and invisible,
# which is worse than missing.
#
# The vocabulary is noun.verb, matching the codes already written by
# app/state/projects.py and app/state/review.py.
# ---------------------------------------------------------------------------

# Authentication. A FAILED login is audited too: an attacker probing accounts
# has to leave a trace. The row names the account that was tried and never the
# password that was tried against it -- see record_event.
ACTION_LOGIN_SUCCEEDED = "login.succeeded"
ACTION_LOGIN_FAILED = "login.failed"
ACTION_LOGOUT = "session.logout"
ACTION_SESSION_EXPIRED = "session.expired"

# Accounts and permission. Every grant is a decision somebody made.
ACTION_ROLE_GRANTED = "role.granted"
ACTION_ROLE_REVOKED = "role.revoked"
ACTION_USER_CREATED = "user.created"
ACTION_USER_SUSPENDED = "user.suspended"
ACTION_PASSWORD_CHANGED = "password.changed"

# The approval model. Segregation of duties is only checkable afterwards if the
# approval names an actor distinct from the one that raised the work.
ACTION_ACTION_APPROVED = "action.approved"
ACTION_ACTION_REJECTED = "action.rejected"
ACTION_ESCALATION_RESOLVED = "escalation.resolved"

# Work that changes what the system looks for or asserts.
ACTION_STEER_ISSUED = "steer.issued"
ACTION_THRESHOLD_CHANGED = "threshold.changed"
ACTION_KNOWLEDGE_SUPERSEDED = "knowledge.superseded"
ACTION_PROJECT_CREATED = "project.created"


def _digest(
    *,
    prev_hash: str,
    seq: int,
    company_id: str,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    reason: str,
    citation: str,
    occurred_at: datetime,
) -> str:
    """Hash every field, so changing any one of them breaks the chain.

    Serialised with sorted keys and no whitespace variation, so the digest
    depends on the values and never on how they were formatted.

    SCHEME 1, AND IT IS FROZEN. Rows already in the log were hashed by this
    exact function. Adding a field, renaming a key, or touching the separators
    would make every one of them fail to verify, and a chain that cannot verify
    its own history is worse than no chain -- it turns a real record into a
    false alarm nobody can distinguish from a real one. New fields go in
    _digest_v2. tests/test_audit_v2.py holds a frozen expected hash for a fixed
    input, so an edit here fails a test rather than silently invalidating the
    past.
    """
    payload = json.dumps(
        {
            "prev_hash": prev_hash,
            "seq": seq,
            "company_id": company_id,
            "actor": actor,
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "reason": reason,
            "citation": citation,
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest_v2(
    *,
    prev_hash: str,
    seq: int,
    company_id: str,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    reason: str,
    citation: str,
    occurred_at: datetime,
    actor_user_id: str | None,
    actor_kind: str | None,
    session_id: str | None,
    ip: str | None,
) -> str:
    """Scheme 2: the scheme-1 fields, plus who acted and from where.

    Attribution is INSIDE the hash, not beside it. A row that merely stored
    actor_user_id in a column would let anyone with write access rename the
    actor while the chain still verified, which is the same as having no
    attribution at all -- the log would keep asserting, with a valid hash, that
    a different person approved the action.

    The scheme number is not itself in the payload, and it does not need to be.
    The two field sets differ, so the two payloads differ, so a row whose
    digest_version is flipped from 2 to 1 or 1 to 2 recomputes to a different
    hash and is caught. tests/test_audit_v2.py proves that rather than assuming
    it.

    What this does NOT do. It does not check that actor_user_id names a real
    user, or that the session was live, or that the ip is the caller's rather
    than a proxy's. It fixes whatever the writer claimed, so that the claim
    cannot be changed afterwards.
    """
    payload = json.dumps(
        {
            "prev_hash": prev_hash,
            "seq": seq,
            "company_id": company_id,
            "actor": actor,
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "reason": reason,
            "citation": citation,
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
            "actor_user_id": actor_user_id,
            "actor_kind": actor_kind,
            "session_id": session_id,
            "ip": ip,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest_for_row(entry: AuditEvent) -> str:
    """Recompute one row's hash under the scheme that row says wrote it.

    An unknown scheme raises rather than falling back to either known one.
    Absence is denial: a row that cannot say how it was hashed cannot be
    verified, and answering "true" for it would let anyone bypass the chain by
    writing a version number nobody has implemented.
    """
    version = entry.digest_version
    if version == DIGEST_V1:
        return _digest(
            prev_hash=entry.prev_hash,
            seq=entry.seq,
            company_id=entry.company_id,
            actor=entry.actor,
            action=entry.action,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            reason=entry.reason,
            citation=entry.citation,
            occurred_at=entry.occurred_at,
        )
    if version == DIGEST_V2:
        return _digest_v2(
            prev_hash=entry.prev_hash,
            seq=entry.seq,
            company_id=entry.company_id,
            actor=entry.actor,
            action=entry.action,
            subject_type=entry.subject_type,
            subject_id=entry.subject_id,
            reason=entry.reason,
            citation=entry.citation,
            occurred_at=entry.occurred_at,
            actor_user_id=entry.actor_user_id,
            actor_kind=entry.actor_kind,
            session_id=entry.session_id,
            ip=entry.ip,
        )
    raise AuditTamperError(
        f"{entry.company_id}: seq {entry.seq} claims digest scheme "
        f"{version!r}, which this build cannot compute. Refusing to verify it "
        "under another scheme."
    )


def record_event(
    session: Session,
    *,
    company_id: str,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str,
    reason: str,
    citation: str = "",
    occurred_at: datetime | None = None,
    actor_user_id: str | None = None,
    actor_kind: str = ACTOR_SYSTEM,
    session_id: str | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Append one decision to the company's chain. The only way in.

    Always writes scheme 2. There is no way to append a scheme-1 row from
    application code, and there should not be: the old scheme exists to read the
    past, not to write more of it.

    NEVER PASS A SECRET. reason, citation and subject_id are stored verbatim and
    are readable by anyone who can read the log, which is the point of a log. A
    failed login records the account that was tried -- never the password tried
    against it, not even hashed, because a hash of a guessed password is still a
    guessable password sitting in an append-only table that by design cannot be
    redacted afterwards. Nothing here can detect a caller that ignores this;
    tests/test_audit_v2.py pins the shape of the failed-login row instead.

    actor_kind defaults to system, so a caller that has not thought about
    attribution records a machine rather than quietly claiming a person.
    """
    if not company_id:
        raise ValueError("company_id is required; an unscoped audit entry is a defect")
    if not actor:
        raise ValueError("actor is required; an unattributed decision is not auditable")
    if actor_kind not in ACTOR_KINDS:
        raise ValueError(
            f"actor_kind must be one of {ACTOR_KINDS}; got {actor_kind!r}. "
            "A misspelt kind writes a row no query for that kind will find."
        )

    # One spelling of absence. An empty string and NULL both mean "not
    # recorded", and keeping both would split every query over this column in
    # two without anyone noticing which half they got.
    actor_user_id = actor_user_id or None
    session_id = session_id or None
    ip = ip or None

    if actor_kind != ACTOR_USER and actor_user_id is not None:
        raise ValueError(
            "a system or model actor carries no user id; attributing a machine "
            "action to a person is a false attribution, and the audit log is "
            "the last place to make one. Record the session it ran under "
            "instead, and name the person in subject_id if they are the subject."
        )

    tail = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()

    seq = 1 if tail is None else tail.seq + 1
    prev_hash = "" if tail is None else tail.entry_hash
    stamp = occurred_at or datetime.now(timezone.utc)

    entry = AuditEvent(
        company_id=company_id,
        seq=seq,
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        reason=reason,
        citation=citation,
        occurred_at=stamp,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        session_id=session_id,
        ip=ip,
        prev_hash=prev_hash,
        digest_version=CURRENT_DIGEST_VERSION,
        entry_hash=_digest_v2(
            prev_hash=prev_hash,
            seq=seq,
            company_id=company_id,
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=reason,
            citation=citation,
            occurred_at=stamp,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            session_id=session_id,
            ip=ip,
        ),
    )
    session.add(entry)
    session.flush()
    return entry


def verify_chain(session: Session, company_id: str) -> bool:
    """Recompute every hash in one company's chain. Raise on the first break.

    Each row is recomputed under the scheme it names, so a chain that starts
    under scheme 1 and continues under scheme 2 verifies end to end. The join
    between them needs nothing special: prev_hash is a value the newer row
    copied from the older one, and a copied value does not know which function
    produced it.

    This walks the whole chain. It is the operation you run when someone
    disputes the record, not one to put in a request path.
    """
    if not company_id:
        raise ValueError("company_id is required; refusing an unscoped verification")

    events = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
        )
        .scalars()
        .all()
    )

    expected_prev = ""
    for position, entry in enumerate(events, start=1):
        if entry.seq != position:
            raise AuditTamperError(
                f"{company_id}: expected seq {position}, found seq {entry.seq}. "
                "A gap means a record was removed."
            )
        if entry.prev_hash != expected_prev:
            raise AuditTamperError(
                f"{company_id}: seq {entry.seq} does not link to its predecessor."
            )
        if _digest_for_row(entry) != entry.entry_hash:
            raise AuditTamperError(
                f"{company_id}: seq {entry.seq} was altered after it was recorded."
            )
        expected_prev = entry.entry_hash

    return True


def chain_head(session: Session, company_id: str) -> str:
    """The latest hash. Publishing this externally is what would close the gap."""
    if not company_id:
        raise ValueError("company_id is required")
    tail = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return "" if tail is None else tail.entry_hash


def event_count(session: Session, company_id: str) -> int:
    if not company_id:
        raise ValueError("company_id is required")
    return session.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.company_id == company_id)
    ).scalar_one()


# The columns attribution added, in the order they are added, with the DDL type
# each needs on an existing table. digest_version defaults to 1 in the database
# as well as in the model: a row already there was written under scheme 1, and a
# row inserted later by raw SQL that does not state a scheme is claiming
# nothing, so it must be read as the older one.
_ADDED_COLUMNS = (
    ("actor_user_id", "VARCHAR(64)"),
    ("actor_kind", "VARCHAR(16)"),
    ("session_id", "VARCHAR(64)"),
    ("ip", "VARCHAR(45)"),
    ("digest_version", "INTEGER NOT NULL DEFAULT 1"),
)


def migrate_audit_schema(engine) -> tuple[str, ...]:
    """Add the attribution columns to a log that predates them. Returns what it did.

    This is the whole migration, and what it does NOT do is the point. It adds
    columns and backfills digest_version to 1. It does not touch actor, action,
    reason, occurred_at, prev_hash or entry_hash -- the fields scheme 1 hashed
    -- so every existing row still verifies byte for byte afterwards. There is
    no rehash step and there must never be one.

    The other columns are left NULL on old rows rather than defaulted. A default
    of "system" would have every historical row claim a machine acted, which is
    a statement the record cannot support; NULL says the scheme of the day did
    not record it, which is true.

    Safe to run twice: it adds only what is missing. It returns the column names
    it added, so a caller can report what happened rather than assume. An empty
    result means the table was already current, or absent -- inspect the
    database if that distinction matters.

    Runs the DDL itself rather than through Alembic because there is no Alembic
    here (see requirements.txt). ALTER TABLE ADD COLUMN with a constant default
    is the one schema change SQLite does support in place.
    """
    inspector = inspect(engine)
    if not inspector.has_table(AuditEvent.__tablename__):
        return ()

    present = {column["name"] for column in inspector.get_columns("audit_events")}
    added: list[str] = []
    with engine.begin() as connection:
        for name, ddl_type in _ADDED_COLUMNS:
            if name in present:
                continue
            connection.execute(
                text(f"ALTER TABLE audit_events ADD COLUMN {name} {ddl_type}")
            )
            added.append(name)
        if added:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_audit_company_actor "
                    "ON audit_events (company_id, actor_user_id)"
                )
            )
    return tuple(added)


@event.listens_for(Session, "before_flush")
def _refuse_to_rewrite_history(session: Session, flush_context, instances) -> None:
    """Application code gets no UPDATE or DELETE path to the audit log.

    Registered on the Session class, so it holds for every session in the
    process rather than only the ones a careful caller remembered to protect.

    It does not stop a direct SQL statement, and it is not meant to. That is the
    hash chain's job, and the two are kept separate because they fail
    differently: this one refuses the mistake, the chain catches the attack.
    """
    for obj in session.dirty:
        if isinstance(obj, AuditEvent) and session.is_modified(obj):
            raise AuditTamperError(
                "audit events are append-only; a correction is a new superseding "
                "event, never an edit to the record it corrects"
            )
    for obj in session.deleted:
        if isinstance(obj, AuditEvent):
            raise AuditTamperError("audit events are append-only; they are never deleted")


__all__ = [
    "AuditTamperError",
    "ACTOR_USER",
    "ACTOR_SYSTEM",
    "ACTOR_MODEL",
    "ACTOR_KINDS",
    "DIGEST_V1",
    "DIGEST_V2",
    "CURRENT_DIGEST_VERSION",
    "ACTION_LOGIN_SUCCEEDED",
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGOUT",
    "ACTION_SESSION_EXPIRED",
    "ACTION_ROLE_GRANTED",
    "ACTION_ROLE_REVOKED",
    "ACTION_USER_CREATED",
    "ACTION_USER_SUSPENDED",
    "ACTION_PASSWORD_CHANGED",
    "ACTION_ACTION_APPROVED",
    "ACTION_ACTION_REJECTED",
    "ACTION_ESCALATION_RESOLVED",
    "ACTION_STEER_ISSUED",
    "ACTION_THRESHOLD_CHANGED",
    "ACTION_KNOWLEDGE_SUPERSEDED",
    "ACTION_PROJECT_CREATED",
    "record_event",
    "verify_chain",
    "chain_head",
    "event_count",
    "migrate_audit_schema",
]
