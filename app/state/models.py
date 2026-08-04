"""SQLAlchemy models. company_id is on every tenant-scoped row by design."""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    docket: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(256))
    # DRAFT or FINAL. Explicit per ADR-005; never inferred at read time.
    status: Mapped[str] = mapped_column(String(16))
    source_text: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))


class Passage(Base):
    __tablename__ = "passages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)


Index("ix_passages_version_ordinal", Passage.version_id, Passage.ordinal)


class UtcDateTime(TypeDecorator):
    """Stores an aware UTC timestamp as ISO 8601 text and returns it aware.

    SQLite has no native timestamp type and SQLAlchemy's DateTime(timezone=True)
    hands back a naive datetime on this dialect. A naive timestamp in an audit
    record is a defect, not a formatting detail: it cannot be compared across
    systems, and "what did we know, and when" is the question this table exists
    to answer.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("audit timestamps must carry a timezone")
        return value.astimezone(timezone.utc).isoformat()

    def process_result_value(self, value, dialect):
        return None if value is None else datetime.fromisoformat(value)


class AuditEvent(Base):
    """Append-only, hash-chained record of every decision the system made.

    Two mechanisms, because they fail differently. The ORM guard in
    app/state/audit.py refuses an UPDATE or DELETE from application code, which
    stops mistakes. The hash chain detects a row rewritten out of band -- by a
    direct SQL statement, or by anyone who reaches the file -- which the guard
    cannot. Neither is sufficient alone; the chain is what survives a host
    compromise being discovered later.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    # Per-company, gapless from 1. A gap is a deletion.
    seq: Mapped[int] = mapped_column(Integer)

    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    # version_id:char_start:char_end, or empty where a decision cites nothing.
    citation: Mapped[str] = mapped_column(String(256), default="")

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64))


Index("ix_audit_company_seq", AuditEvent.company_id, AuditEvent.seq, unique=True)
