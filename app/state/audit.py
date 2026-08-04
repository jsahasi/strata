"""The audit trail, and the two mechanisms that keep it honest.

A history view that the writing process can edit proves nothing under dispute,
which is exactly the situation this product exists for: a regulator or an
auditor asking why the company said what it said, and when it knew. So the log
is append-only by refusal and tamper-evident by construction.

What this does NOT claim. The hash chain detects a record that was altered or
removed after the fact. It does not prevent it. Anyone who can write the whole
database file can recompute every hash from the tampered record forward, and
nothing here would notice. Defeating that needs the chain head published
somewhere the same attacker cannot reach -- external write-once storage, or
periodic notarisation. That is a production requirement and it is not built.
docs/security.html says the same thing rather than implying otherwise.
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.state.models import AuditEvent


class AuditTamperError(RuntimeError):
    """Raised when the log was altered, or when code tries to alter it."""


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
) -> AuditEvent:
    """Append one decision to the company's chain. The only way in."""
    if not company_id:
        raise ValueError("company_id is required; an unscoped audit entry is a defect")
    if not actor:
        raise ValueError("actor is required; an unattributed decision is not auditable")

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
        prev_hash=prev_hash,
        entry_hash=_digest(
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
        ),
    )
    session.add(entry)
    session.flush()
    return entry


def verify_chain(session: Session, company_id: str) -> bool:
    """Recompute every hash in one company's chain. Raise on the first break."""
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
        recomputed = _digest(
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
        if recomputed != entry.entry_hash:
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


@event.listens_for(Session, "before_flush")
def _refuse_to_rewrite_history(session: Session, flush_context, instances) -> None:
    """Application code gets no UPDATE or DELETE path to the audit log.

    Registered on the Session class, so it holds for every session in the
    process rather than only the ones a careful caller remembered to protect.
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
