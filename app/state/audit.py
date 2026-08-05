"""The audit trail, and the three mechanisms that keep it honest.

A history view that the writing process can edit proves nothing under dispute,
which is exactly the situation this product exists for: a regulator or an
auditor asking why the company said what it said, and when it knew. So the log
is append-only by refusal and tamper-evident by construction.

ONE LOG. Logins, permission grants, approvals and state changes all land here.
A second table for "security events" would drift from this one, and the log
nobody reads is the one that goes wrong first -- silently, because nothing
compares them. Attribution is columns on this row, not a parallel record.

THREE DIGEST SCHEMES, ON PURPOSE. Version 1 hashes the ten original fields.
Version 2 hashes those ten plus the four attribution fields. Version 3 hashes
those fourteen plus the reversal pointer. Rows written before attribution
existed keep their v1 hashes and verify under v1 forever; nothing rewrites them.
This is best-practices.html section 27 pointed the other way round: a derived
corpus migrates all at once or not at all, and here the derived data IS the
evidence, so the answer is "not at all". Re-hashing the old rows under a newer
scheme would produce a chain that verifies and proves nothing, because the
process that verifies it would be the process that rewrote it. Each row
therefore states its own scheme in digest_version and verify_chain dispatches
per row. A chain that mixes all three verifies end to end: the prev_hash linkage
is a value copied forward and does not care which scheme computed it.

ROLLBACK IS A ROW. A decision somebody later took back is recorded by appending
an event that names the one it reverses, in reverts_event_id. The reverted row
is not edited, not deleted and not flagged, because the record of a correction
is exactly what an auditor asks for. The undo itself -- which decisions can be
taken back, and what state goes back with them -- lives in app/state/rollback.py.
This file owns only the chain: the pointer, its hash, and the two refusals that
stop a second row undoing the same decision or a row undoing another tenant's.

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

THE SCOPE GUARD IS IMPORTED, NOT RESTATED, AND THIS FILE WAS THE ONE THAT
RESTATED IT. Every other module under app/state routes its company_id through
app/state/queries.py::_require_scope. This one wrote `if not company_id:` at four
sites instead, and that substitution accepts every value the guard exists to
refuse: "   ", " MEP", "MEP%", 7. Live, before the fix:

    verify_chain(session, "   ")         -> True   ("chain verifies, 0 events")
    versions_for_company(session, "   ") -> ValueError

Two answers to one question about scope, and the audit log gave the reassuring
one. A blank or padded scope matches no row, so the chain walked zero events,
found nothing wrong with any of them, and returned True -- which a reader takes
as "this company's record is intact" when what happened is "I could not work out
whose record you meant". That is the worst place in the product for the guard to
be missing, because this is the module whose whole job is to be trustworthy under
dispute. Absence is denial: a scope this file cannot parse is refused and said
so, never reported as a chain that verifies over nothing.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.orm import Session

from app.state.models import AuditEvent
from app.state.queries import _require_scope


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
# 3: those fourteen plus reverts_event_id.
#
# WHY 3 IS CHOSEN PER ROW RATHER THAN BY DATE. CURRENT_DIGEST_VERSION stays at
# 2, and a row is hashed under 3 only when it carries a reversal pointer.
# Scheme 3 exists to cover one nullable column, so an ordinary row has nothing
# extra to cover and gains nothing from the wider payload. Every row already in
# the log stays exactly as it is.
#
# THE HOLE THAT LEAVES, AND HOW IT IS CLOSED. Schemes 1 and 2 do not cover
# reverts_event_id, so a pointer written onto one of their rows out of band
# would not break its hash -- the log would assert, with a valid hash, that a
# decision had been taken back by a row nobody wrote that way. _digest_for_row
# therefore REFUSES a v1 or v2 row that carries a pointer instead of verifying
# it. Under those schemes the column is storage the chain does not defend, and
# a field the chain does not defend is not evidence.
#
# DIGEST_V3 belongs here, beside its siblings. app/state/models.py declares the
# same number next to the column, because the column landed first and this file
# was being written elsewhere at the time; that line is meant to be deleted, not
# kept in step by hand. tests/test_rollback.py pins the two together until it is.
# ---------------------------------------------------------------------------

DIGEST_V1 = 1
DIGEST_V2 = 2
DIGEST_V3 = 3
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
#
# All six sit in the user. namespace, including the two about roles. A role
# grant is a fact about an account, and the person asking "what happened to this
# account" wants one prefix to search rather than three. app/state/identity.py
# imports these rather than restating them.
ACTION_USER_CREATED = "user.created"
ACTION_USER_SUSPENDED = "user.suspended"
# Suspension needs a way back, and the way back is a separate code rather than
# a second "suspended" row with a reason nobody parses. Reinstating somebody is
# the decision an investigation asks about, so it is searchable on its own.
ACTION_USER_REINSTATED = "user.reinstated"
ACTION_ROLE_GRANTED = "user.role_granted"
ACTION_ROLE_REVOKED = "user.role_revoked"
# A permission held directly, outside any role. Separate codes from the two
# above because the two acts answer different questions: a role grant says a
# person was put in a seat, a direct grant says somebody made an exception to
# the seat. An investigation looks for the exception first, and it cannot look
# for it if both spellings land under one action.
ACTION_PERMISSION_GRANTED = "user.permission_granted"
ACTION_PERMISSION_REVOKED = "user.permission_revoked"
ACTION_PASSWORD_CHANGED = "user.password_changed"

# A company composing its own role. subject_type is "role" rather than "user":
# nobody's access changed at the moment one was written, and recording it
# against a person would put an act with no subject on that person's record.
ACTION_ROLE_CREATED = "role.created"
ACTION_ROLE_EDITED = "role.edited"
# A fifth, which the handoff did not ask for. A deletion recorded as an edit is
# a deletion no query for a deletion can find, and "the role that could approve
# rate filings stopped existing" is exactly the event somebody comes looking
# for months later.
ACTION_ROLE_DELETED = "role.deleted"

# The approval model. Segregation of duties is only checkable afterwards if the
# approval names an actor distinct from the one that raised the work.
ACTION_ACTION_APPROVED = "action.approved"
ACTION_ACTION_REJECTED = "action.rejected"
ACTION_ESCALATION_RESOLVED = "escalation.resolved"

# The two codes the review queue actually writes when somebody closes a refusal:
# approved means the refusal was right, rejected means a person disputes it.
# Neither publishes the claim.
#
# THEY LIVE HERE BECAUSE THEY WERE ALREADY DRIFTING. app/web/views/review.py
# declares its own copies of these two strings and review_centre.py imports them
# from there, so the vocabulary this file is supposed to hold has a second home
# with the same two words in it. That is the failure the ACTION_ constants were
# consolidated to stop, and it bites harder here than usual: rollback matches a
# resolution by its action code, so a resolution written under a code this file
# does not know is a decision nobody can take back, with nothing saying so.
# review.py must import these rather than restate them;
# tests/test_rollback.py pins the two copies together until it does.
ACTION_ESCALATION_APPROVED = "escalation.approved"
ACTION_ESCALATION_REJECTED = "escalation.rejected"

# Undo, and the two ways it goes.
#
# decision.reverted -- somebody took back a decision, and the state that
# decision changed went back with it.
#
# decision.reversal_withdrawn -- somebody took back a REVERSAL. Nothing is
# reinstated: the subject stays where the reversal left it and needs a fresh
# decision. It is a separate code from the first on purpose, so that a reader
# scanning for undone decisions is never handed a row that undid an undo and
# restored nothing. Reading the second as the first would put a decision back in
# force that nobody re-made, which is the one thing a log must never allow.
ACTION_DECISION_REVERTED = "decision.reverted"
ACTION_REVERSAL_WITHDRAWN = "decision.reversal_withdrawn"

# Authorisation. A refusal is a decision the system made about a person, so it
# belongs in the same chain as the approvals it withholds -- "who was turned
# away, and from what" is asked in the same breath as "who approved this".
#
# approval.waived is the demo downgrade recording itself. It is a separate code
# from action.approved on purpose: action.approved means an obligation owner
# signed off, approval.waived means the check that should have stopped them was
# switched off by configuration. Reading the second as the first would make a
# waived separation of duties look like a clean approval a year later, which is
# the one reading a log must never allow.
ACTION_ACCESS_DENIED = "access.denied"
ACTION_APPROVAL_WAIVED = "approval.waived"

# The one judgement a model makes in this product, recorded like any other
# decision. app/interpretation/propose.py asks whether a change matters and
# writes the answer beside Change.materiality; this is the code that write
# appends under.
#
# IT IS HERE BECAUSE IT WAS ALREADY DRIFTING, which is the same sentence the
# escalation pair above carries and the reason it is worth repeating.
# tests/test_policy.py types "change.materiality_set" as a bare literal in two
# places -- it was the obvious spelling, so somebody wrote it before any code
# did -- and app/interpretation/propose.py named that as one of the three
# things persistence needed. A retyped action code does not fail: it writes a
# row that hashes perfectly and that no query for that action will ever return,
# so the decision is in the log and invisible. tests/test_audit.py now walks
# every module under app/ for a literal that matches a constant here, so the
# next one fails a test rather than waiting to be read.
#
# THE SUBJECT IS THE CHANGE, NOT THE CLAIM. A materiality verdict interprets
# the change itself, and app/auth/policy.py already treats an act on the change
# underneath a claim as authorship of that claim -- so the person or model that
# set materiality cannot then approve what follows from it. That control reads
# this code, which is the second reason it may only have one spelling.
ACTION_MATERIALITY_SET = "change.materiality_set"

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


def _digest_v3(
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
    reverts_event_id: int | None,
) -> str:
    """Scheme 3: the scheme-2 fields, plus the event this row takes back.

    The pointer is INSIDE the hash for the same reason attribution is. A column
    beside the hash could be re-pointed by anyone with write access, and the
    chain would still verify while the log said a different decision had been
    undone -- or that this row had undone nothing at all, leaving a decision
    somebody took back apparently standing. Both readings are the log asserting,
    with a valid hash, something nobody wrote.

    Written verbatim rather than by extending _digest_v2, and the repetition is
    deliberate: v2 is frozen evidence for every row already in the log, and a
    shared helper is a single edit away from moving all of them at once.

    What this does NOT do. It fixes the pointer the writer supplied. It does not
    prove the target exists, that it sits in the same company, or that reverting
    it was right. record_event checks the first two on write; nothing checks the
    third, because nothing can.
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
            "reverts_event_id": reverts_event_id,
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

    The same rule applied to a field rather than a row: a v1 or v2 row carrying
    a reversal pointer is refused outright. Those schemes do not hash that
    column, so the pointer on such a row is an assertion nothing covers, and
    verifying the row would report the chain as sound while it carried a claim
    the chain never defended. Application code cannot produce one -- record_event
    writes scheme 3 the moment a pointer is present -- so a row in that state
    arrived out of band.
    """
    version = entry.digest_version
    if version in (DIGEST_V1, DIGEST_V2) and entry.reverts_event_id is not None:
        raise AuditTamperError(
            f"{entry.company_id}: seq {entry.seq} says it was hashed under scheme "
            f"{version} yet carries a reversal of event {entry.reverts_event_id}. "
            "Neither scheme covers that field, so the pointer is not evidence and "
            "the row is refused rather than verified."
        )
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
    if version == DIGEST_V3:
        return _digest_v3(
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
            reverts_event_id=entry.reverts_event_id,
        )
    raise AuditTamperError(
        f"{entry.company_id}: seq {entry.seq} claims digest scheme "
        f"{version!r}, which this build cannot compute. Refusing to verify it "
        "under another scheme."
    )


# The two refusals a reversal pointer can hit on write. Constants because
# app/state/rollback.py raises the same words earlier, before it touches any
# state, and two spellings of one refusal are two messages a caller has to
# match separately.
NO_SUCH_EVENT = "no such audit event for this company"
ALREADY_REVERTED = (
    "this event was already reverted; a second reversal would leave two rows "
    "undoing one decision with nothing saying which of them governs"
)


def _reversing_row(
    session: Session, company_id: str, event_id: int
) -> AuditEvent | None:
    """The row in this company that reverts event_id, or None. One query, one home.

    Scoped, so a reversal written in another tenant's chain does not count as
    one here. audit_events.id is unique across tenants and nothing in the schema
    stops a cross-company pointer, so this filter is the check, not decoration.
    """
    return session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .where(AuditEvent.reverts_event_id == event_id)
        .order_by(AuditEvent.seq)
        .limit(1)
    ).scalar_one_or_none()


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
    reverts_event_id: int | None = None,
) -> AuditEvent:
    """Append one decision to the company's chain. The only way in.

    Writes scheme 2, or scheme 3 when the row takes another event back. There is
    no way to append a scheme-1 row from application code, and there should not
    be: the old scheme exists to read the past, not to write more of it.

    reverts_event_id is checked here as well as in app/state/rollback.py, and
    the repetition is the point: this is the chokepoint every write goes
    through, so a caller that never heard of the rollback module still cannot
    write a pointer into another tenant's chain or stack a second undo on one
    decision. What this does NOT check is whether the state that decision
    changed went back with it. That is rollback's job, and a pointer written
    from here alone records an undo that never happened.

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
    # The one definition of a usable scope, not a second opinion about it. A row
    # written under "   " lands in a chain nothing can later read back, because
    # every read here refuses that value -- an event in the log and invisible,
    # which is the failure the ACTION_ constants above were consolidated to stop,
    # arrived at from the scope instead of the action.
    _require_scope(company_id)
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

    if reverts_event_id is not None:
        if not isinstance(reverts_event_id, int) or isinstance(reverts_event_id, bool):
            raise ValueError(
                f"reverts_event_id must be an audit event id; got "
                f"{reverts_event_id!r}. A value SQLite cannot match points at "
                "nothing, and the row would claim an undo of no event at all."
            )
        target = session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == company_id)
            .where(AuditEvent.id == reverts_event_id)
        ).scalar_one_or_none()
        # One message for "not here" and "not yours". Which of the two it is
        # would itself tell a caller that another tenant holds that id.
        if target is None:
            raise ValueError(NO_SUCH_EVENT)
        if _reversing_row(session, company_id, reverts_event_id) is not None:
            raise ValueError(ALREADY_REVERTED)

    tail = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()

    seq = 1 if tail is None else tail.seq + 1
    prev_hash = "" if tail is None else tail.entry_hash
    stamp = occurred_at or datetime.now(timezone.utc)

    fields = {
        "prev_hash": prev_hash,
        "seq": seq,
        "company_id": company_id,
        "actor": actor,
        "action": action,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "reason": reason,
        "citation": citation,
        "occurred_at": stamp,
        "actor_user_id": actor_user_id,
        "actor_kind": actor_kind,
        "session_id": session_id,
        "ip": ip,
    }

    # The scheme follows the row's own content. A row with no pointer has
    # nothing scheme 3 would add, so it stays where every other row is; a row
    # with one is hashed under the scheme that covers it. Nothing here can write
    # a pointer that the row's own digest does not fix.
    if reverts_event_id is None:
        digest_version = CURRENT_DIGEST_VERSION
        entry_hash = _digest_v2(**fields)
    else:
        digest_version = DIGEST_V3
        entry_hash = _digest_v3(**fields, reverts_event_id=reverts_event_id)

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
        digest_version=digest_version,
        entry_hash=entry_hash,
        reverts_event_id=reverts_event_id,
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

    A SCOPE THIS CANNOT PARSE IS REFUSED, NOT VERIFIED. True here means "every
    row in this company's chain recomputes to the hash it carries". Over a scope
    that matched no row it would mean "there were no rows", and the two sentences
    read identically to the person who asked. Whitespace, padding and a LIKE
    wildcard all used to reach this function and all used to answer True.
    """
    _require_scope(company_id)

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
    """The latest hash. Publishing this externally is what would close the gap.

    The empty string means this company has recorded nothing yet, and it must
    only ever mean that. A scope the guard cannot parse would return the same
    empty string, and a head published under it would be a notarised statement
    about a tenant nobody named.
    """
    _require_scope(company_id)
    tail = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return "" if tail is None else tail.entry_hash


def event_count(session: Session, company_id: str) -> int:
    """How many events this company has recorded.

    Zero is a fact about a tenant, so the scope has to be one the guard accepts.
    Several tests read this before and after an act to prove nothing was written
    into another company's chain; under an unparsable scope that comparison is
    0 == 0 and proves nothing at all.
    """
    _require_scope(company_id)
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
    # The reversal link (models.py::AuditEvent.reverts_event_id). Added here
    # because the ORM names every mapped column in every SELECT: without this
    # line, one new column makes every audit read on an existing database fail
    # with "no such column", which is the whole reason this migration exists.
    # Plain INTEGER and no REFERENCES clause, matching actor_user_id above:
    # SQLite does not enforce foreign keys unless asked, and ALTER TABLE is not
    # the place to start.
    ("reverts_event_id", "INTEGER"),
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
        # The names and types are the module constants above, never anything a
        # caller supplied. A column name cannot be a bind parameter, so DDL has
        # to be built as text; keeping the values in one frozen tuple is what
        # makes that safe rather than a habit.
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
            # "Was this decision taken back" is asked of every decision the
            # review queue shows, so it cannot be a table scan. The name is the
            # one SQLAlchemy generates from index=True on the column, so a
            # migrated database and a freshly created one carry the same index
            # under the same name rather than two that only a DBA can tell apart.
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_audit_events_reverts_event_id "
                    "ON audit_events (reverts_event_id)"
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
    "DIGEST_V3",
    "CURRENT_DIGEST_VERSION",
    "NO_SUCH_EVENT",
    "ALREADY_REVERTED",
    "ACTION_LOGIN_SUCCEEDED",
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGOUT",
    "ACTION_SESSION_EXPIRED",
    "ACTION_ROLE_GRANTED",
    "ACTION_ROLE_REVOKED",
    "ACTION_PERMISSION_GRANTED",
    "ACTION_PERMISSION_REVOKED",
    "ACTION_ROLE_CREATED",
    "ACTION_ROLE_EDITED",
    "ACTION_ROLE_DELETED",
    "ACTION_USER_CREATED",
    "ACTION_USER_SUSPENDED",
    "ACTION_USER_REINSTATED",
    "ACTION_PASSWORD_CHANGED",
    "ACTION_ACTION_APPROVED",
    "ACTION_ACTION_REJECTED",
    "ACTION_ESCALATION_RESOLVED",
    "ACTION_ESCALATION_APPROVED",
    "ACTION_ESCALATION_REJECTED",
    "ACTION_DECISION_REVERTED",
    "ACTION_REVERSAL_WITHDRAWN",
    "ACTION_ACCESS_DENIED",
    "ACTION_APPROVAL_WAIVED",
    "ACTION_MATERIALITY_SET",
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
