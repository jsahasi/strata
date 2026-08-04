"""SQLAlchemy models. company_id is on every tenant-scoped row by design."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
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


# ---------------------------------------------------------------------------
# The project workspace
#
# Everything above this line describes one change and the citation behind it.
# Everything below describes the work a person does around a set of them over
# months: a project, the changes attached to it, the questions still open, the
# plan, the re-runs that keep it current, and what the company learned.
#
# Two conventions run through this block.
#
# Ids. Anything a URL points at carries a string id the writer chooses, so a
# link stays stable. Rows that are only ever read in sequence -- an attachment,
# a turn in a thread -- take an autoincrement integer, like Passage and
# AuditEvent above, because their order is their meaning.
#
# Vocabularies. The small closed sets (a project's status, a step's state) are
# named here as tuples rather than left as free strings in the write layer, so
# there is one list to read and one place a new value has to be added.
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


PROJECT_STATUSES = ("active", "monitoring", "closed")
THREAD_STATUSES = ("open", "answered", "parked")
TURN_AUTHOR_KINDS = ("analyst", "system")
STEP_STATES = ("todo", "doing", "blocked", "done")
RUN_CADENCES = ("daily", "weekly", "on-filing")
KNOWLEDGE_KINDS = ("precedent", "definition", "mapping", "lesson")


class Project(Base):
    """A live line of work: one jurisdiction, one owner, a running list of changes.

    This is the unit the analyst actually lives in. A change is an event; a
    project is the thing the event lands on and keeps landing on, which is why
    the change list here is an association rather than a column -- the same
    change can bind two projects at once, and usually does.

    No counts are stored on this row. A stored count is a second copy of the
    truth that nothing recomputes, and the day it disagrees with the rows it
    counts, the row wins and the count is believed. project_card() in
    app/state/projects.py derives them instead.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    name: Mapped[str] = mapped_column(String(256))
    # The docket this project tracks, where it tracks one. Plenty do not --
    # an internal programme has no docket -- so this is nullable rather than
    # defaulted to a blank string that reads like a missing value.
    docket_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jurisdiction: Mapped[str] = mapped_column(String(128))
    # One of PROJECT_STATUSES. Checked on write, not at read time.
    status: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str] = mapped_column(String(256))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    summary: Mapped[str] = mapped_column(Text, default="")


Index("ix_projects_company_status", Project.company_id, Project.status)


class ProjectChange(Base):
    """One change attached to one project, with who attached it and when.

    An association row rather than a project_id column on Change, because a
    single change routinely binds several projects -- a definition that moves in
    one docket touches every project that quoted it -- and a column would force
    a choice between them. Attaching is also a judgement someone made, so it
    carries an actor and a timestamp; a foreign key would carry neither.

    Nothing here detaches. Removing an attachment would erase the fact that
    somebody once thought the change applied, which is exactly the question
    asked afterwards.
    """

    __tablename__ = "project_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("changes.id"), index=True)

    attached_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    attached_by: Mapped[str] = mapped_column(String(256))


# One attachment per pair. A change binding two projects is the design; the
# same change listed twice on one project is a double click.
Index(
    "ix_project_changes_pair",
    ProjectChange.project_id,
    ProjectChange.change_id,
    unique=True,
)


class ResearchThread(Base):
    """An open question against a project, and the record of chasing it.

    A thread is deliberately not a change. A change has a deterministic diff
    behind it and can be verified; a thread is enquiry, and its answer is worth
    only as much as the claims cited in its turns. Keeping the two as separate
    tables is what stops an unanswered question drifting into the change list
    and being read as a finding.
    """

    __tablename__ = "research_threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    question: Mapped[str] = mapped_column(Text)
    # One of THREAD_STATUSES.
    status: Mapped[str] = mapped_column(String(16))
    opened_by: Mapped[str] = mapped_column(String(256))

    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # Denormalised on purpose, and the one exception to the rule above: a
    # thread list sorts by it, and deriving it per row would mean a query per
    # thread. add_turn() is the only writer, so there is one place it can go
    # stale rather than several.
    last_activity_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)


Index(
    "ix_research_threads_company_project",
    ResearchThread.company_id,
    ResearchThread.project_id,
)


class ResearchTurn(Base):
    """One entry in a thread: an analyst's note, or something the system found.

    claim_id is nullable and is the only way a turn points at evidence. A turn
    that cites nothing is a note, and the interface must read it as one -- the
    citation is what separates a finding from an opinion, and a turn is allowed
    to be an opinion so long as it does not dress up as a finding.

    author_kind is stored rather than inferred from the author's name. Whether
    a person or the pipeline wrote a line is the first thing a reviewer wants
    to know, and it is not recoverable later from a string.
    """

    __tablename__ = "research_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("research_threads.id"))
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # One of TURN_AUTHOR_KINDS.
    author_kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    claim_id: Mapped[str | None] = mapped_column(
        ForeignKey("claims.id"), nullable=True, index=True
    )


# Covers the only read there is -- one thread's turns in order -- so thread_id
# needs no index of its own.
Index("ix_research_turns_thread_seq", ResearchTurn.thread_id, ResearchTurn.id)


class WorkPlan(Base):
    """What the project intends to do, as ordered steps someone owns."""

    __tablename__ = "work_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    title: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class WorkPlanStep(Base):
    """One step of a plan, owned by a named person, optionally tied to a change.

    ordinal, not a linked list and not a float: the order is data the analyst
    set, and renumbering a short list is cheaper than debugging a broken chain.

    change_id is nullable because plenty of steps answer no single change --
    "brief the trading desk" is real work with no diff behind it. Where a step
    does answer one, the link is what lets the product show why the step exists
    and quote the words that caused it.
    """

    __tablename__ = "work_plan_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("work_plans.id"), index=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    ordinal: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(256))
    # One of STEP_STATES.
    state: Mapped[str] = mapped_column(String(16))

    change_id: Mapped[str | None] = mapped_column(
        ForeignKey("changes.id"), nullable=True, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index("ix_work_plan_steps_plan_ordinal", WorkPlanStep.plan_id, WorkPlanStep.ordinal)


class ScheduledRun(Base):
    """A standing instruction to re-check a project's sources on a cadence.

    last_run_at is nullable and starts empty, which matters more than it looks.
    A schedule that has never fired and a schedule that fired an hour ago must
    not render the same, or a broken scheduler reads as a quiet week. The
    interface is expected to say "never run" rather than leave the field blank.

    last_result is a short line of plain words, not a status code, because the
    person reading it wants to know what happened and there is nothing to
    branch on.
    """

    __tablename__ = "scheduled_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    # One of RUN_CADENCES.
    cadence: Mapped[str] = mapped_column(String(16))
    last_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(UtcDateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_result: Mapped[str | None] = mapped_column(String(256), nullable=True)


Index(
    "ix_scheduled_runs_company_enabled", ScheduledRun.company_id, ScheduledRun.enabled
)


class KnowledgeItem(Base):
    """Something the company learned, kept as a version rather than a value.

    This is the compounding store: the precedents, the definitions, the mapping
    between a regulator's wording and the company's own, the lessons. It is the
    part the MRD calls the moat, and it only compounds if the history survives.

    So nothing here is edited. A revised belief is a NEW row, and the old row
    gets its superseded_by set to point at the replacement -- write-once, and
    the only field on this table that is ever written twice. body is never
    touched again. That is what makes "what did we believe in March, and on
    what" answerable in December, which is the question an auditor asks.

    project_id is nullable because some knowledge belongs to the company rather
    than to any one project -- how the company words its own load-study
    obligation is true everywhere, and copying it per project would give one
    fact several places to be wrong.
    """

    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )

    # One of KNOWLEDGE_KINDS.
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    source_claim_id: Mapped[str | None] = mapped_column(
        ForeignKey("claims.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    # Who put their name to it. Empty means nobody has, and an unconfirmed item
    # is not the same as a confirmed one -- the interface has to say which.
    confirmed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_items.id"), nullable=True
    )


Index(
    "ix_knowledge_company_project",
    KnowledgeItem.company_id,
    KnowledgeItem.project_id,
)


# ---------------------------------------------------------------------------
# The review centre
#
# The demo describes this surface as sources, analytics, expert review, all
# findings, questions, the collective take, deliverables, and "steer"
# directives. Every table below either holds evidence or synthesises it, and one
# rule shapes all of them: a surface that counts or synthesises must be able to
# say what it left out.
#
# That rule is why CollectiveTake and Deliverable carry two counts and not one.
# A take that says "rests on nine findings" is a different document from one
# that says "rests on nine findings and excludes four that failed
# verification", and only the second one can be handed to a regulator. The
# counts are written at compose time, from coverage_for_project(), because a
# take is a statement about what was known then -- recomputing it later would
# answer a question nobody asked.
#
# It is also why a steer is a row rather than a setting. A directive that
# changes what the system looks for, with no record of who issued it and when,
# cannot be reconciled afterwards with the findings it produced.
# ---------------------------------------------------------------------------


SOURCE_KINDS = ("internal", "external")
FINDING_STATUSES = ("open", "confirmed", "rejected", "superseded")
DELIVERABLE_KINDS = ("memo", "filing", "briefing", "register")
DELIVERABLE_STATES = ("draft", "in_review", "final")


class Source(Base):
    """Where evidence came from: the company's own words, or a filing.

    kind is internal or external, and the split is not decoration. An internal
    source is the company writing about itself -- an obligation register, a
    project note. An external one is a document the company does not control.
    Coverage reports the two separately because a synthesis resting only on
    internal sources has read nothing the regulator actually wrote, and that is
    invisible in a single total.

    version_id is set when the source was ingested, which is what makes it
    citable at an offset. It is NULL for a source that is only pointed at -- a
    docket number, a URL nobody has pulled in. Such a source can be listed and
    cannot carry a verified claim, and nothing here pretends otherwise.
    """

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    # Nullable: a source can belong to the company rather than to one project.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )

    # One of SOURCE_KINDS.
    kind: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(256))
    # A URL, a docket id, or a file path. Stored exactly as given. Rewriting it
    # to something tidier would break the one thing it is for: going back.
    locator: Mapped[str] = mapped_column(String(512))

    version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_versions.id"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    # Whether a person vouched for this source. It is not a verification result
    # and must never stand in for one -- trusting a source says nothing about
    # whether a quote taken from it matches the bytes at the cited offsets.
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)


Index("ix_sources_company_project", Source.company_id, Source.project_id)


class Finding(Base):
    """One thing the review centre would assert, and the claim that earns it.

    claim_id is nullable because a person can raise a finding from their own
    reading before any claim exists for it -- the demo's "make it easy for a
    human to inject knowledge". Such a finding is not evidence yet. Coverage
    counts it with the withheld ones until a verifying claim is attached, which
    is the same rule the rest of the product runs on, applied to human input
    rather than to the model's.

    status is a separate axis from verification and is deliberately not folded
    into it. A reviewer may reject a finding whose citation verifies perfectly,
    and a finding nobody has read yet may rest on a citation that holds. Mixing
    the two would mean neither number could be trusted.
    """

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    change_id: Mapped[str | None] = mapped_column(
        ForeignKey("changes.id"), nullable=True, index=True
    )
    claim_id: Mapped[str | None] = mapped_column(
        ForeignKey("claims.id"), nullable=True, index=True
    )

    headline: Mapped[str] = mapped_column(String(512))
    detail: Mapped[str] = mapped_column(Text, default="")

    raised_by: Mapped[str] = mapped_column(String(256))
    raised_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # One of FINDING_STATUSES.
    status: Mapped[str] = mapped_column(String(16))


Index("ix_findings_company_project", Finding.company_id, Finding.project_id)


class Question(Base):
    """Something the work needs answered, and whether it stops the work.

    blocking is the whole point of the table. Every project accumulates open
    questions; a handful of them mean the deliverable cannot honestly be
    written yet. A review centre that lists twenty questions without saying
    which three are load-bearing has moved the sorting back onto the analyst.

    Answering appends -- answer, answered_by, answered_at -- rather than
    clearing the row, so the question stays readable next to what was said
    about it. A question that vanishes when answered takes the reasoning with
    it.
    """

    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    body: Mapped[str] = mapped_column(Text)
    asked_by: Mapped[str] = mapped_column(String(256))
    asked_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    answered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    blocking: Mapped[bool] = mapped_column(Boolean, default=False)


Index("ix_questions_company_project", Question.company_id, Question.project_id)


class CollectiveTake(Base):
    """The synthesis: what the project believes right now, and what it excluded.

    findings_included and findings_withheld are written together at compose
    time and neither is optional. This is the table where the product's central
    rule becomes a column: a synthesis that hides what it could not
    substantiate is the exact failure Strata exists to prevent, and a schema
    that allowed the count to be absent would make that failure a matter of
    template discipline rather than of storage.

    The counts are a snapshot, not a live figure. They record what coverage
    said when the take was composed; the rows have moved since, and the take is
    a statement about the earlier moment. compose_take() in app/state/review.py
    is the only writer.

    A take is never edited. A new take supersedes the old one by setting the
    old row's superseded_by, so the record of what was believed last week
    survives being wrong.
    """

    __tablename__ = "collective_takes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    body: Mapped[str] = mapped_column(Text)
    composed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    composed_by: Mapped[str] = mapped_column(String(256))

    findings_included: Mapped[int] = mapped_column(Integer)
    findings_withheld: Mapped[int] = mapped_column(Integer)

    superseded_by: Mapped[str | None] = mapped_column(
        ForeignKey("collective_takes.id"), nullable=True
    )


Index(
    "ix_takes_company_project",
    CollectiveTake.company_id,
    CollectiveTake.project_id,
)


class Deliverable(Base):
    """What leaves the building: a memo, a filing, a briefing, a register.

    Carries the same two counts as a take, for the same reason and with more at
    stake. A deliverable is the artefact somebody outside this system reads,
    and it is the last place a silent omission can still be caught. The counts
    are frozen at creation because the document is a statement about what was
    known when it was written.

    approved_by is NULL until someone puts their name to it. Empty is not the
    same as approved, and the interface has to show which -- an unapproved
    draft that looks final is how a draft gets filed.
    """

    __tablename__ = "deliverables"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    title: Mapped[str] = mapped_column(String(256))
    # One of DELIVERABLE_KINDS.
    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text, default="")
    # One of DELIVERABLE_STATES.
    state: Mapped[str] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    findings_included: Mapped[int] = mapped_column(Integer, default=0)
    findings_withheld: Mapped[int] = mapped_column(Integer, default=0)


Index(
    "ix_deliverables_company_project",
    Deliverable.company_id,
    Deliverable.project_id,
)


class SteerDirective(Base):
    """A standing instruction that shifts what the research looks for.

    A row, with an issuer and a time, rather than a field on the project. The
    difference matters after the fact: a steer changes which findings appear,
    so reconciling a set of findings with the instruction that produced them
    needs the instruction to have a date. A setting overwritten in place cannot
    answer "what were we looking for in March".

    Revoking sets revoked_at and deletes nothing. The directive drops out of
    the active list and stays readable, because the findings it produced are
    still on the project and still have to be explainable.

    effect is filled in by whatever applied the directive -- in plain words,
    what changed as a result. It is NULL until then, and a NULL here means
    nobody has recorded an effect, never that there was none.
    """

    __tablename__ = "steer_directives"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)

    instruction: Mapped[str] = mapped_column(Text)
    issued_by: Mapped[str] = mapped_column(String(256))
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    applied_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index(
    "ix_steer_company_project",
    SteerDirective.company_id,
    SteerDirective.project_id,
)
