"""SQLAlchemy models. company_id is on every tenant-scoped row by design."""

from datetime import datetime, timezone

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, TypeDecorator
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


class Proceeding(Base):
    """One docket at one commission, tracked across its versions."""

    __tablename__ = "proceedings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    docket: Mapped[str] = mapped_column(String(128), index=True)
    commission: Mapped[str] = mapped_column(String(128))
    subject: Mapped[str] = mapped_column(String(512))


class Change(Base):
    """One typed difference between two versions of a proceeding.

    Offsets rather than passage ids, on both sides, because an offset into a
    frozen source is checkable by reading the bytes. A pure addition has no
    before offsets and a pure removal has no after offsets, so all four are
    nullable and a reader must not assume either side exists.
    """

    __tablename__ = "changes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    proceeding_id: Mapped[str] = mapped_column(ForeignKey("proceedings.id"), index=True)

    from_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    to_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))

    # added, removed or modified. The diff engine's vocabulary, unchanged.
    change_type: Mapped[str] = mapped_column(String(16))

    before_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    section: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # A float here and basis points on Claim.confidence_bp, on purpose. This
    # number is a diff diagnostic: it decides whether an alignment escalates,
    # and it is never quoted back to the analyst or covered by a hash over the
    # row. The number on a claim is both, so it gets the integer treatment.
    alignment_confidence: Mapped[float] = mapped_column(Float)

    # The model would fill this in. No model call exists yet, so it stays NULL
    # rather than being defaulted to a value that reads like a judgement.
    materiality: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # DRAFT or FINAL, copied from the to_version AT WRITE TIME per ADR-005.
    # Copied rather than joined so the status a decision was taken under is the
    # status stored beside it, even after the version row moves on.
    status: Mapped[str] = mapped_column(String(16))


Index("ix_changes_company_proceeding", Change.company_id, Change.proceeding_id)


class Claim(Base):
    """A statement the product would make, and the citation that has to earn it.

    Nothing here records whether the citation verified. Verification runs at
    read time against the stored source (app/state/claims.py), because a stored
    boolean is a promise about bytes that may since have changed, and the whole
    bet in ADR-003 is that the product re-reads rather than remembers.
    """

    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("changes.id"), index=True)

    statement: Mapped[str] = mapped_column(Text)

    citation_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    citation_start: Mapped[int] = mapped_column(Integer)
    citation_end: Mapped[int] = mapped_column(Integer)
    citation_quote: Mapped[str] = mapped_column(Text)

    # Which occurrence of the quote the claim relies on, zero-based. NULL where
    # the quote is unique. The same words in a different section mean a
    # different thing, so an ambiguous quote must name its occurrence.
    cited_occurrence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Basis points, 0..10000. An integer, not a float: float text differs
    # across platforms and across drivers, so any hash taken over this row
    # would differ too, and the audit chain would report tampering where none
    # happened. Integers are exact everywhere.
    confidence_bp: Mapped[int] = mapped_column(Integer)


class Escalation(Base):
    """A claim the product refused to assert, and why, in plain words.

    reason_code is for code to branch on; reason_text is for the analyst to
    read; detail carries the specifics -- what was quoted against what the
    source actually says.
    """

    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)

    reason_code: Mapped[str] = mapped_column(String(64))
    reason_text: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")

    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(256), nullable=True)


Index("ix_escalations_company_claim", Escalation.company_id, Escalation.claim_id)
