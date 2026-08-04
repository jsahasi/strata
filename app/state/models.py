"""SQLAlchemy models. company_id is on every tenant-scoped row by design."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    text,
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


# The digest scheme that covers the reversal link below.
#
# IT BELONGS BESIDE DIGEST_V1 AND DIGEST_V2 IN app/state/audit.py, and it is
# here only because the column it describes is here and that file is being
# written by somebody else this hour. Whoever adds _digest_v3 must move this
# line rather than restate the number: two spellings of one scheme is the drift
# the ACTION_ constants were consolidated to stop, and here the two copies would
# disagree about which fields a hash covers.
#
# Nothing writes a v3 row until that function exists. CURRENT_DIGEST_VERSION
# stays at 2, and _digest_for_row already refuses a scheme it cannot compute
# rather than guessing one, so a premature v3 row would fail verification loudly.
DIGEST_V3 = 3


class AuditEvent(Base):
    """Append-only, hash-chained record of every decision the system made.

    Two mechanisms, because they fail differently. The ORM guard in
    app/state/audit.py refuses an UPDATE or DELETE from application code, which
    stops mistakes. The hash chain detects a row rewritten out of band -- by a
    direct SQL statement, or by anyone who reaches the file -- which the guard
    cannot. Neither is sufficient alone; the chain is what survives a host
    compromise being discovered later.

    ATTRIBUTION, AND WHY IT IS VERSIONED. The four actor columns below were
    added after rows already existed. Their values are inside the hash for the
    rows written since; they cannot be inside the hash for the rows written
    before, because those hashes are the evidence and re-hashing them would
    destroy the thing being kept. So each row states which scheme hashed it in
    digest_version, and app/state/audit.py verifies each row under its own
    scheme. Attribution is covered by the digest, not merely stored beside it:
    a row whose actor_user_id is edited to name someone else breaks the chain.

    WHAT THESE COLUMNS DO NOT PROVE. They record what the writing process
    believed about the actor. They do not prove a person was at the keyboard,
    and they do not survive an attacker who can rewrite the whole file and
    recompute every hash forward from the row they touched.

    ROLLBACK IS A ROW, NOT AN ERASURE. reverts_event_id names the event this
    one undoes. Both stay in the chain, both stay verifiable, and the row being
    reversed is not edited, not deleted and not marked -- it does not know it
    was reversed, because writing anything on it would be the rewrite the whole
    table exists to prevent. A reader asking "does this still stand" looks for
    a later row pointing back at it.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    # Per-company, gapless from 1. A gap is a deletion.
    seq: Mapped[int] = mapped_column(Integer)

    # The display string: an email, a service name. Human-readable, and not an
    # identity -- two people can share a mailbox and a person can be renamed.
    actor: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    # version_id:char_start:char_end, or empty where a decision cites nothing.
    citation: Mapped[str] = mapped_column(String(256), default="")

    # The identity, as opposed to the label in `actor`. Nullable, because the
    # pipeline and the model act with nobody behind them, and because a login
    # attempt against an account that does not exist has no user to point at.
    # NULL means "no person is claimed here", never "the person is unknown but
    # there was one".
    #
    # Deliberately not a foreign key. A user row can be deleted; an audit row
    # never can, and a cascade that nulled this column would be an UPDATE to a
    # record that must not change -- it would break the hash of every row it
    # touched. The id is copied in as a value, and a reader that cannot resolve
    # it must say so rather than drop the attribution.
    actor_user_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # user, system or model -- ACTOR_KINDS in app/state/audit.py. Stored rather
    # than guessed from the actor string, for the same reason
    # ResearchTurn.author_kind is: whether a person or a machine did this is the
    # first question asked of any row, and it is not recoverable later from a
    # name. NULL on rows written before attribution existed; NULL there means
    # the scheme of the day did not record it, not that a machine acted.
    actor_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # The login session this happened under. Not a foreign key, for the reason
    # given above: sessions are pruned, audit rows are not.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Where the request came from. 45 characters holds an IPv6 address with an
    # embedded IPv4 one. Stored exactly as the server saw it; a proxy in front
    # of this makes it the proxy's address, and nothing here can tell.
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64))

    # Which scheme computed entry_hash. 1 is the original field set; 2 adds the
    # four attribution columns. The default is 1 in the model and in the DDL on
    # purpose: a row that arrives without stating its scheme is older than the
    # scheme change, and reading it as the newer one would report tampering on
    # an untouched record.
    digest_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    # The event this one reverses. NULL on every row that reverses nothing,
    # which is nearly all of them.
    #
    # A REAL FOREIGN KEY, unlike actor_user_id above, and the difference is the
    # reason given there: a user row can be deleted and an audit row never can,
    # so pointing at one is safe where pointing at the other was not.
    #
    # WHAT THE KEY DOES NOT CHECK. audit_events.id is unique across every
    # tenant, so nothing here stops a row in one company naming a row in
    # another. The writer has to check that both sit in the same chain, and a
    # reader that finds a cross-company reversal must refuse to read it as one
    # rather than resolve it. Same-company-ness is a rule, not a constraint.
    #
    # IT MUST GO INSIDE THE DIGEST -- see DIGEST_V3 above. Left outside, anyone
    # with write access could re-point a reversal at a different event and the
    # chain would still verify, which is the exact failure the v2 decision was
    # taken to prevent: a log that keeps asserting, with a valid hash, something
    # nobody wrote. Until _digest_v3 exists, this column is storage the chain
    # does not defend, and no product surface may treat it as evidence.
    reverts_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_events.id"), nullable=True, index=True
    )


Index("ix_audit_company_seq", AuditEvent.company_id, AuditEvent.seq, unique=True)
# "What did this person do here" -- scoped by company like every other read.
Index("ix_audit_company_actor", AuditEvent.company_id, AuditEvent.actor_user_id)


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

    ROUTING. assigned_to_user_id is who is holding it. NULL means nobody is,
    and that is the state an escalation is born in and returns to when the
    person it named leaves. Nobody holding it is not the same as nobody being
    needed: an unassigned escalation is work the product has failed to route,
    and the interface has to say so rather than leave the row looking calm.

    It is an id and not a name, unlike resolved_by beside it. resolved_by is a
    label somebody typed and can name two people; this one has to resolve to an
    account, because the next thing done with it is deciding whether that
    account may approve. The names differ so the two can never be confused at a
    call site -- a display string written into an id column would be accepted
    by SQLite without complaint and route nowhere.
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

    assigned_to_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # When it landed on that desk. NULL wherever assigned_to_user_id is NULL;
    # the pair is written together or not at all, because an assignment time
    # with no assignee describes an event nobody can name.
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index("ix_escalations_company_claim", Escalation.company_id, Escalation.claim_id)
# "What is on my desk", and the unassigned pile, which is the same query with a
# NULL. Both are read per company, like everything else.
Index(
    "ix_escalations_company_assigned",
    Escalation.company_id,
    Escalation.assigned_to_user_id,
)


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
# Named members, not bare strings in the tuple, because two tables now default
# to one of them and a defaulted literal is a string with no single home.
AUTHOR_ANALYST = "analyst"
AUTHOR_SYSTEM = "system"
TURN_AUTHOR_KINDS = (AUTHOR_ANALYST, AUTHOR_SYSTEM)
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


# ---------------------------------------------------------------------------
# Identity and access
#
# docs/security.html names three roles -- analyst, obligation owner, admin --
# and rests the whole approval design on segregation of duties: the analyst who
# interprets a change is never the person who approves the action that follows.
# That control is a data structure, not a paragraph. The tables below are where
# it becomes one, so a reviewer can read the grid of who may do what instead of
# taking the document's word for it.
#
# Four conventions run through this block.
#
# Secrets are never stored in a form that can be replayed. User rows hold a
# scrypt hash and its salt, never a password. Session rows hold a SHA-256 of the
# bearer token, never the token: an attacker who reads the whole table still
# cannot present a valid session, which is the only property that makes a
# database read less than a total compromise.
#
# History survives revocation. A revoked role grant keeps its row and gains a
# revoked_at; a revoked session keeps its row and gains a reason. "Who could do
# what, when" is the question asked after an incident, and a DELETE is what
# makes it unanswerable.
#
# Cost parameters travel with the hash. kdf_params sits on the user row rather
# than in a module constant, so raising the cost later re-hashes nobody: an old
# hash still verifies under the parameters it was made with. A KDF pinned to a
# global constant forces a choice between locking every existing user out and
# never raising the cost, and the second is what actually happens.
#
# Two tables carry no company_id, on purpose, and the exception is narrow.
# Permission and RolePermission hold vocabulary -- the codes the product knows
# and the grid mapping them onto role names -- in the same sense as
# PROJECT_STATUSES above. Neither holds a fact about a tenant, so scoping them
# would attach a company to a definition rather than to data. Every path that
# crosses from vocabulary into tenant space does so through Role, UserRole and
# User, all three of which are scoped and all three of which are filtered on
# the same company in app/state/identity.py.
# ---------------------------------------------------------------------------


STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_INVITED = "invited"
USER_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_INVITED)

ROLE_ANALYST = "analyst"
ROLE_OBLIGATION_OWNER = "obligation_owner"
ROLE_ADMIN = "admin"
SYSTEM_ROLE_NAMES = (ROLE_ANALYST, ROLE_OBLIGATION_OWNER, ROLE_ADMIN)

# Verb on noun, and stable. These strings end up in the audit log and in role
# grants that outlive any one release, so renaming one is a migration rather
# than a rename -- which is the reason they are listed here once and never
# spelled inline at a call site.
PERMISSION_CODES = (
    "proceeding.read",
    "change.read",
    "claim.read",
    "action.propose",
    "action.approve",
    "action.reject",
    "escalation.resolve",
    "steer.issue",
    "project.create",
    "knowledge.write",
    "threshold.set",
    "user.manage",
    # Pull a colleague onto the item that needs them. Held apart from
    # user.manage on purpose: user.manage creates any account at will, and
    # user.invite creates one narrow account attached to one piece of work.
    # Folding them would have meant every analyst who may invite a colleague
    # could also grant themselves admin, which is the escalation path the
    # invite design refuses. See the Invitation table at the foot of this file.
    "user.invite",
    "audit.read",
    # Design and activate the approval route: the five /admin/workflows calls.
    # Deliberately NOT an approval permission. Whoever draws the route decides
    # who gets asked; whoever holds action.approve decides the answer. Handing
    # one person both would let them route an approval to themselves, which is
    # the segregation of duties this grid exists to express, applied to the
    # thing that does the routing.
    "workflow.manage",
)


class User(Base):
    """A person who can hold a session, scoped to exactly one company.

    One row per person per company. Someone who works for two tenants gets two
    rows and two passwords, which is deliberate: a single identity spanning
    tenants would make company_id a property of the session rather than of the
    account, and every read in this codebase trusts that it is a property of
    the row.

    What this row does NOT protect against. It stores a scrypt hash, so reading
    the table does not hand over passwords -- but a weak password is still a
    weak password, and nothing here enforces length, rotation, or reuse against
    a breach corpus. failed_attempts and locked_until give a login path
    somewhere to record throttling; they are storage, not the throttle itself,
    and a login path that never writes them leaves the columns permanently at
    zero and NULL rather than failing loudly. There is no second factor.

    email is stored already normalised -- trimmed and lower-cased by
    create_user -- because the unique index below is the only thing standing
    between one person and two accounts, and SQLite compares text
    case-sensitively. Normalising at read time instead would mean the index
    guarded a different string from the one anybody looks up.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # 320 is the longest address RFC 5321 permits. Stored normalised.
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(256))

    # Hex of the scrypt output and of the salt. Hex rather than raw bytes so
    # the value compares exactly across drivers and platforms, the same reason
    # Claim.confidence_bp is an integer.
    password_hash: Mapped[str] = mapped_column(String(256))
    password_salt: Mapped[str] = mapped_column(String(64))
    # JSON: n, r, p, dklen. Per user, so the cost can be raised for new
    # passwords without invalidating any hash already written.
    kdf_params: Mapped[str] = mapped_column(Text)

    # One of USER_STATUSES. invited means the row exists and the person has not
    # accepted; suspended means the account is kept for the audit trail and must
    # not authenticate. Neither is the same as absent, and a login path has to
    # tell all three apart.
    status: Mapped[str] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # NULL until the first successful login. Never logged in and logged in a
    # year ago are different facts about an account, and a default of "now"
    # would erase the difference on the day the row was written.
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


# One account per address per tenant. The same address in two companies is two
# people as far as this schema is concerned, which is the point of the pair.
Index("ix_users_company_email", User.company_id, User.email, unique=True)


class Role(Base):
    """A named bundle of permissions, either global or belonging to one tenant.

    company_id NULL means a system role every tenant gets -- analyst,
    obligation_owner, admin. A tenant that needs its own bundle writes a row
    carrying its company_id, and app/state/identity.py resolves a name to the
    tenant's own row first and the system row second.

    What this does NOT protect against. The unique index below cannot stop two
    system roles sharing a name: SQL treats NULLs as distinct in a unique
    index, so (NULL, 'analyst') twice is legal to the database. Seeding is
    idempotent by name in identity.py, which is where the guarantee actually
    lives; the index catches the tenant-scoped half of the problem only.
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # NULL means a system role, shared by every tenant.
    company_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")


Index("ix_roles_scope_name", Role.company_id, Role.name, unique=True)


class Permission(Base):
    """One thing the product can be asked to allow. Vocabulary, not tenant data.

    A code is verb on noun -- action.approve, threshold.set -- and it is stable
    across releases because role grants and audit entries both reference it by
    string. There is no company_id here for the reason set out at the head of
    this block: a permission code is a definition, and a definition scoped to a
    tenant would have to be re-stated per tenant to mean the same thing.
    """

    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # One of PERMISSION_CODES.
    code: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class RolePermission(Base):
    """Which codes a role carries. The segregation of duties control, as rows.

    A composite natural key rather than a surrogate id, so granting the same
    permission to the same role twice is a no-op the database refuses rather
    than a duplicate row every later count has to de-duplicate.

    The absence of a row is the control. The analyst role has no
    action.approve, and that missing row is what stops the person who
    interpreted a change from approving the action that follows from it. A
    reviewer checking the security document's central claim reads this table.
    """

    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(
        ForeignKey("permissions.id"), primary_key=True
    )


class UserRole(Base):
    """One grant of one role to one user, with who granted it and when.

    Revoking sets revoked_at and deletes nothing. That is not tidiness: an
    investigation asks what a person could do at the moment they did something,
    and a schema that deletes grants can only answer what they can do now. The
    same row therefore appears in the history of a permission the user no
    longer holds, and every read that decides authorisation has to filter on
    revoked_at rather than on the row's existence.

    There is no unique index over (user_id, role_id). A grant, a revoke and a
    re-grant are three legitimate rows for the same pair. Uniqueness applies
    only to the active grant, which is enforced in identity.py::grant_role by
    returning the existing active grant instead of writing a second one.

    Who revoked a grant is not a column here; it is the actor on the audit
    event that identity.py::revoke_role appends. One log, not two.
    """

    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), index=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    granted_by: Mapped[str] = mapped_column(String(256))
    granted_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


Index("ix_user_roles_company_user", UserRole.company_id, UserRole.user_id)


class LoginSession(Base):
    """A live session, stored as a hash of its token so the row cannot be replayed.

    token_hash is SHA-256 of a token from secrets.token_urlsafe. A plain hash
    is right here and would be wrong on User.password_hash, and the difference
    is worth stating rather than looking like an inconsistency: a session token
    is 256 bits this process generated at random, so there is no dictionary to
    run against it and a slow KDF would only tax every authenticated request. A
    password is short, human-chosen and often reused, so it needs a KDF that
    makes guessing expensive.

    What this does NOT protect against. Anyone holding the token itself holds
    the session -- hashing protects the database, not the wire, and transport
    security is what protects the wire. Nothing here binds a session to the ip
    or user_agent it was created from; both are recorded so an incident can be
    read afterwards, and treating a change in either as proof of theft breaks
    legitimate users on mobile networks more often than it catches anybody.

    expires_at is written at creation and never extended in place by the
    design, so a stolen token has a fixed ceiling on its usefulness.
    last_seen_at moves; the expiry does not.
    """

    __tablename__ = "login_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # SHA-256 hex of the bearer token. The token itself is shown once, to the
    # browser that created it, and is never stored anywhere.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    # 45 characters is the longest an IPv6 address gets. Both fields are
    # evidence for an investigation, never an authorisation input.
    ip: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(String(512), default="")

    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Plain words -- "user signed out", "password changed", "revoked by admin".
    # A revoked session with no reason cannot be told apart from a bug.
    revoked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)


Index(
    "ix_login_sessions_company_user",
    LoginSession.company_id,
    LoginSession.user_id,
)


# ---------------------------------------------------------------------------
# Routing: what the company must do, who owns it, and who is asked
#
# Everything above this line ends at an escalation: the product refused to
# assert something, and a person has to look. This block is how that reaches
# the right person and what happens when it does not.
#
# An obligation is the company's own duty in the company's own words, and it is
# the join a docket change lands on. data/company_context.json already carries
# eight of them with owners; these columns are that file's shape, not an
# invention beside it.
#
# Four rules run through the block.
#
# A user reference is an id and its name ends in _user_id. The older tables
# spell a person as a display string in columns ending _by -- resolved_by,
# raised_by, composed_by -- and those stay as they are. New columns that decide
# routing or authorisation hold an account id, because the next question asked
# of them is whether that account may act, and a name cannot answer it. Keeping
# the two suffixes apart is what stops a display string being written into an
# id column, which SQLite would accept in silence.
#
# NULL routes nowhere. An obligation whose owner left keeps its row and loses
# its owner; an escalation nobody holds says so. Absence is denial here as it
# is everywhere else: unroutable work is escalated to a human, never quietly
# assigned to the last person who touched it.
#
# The graph tables carry no company_id. WorkflowStep and WorkflowEdge are
# meaningless outside the workflow that owns them and are never read without
# it, so tenancy comes from that row through a join -- the same argument
# Passage takes from DocumentVersion, and for the same reason: a second copy of
# the company on a child row is a column two writers can let drift. Every other
# table here is read on its own and carries its own company_id.
#
# A vocabulary is a tuple with named members. The engine compares against these
# constants rather than typing the strings, because a misspelt outcome does not
# raise -- it writes a row that no query for that outcome will ever return.
# ---------------------------------------------------------------------------


WORKFLOW_DRAFT = "draft"
WORKFLOW_ACTIVE = "active"
WORKFLOW_ARCHIVED = "archived"
WORKFLOW_STATUSES = (WORKFLOW_DRAFT, WORKFLOW_ACTIVE, WORKFLOW_ARCHIVED)

# What a step does when its clock runs out. The wire contract's three words.
TIMEOUT_REMIND = "remind"
TIMEOUT_ESCALATE = "escalate"
TIMEOUT_BYPASS = "bypass"
STEP_TIMEOUT_ACTIONS = (TIMEOUT_REMIND, TIMEOUT_ESCALATE, TIMEOUT_BYPASS)

# How a step decides who is asked. Two of the four are whole values; the other
# two are prefixes on a role name or an account id, so the vocabulary is
# half-closed and both halves are named here rather than parsed from a literal
# at a call site. escalate_to uses the same two prefixes.
ASSIGNEE_OBLIGATION_OWNER = "obligation_owner"
ASSIGNEE_UNASSIGNED = "unassigned"
ASSIGNEE_ROLE_PREFIX = "role:"
ASSIGNEE_USER_PREFIX = "user:"
ASSIGNEE_RULE_LITERALS = (ASSIGNEE_OBLIGATION_OWNER, ASSIGNEE_UNASSIGNED)
ASSIGNEE_RULE_PREFIXES = (ASSIGNEE_ROLE_PREFIX, ASSIGNEE_USER_PREFIX)

# Where a run got to. completed means the route reached its end. It does NOT
# mean every step was approved -- a run can reach the end with a step nobody
# answered. Whether anyone actually said yes is a question only the step rows
# can answer, and WorkflowStepRun.outcome is where it is answered.
WORKFLOW_RUN_RUNNING = "running"
WORKFLOW_RUN_COMPLETED = "completed"
WORKFLOW_RUN_REJECTED = "rejected"
WORKFLOW_RUN_CANCELLED = "cancelled"
WORKFLOW_RUN_STATUSES = (
    WORKFLOW_RUN_RUNNING,
    WORKFLOW_RUN_COMPLETED,
    WORKFLOW_RUN_REJECTED,
    WORKFLOW_RUN_CANCELLED,
)

# WHAT HAPPENED AT ONE STEP. Exactly one of these means a person said yes.
#
#   approved   a named account approved it.
#   rejected   a named account refused it.
#   bypassed   NOBODY ANSWERED. The step ran out of time, on_timeout said
#              bypass, and the workflow moved on without it.
#   escalated  nobody answered in time and the step passed to somebody else. A
#              new step run carries it from there, so who was asked first and
#              did not reply stays on the record.
#   cancelled  the run was stopped before anyone answered.
#
# NULL is not in this tuple and is not a value: it means the step is open, so a
# reader must not treat "no outcome yet" as any outcome at all.
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_BYPASSED = "bypassed"
OUTCOME_ESCALATED = "escalated"
OUTCOME_CANCELLED = "cancelled"
STEP_RUN_OUTCOMES = (
    OUTCOME_APPROVED,
    OUTCOME_REJECTED,
    OUTCOME_BYPASSED,
    OUTCOME_ESCALATED,
    OUTCOME_CANCELLED,
)


class Obligation(Base):
    """Something the company must do, in the company's own words, with an owner.

    The wording is the company's and not the regulator's, and that gap is the
    product. data/company_context.json says "post security" where the docket
    says "post collateral"; joining the two is the work, and a table that stored
    the docket's wording instead would have thrown away the half a person
    recognises.

    OWNERSHIP IS NULLABLE, AND THAT IS THE POINT. An owner who leaves the
    company takes their user row's usefulness with them, not the duty. The row
    stays, owner_user_id goes NULL, and the obligation becomes visible and
    unroutable -- which is a state the interface must show as work, because an
    obligation nobody owns is how a filing gets missed. A schema that made the
    owner mandatory would force a lie: some name, any name, in a column a
    reviewer would then read as accountability.

    project_id is nullable for the same reason it is on Source and
    KnowledgeItem: some duties belong to the company rather than to any one
    project, and copying them per project would give one fact several places to
    be wrong.

    WHAT IS NOT HERE. The owner's job title, which is a fact about the person
    and belongs on the person, not restated on every duty they hold. The
    docket sections the company context lists against each obligation, because
    those are version-tagged prose ("6.1 (v1/v2)") and the real mapping is the
    change_obligations table below, where it can be queried and attributed.
    """

    __tablename__ = "obligations"

    # OBL-001, from the company context. A URL points at it, so the id is the
    # one the source file already gives rather than one generated here.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # The company's own sentence, loaded from internal_wording in
    # data/company_context.json. A sentence rather than a heading, because that
    # is what the source holds and shortening it here would lose the wording
    # the join has to work against.
    title: Mapped[str] = mapped_column(String(512))

    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )

    # DOC-2 and the like: the internal document this duty was written down in.
    # NOT a foreign key -- those documents have no table, they are entries in
    # data/company_context.json, and naming the column source_document_id would
    # promise a join nothing can satisfy.
    source_document_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


# "What does this person own", and the unowned pile, which is the same query
# with a NULL.
Index("ix_obligations_company_owner", Obligation.company_id, Obligation.owner_user_id)


class ChangeObligation(Base):
    """One docket change bearing on one company obligation, and who said so.

    Many to many in both directions, and the corpus needs both: CHG-5 touches
    OBL-001 and OBL-008, while OBL-001 is touched by CHG-5 and CHG-1. A column
    on either table would force a choice between them.

    An association row rather than a bare pair, for the reason ProjectChange
    gives: mapping a change to an obligation is a judgement somebody made, and
    in this product it is the load-bearing one. The regulator's words and the
    company's do not share a vocabulary -- "post collateral" against "post
    security" -- so the mapping is reasoning, not string matching, and the row
    has to say whose reasoning it was.

    mapped_by_kind is why the row is worth more than the pair. A mapping a
    person made and a mapping the pipeline proposed must not read alike, and
    the difference is not recoverable later from a name. It reuses
    TURN_AUTHOR_KINDS rather than declaring a parallel tuple with the same two
    words. The default is the machine, following record_event: a caller that
    has not thought about attribution records a machine rather than quietly
    claiming a person did the thinking.

    Nothing here unmaps. A mapping somebody later disagrees with is a fact
    about what was believed, and deleting it erases the reason an action was
    taken. Withdrawing one is a new row somewhere that says so, never a DELETE
    here.
    """

    __tablename__ = "change_obligations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("changes.id"), index=True)
    obligation_id: Mapped[str] = mapped_column(ForeignKey("obligations.id"), index=True)

    mapped_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    mapped_by: Mapped[str] = mapped_column(String(256))
    # One of TURN_AUTHOR_KINDS.
    mapped_by_kind: Mapped[str] = mapped_column(String(16), default=AUTHOR_SYSTEM)


# One mapping per pair. The same change against the same obligation twice is a
# double click, and a duplicate would double every count taken over this table.
Index(
    "ix_change_obligations_pair",
    ChangeObligation.change_id,
    ChangeObligation.obligation_id,
    unique=True,
)


class ApprovalWorkflow(Base):
    """One approval route: a graph of steps an escalation is walked through.

    ONE IS ACTIVE PER COMPANY, AND THE DATABASE SAYS SO. The partial unique
    index below allows any number of drafts and archived routes and at most one
    row per company whose status is active. Enforcing it in the write layer
    instead would mean two admins activating at once could both succeed, and
    the product would then have two live routes and no way to say which one
    ran. What the index cannot catch is a status spelt some other way -- a row
    saying "Active" slips past it -- so the vocabulary check on write is the
    other half, and neither half is sufficient alone.

    A RUN PINS THE ROUTE IT STARTED UNDER, AND THE ROUTE STOPS CHANGING.
    Editing is allowed while status is draft and never afterwards. Activation
    validates and freezes; changing a live route means copying it into a new
    draft, activating that, and archiving the old row, which supersedes_id
    records. A run therefore names a workflow whose steps can no longer move,
    so nothing an admin does tonight can change what a run in flight is doing
    or what a finished run recorded.

    This is the frozen v1 digest argument in another table. The alternative --
    keeping one mutable row and storing a copy of the graph on each run -- puts
    the same graph in two places, and the day they disagree the copy is
    believed while the rows are read. Freezing the original and writing a new
    one forward keeps a single copy of every version, all of them queryable,
    none of them rewritten. What the SCHEMA contributes is the status column,
    the supersedes chain and the index; the refusal to write a step against a
    non-draft workflow lives in the write layer, because no constraint
    available here can express a rule about a parent row's column. That is a
    real gap and it is stated rather than implied.

    There is no archived_at. The moment a route stopped being live is the
    moment its successor was activated, which the successor's activated_at
    already records exactly; storing it twice would give one fact two homes.
    """

    __tablename__ = "approval_workflows"

    # WF-0001. A URL points at it -- the canvas editor, the graph endpoint --
    # so it takes a chosen string id and the link stays good.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    name: Mapped[str] = mapped_column(String(256))
    # One of WORKFLOW_STATUSES. No default: a route's state is a decision, and
    # the caller says which. Checked on write, like every other vocabulary here.
    status: Mapped[str] = mapped_column(String(16))

    # NULL where the seed or a migration built the route rather than a person.
    # NULL means no person is claimed, never that the person is unknown --
    # AuditEvent.actor_user_id makes the same distinction for the same reason.
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # NULL until it goes live. A draft that has never run and a route that ran
    # for a year must not render alike.
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # The route this one replaces. The same write-once shape as
    # KnowledgeItem.superseded_by, pointed the other way: a new version names
    # its predecessor, so a chain of versions reads backwards from the live one
    # and no row is edited to record being replaced.
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("approval_workflows.id"), nullable=True
    )


Index(
    "ix_workflows_company_status",
    ApprovalWorkflow.company_id,
    ApprovalWorkflow.status,
)
# At most one active route per company. Partial, so drafts and archives are
# unlimited. Rendered on SQLite and on PostgreSQL; both support it, which keeps
# the constraint with the schema rather than in whichever process happens to be
# writing.
Index(
    "uq_one_active_workflow_per_company",
    ApprovalWorkflow.company_id,
    unique=True,
    sqlite_where=ApprovalWorkflow.status == WORKFLOW_ACTIVE,
    postgresql_where=ApprovalWorkflow.status == WORKFLOW_ACTIVE,
)


class WorkflowStep(Base):
    """One node of a route: who is asked, how long they have, what happens then.

    THE COLUMNS ARE THE WIRE CONTRACT, FIELD FOR FIELD, so the graph endpoint
    serialises this row without a translation table in between. A translation
    table is where two teams' vocabularies drift.

    THE ID IS LOCAL TO THE WORKFLOW. The canvas numbers its own nodes STP-1,
    STP-2, and every route starts at STP-1, so the primary key is
    (workflow_id, id). Making step ids globally unique would have forced the
    editor to invent qualified ids and the contract to carry them, and copying
    a route to a new version would have had to renumber every node -- the one
    operation that must produce an identical graph.

    A HALF-FINISHED DRAFT SAVES WITHOUT INVENTING ANYTHING. The contract says a
    draft may be saved while invalid, so every field the admin has yet to decide
    is NULL: no approval_hours, no on_timeout, no remind_every_hours. A default
    of 24 hours or of "remind" would be an answer nobody gave, sitting in a
    column a reviewer would read as a decision. Activation is where this bites
    -- it must refuse a step whose approval_hours is NULL, whose on_timeout is
    NULL, whose on_timeout is remind with no remind_every_hours, and whose
    on_timeout is escalate with no escalate_to.

    assignee_rule is the exception and it defaults to "unassigned", because the
    contract already has a word for nobody-yet and one spelling of absence is
    better than two. NULL and "unassigned" would split every query over this
    column without anyone noticing which half they got. An unassigned step is
    still a step activation should refuse: a node that cannot name who is asked
    cannot route.
    """

    __tablename__ = "workflow_steps"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("approval_workflows.id"), primary_key=True
    )
    # STP-1, chosen by the canvas. Unique within its workflow, not beyond it.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    label: Mapped[str] = mapped_column(String(256), default="")

    # obligation_owner, unassigned, role:<role_name> or user:<user_id> --
    # ASSIGNEE_RULE_LITERALS and ASSIGNEE_RULE_PREFIXES above. A rule and not an
    # account: obligation_owner resolves to a different person per escalation,
    # which is the whole reason the route is worth drawing once.
    assignee_rule: Mapped[str] = mapped_column(
        String(128), default=ASSIGNEE_UNASSIGNED
    )

    # Hours, an integer greater than zero. NULL means the admin has not said.
    approval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # One of STEP_TIMEOUT_ACTIONS. NULL means undecided, which is not "remind".
    on_timeout: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # role:<role_name> or user:<user_id>. NULL unless on_timeout is escalate,
    # and the contract types it nullable for exactly that case.
    escalate_to: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Used only when on_timeout is remind. Integer greater than zero.
    remind_every_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Where the node sits on the canvas. Integers, and 0,0 is a position rather
    # than a missing value, so these default instead of going NULL.
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)


class WorkflowEdge(Base):
    """One arrow from one step to another, inside one route.

    A composite natural key rather than a surrogate id, the same choice
    RolePermission makes and for the same reason: the same arrow drawn twice is
    a double click, and the database refusing it is cheaper than every later
    count having to de-duplicate.

    Both ends are composite foreign keys into workflow_steps, so an edge cannot
    name a step in a different route. SQLite does not enforce foreign keys
    unless asked, so on the demo database a draft can be saved carrying an edge
    whose step was deleted a moment ago. That is survivable -- a draft is
    allowed to be invalid -- but it means activation has to check that every
    edge names a step that exists rather than trusting the constraint.
    """

    __tablename__ = "workflow_edges"

    workflow_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    to_step_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(["workflow_id"], ["approval_workflows.id"]),
        ForeignKeyConstraint(
            ["workflow_id", "from_step_id"],
            ["workflow_steps.workflow_id", "workflow_steps.id"],
        ),
        ForeignKeyConstraint(
            ["workflow_id", "to_step_id"],
            ["workflow_steps.workflow_id", "workflow_steps.id"],
        ),
    )


class WorkflowRun(Base):
    """One escalation being walked through one route.

    workflow_id pins the version. The route it names cannot change once
    activated, so a run in flight and a run finished last month both read the
    graph they actually ran under -- see the note on ApprovalWorkflow.

    current_step_id is NULL twice over: before the run has reached a step, and
    after it has left the last one. Those are different facts and the status
    column is what tells them apart, so nothing may infer "finished" from a
    missing step.

    status says where the run got to and NOT whether anybody approved
    anything. A run can be completed with a step nobody answered, and the
    escalation behind it is only truly cleared when no step was bypassed. That
    question is answered by the step rows below, never by this column.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    escalation_id: Mapped[str] = mapped_column(
        ForeignKey("escalations.id"), index=True
    )
    current_step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # One of WORKFLOW_RUN_STATUSES.
    status: Mapped[str] = mapped_column(String(16))

    __table_args__ = (
        ForeignKeyConstraint(["workflow_id"], ["approval_workflows.id"]),
        # The step it is standing on has to belong to the route it pinned.
        ForeignKeyConstraint(
            ["workflow_id", "current_step_id"],
            ["workflow_steps.workflow_id", "workflow_steps.id"],
        ),
    )


Index("ix_workflow_runs_company_status", WorkflowRun.company_id, WorkflowRun.status)


class WorkflowStepRun(Base):
    """What happened at one step of one run. The row a reviewer reads.

    OUTCOME IS THE FIELD THAT MATTERS, AND BYPASSED IS NOT APPROVED. A bypassed
    step was NOT approved: nobody answered it, its clock ran out, on_timeout
    said bypass and the workflow moved on without it. It is recorded as a
    separate value from approved so that everything downstream -- the run
    summary, the escalation, a deliverable that cites the approval, an auditor
    a year later -- can see that a step was skipped rather than signed. Folding
    the two into one value, or reporting "completed" without saying which steps
    were answered, is how an unapproved action ends up filed as an approved
    one. Exactly one value in STEP_RUN_OUTCOMES means a person said yes.

    NULL OUTCOME MEANS OPEN. Not approved, not refused: waiting. Anything
    counting approvals has to exclude it rather than treat absence as consent.

    A STEP MAY RUN MORE THAN ONCE. When a step times out and escalates, this
    row closes with outcome escalated and a new row opens for the same step
    with the new assignee, so who was asked first and did not answer stays on
    the record. That is why the key is an autoincrement integer over a run's
    history rather than the (run, step) pair -- the pair is not unique, and a
    schema that assumed it was would have to overwrite the first attempt to
    record the second.

    acted_by_user_id is NULL wherever nobody acted, which is every bypassed,
    escalated and cancelled row. A bypassed row naming an actor would be a
    false attribution: the clock is not a person.

    due_at is written when the step is assigned, from the step's approval_hours
    at that moment. It is a copy of a derived value and it earns its place --
    the deadline the person was actually given is the deadline they should be
    held to, and recomputing it later would answer a question about the graph
    rather than about what was promised.
    """

    __tablename__ = "workflow_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Carried rather than joined from the run, unlike the graph tables above:
    # "what is on my desk" is the most frequent read in the product, it is per
    # company and per person, and forcing it through two joins is where a
    # missing scope creeps in.
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    # The step in the run's pinned workflow. Not a foreign key: the composite
    # one would need this row to carry workflow_id as well, a third copy of a
    # value the run already holds. The writer takes it from the run.
    step_id: Mapped[str] = mapped_column(String(64))

    assigned_to_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # One of STEP_RUN_OUTCOMES, or NULL while the step is open.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    acted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    # How many reminders went out. Zero is a fact, not a missing value.
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)


# "What is waiting on me": one company, one person, no outcome yet.
Index(
    "ix_workflow_step_runs_desk",
    WorkflowStepRun.company_id,
    WorkflowStepRun.assigned_to_user_id,
    WorkflowStepRun.outcome,
)
# One run's history, in the order it happened.
Index("ix_workflow_step_runs_run_seq", WorkflowStepRun.run_id, WorkflowStepRun.id)


# ---------------------------------------------------------------------------
# Chat, and what a person says about it afterwards
#
# Clarke is the softest surface in the product. Everywhere else a person clicks
# a row and reads a citation beside it; here they type a loose question and get
# prose back, and prose is where a withheld claim leaks out inside a summary.
# app/chat/persona.py holds the behaviour that stops it. These tables hold what
# happened, so a reviewer can check the behaviour rather than take its word.
#
# Three rules shape the block.
#
# A COUNT AND AN ABSENCE ARE DIFFERENT FACTS, AND THEY GET DIFFERENT VALUES.
# withheld_count is NULL on a person's turn and on a clerk turn nobody
# measured; it is 0 on a clerk turn that measured and withheld nothing. Folding
# the two would report full coverage for a turn that was never counted, which is
# the failure ADR-22 exists to prevent, arriving through the one surface where
# nobody would notice. tools_used works the same way: [] means no tool ran, NULL
# means nobody recorded what ran.
#
# A SNAPSHOT, NOT A LOOKUP. Feedback.context holds the turns as they stood when
# the thumb went down. CollectiveTake freezes its counts for the same reason: a
# complaint is about a moment, and resolving it against a live transcript
# answers a question nobody asked.
#
# TWO STRUCTURED COLUMNS ARE JSON RATHER THAN TEXT, unlike User.kdf_params
# above, and the difference is worth stating so it does not read as drift.
# kdf_params is a fixed four-key record that exactly one function reads.
# tools_used and context are variable-length lists that go out on the wire
# unchanged and are read by every transcript and triage screen there is. A
# column that hands back a list is one json.loads nobody can forget, and a
# forgotten one produces a reply that looks right and carries a string where the
# contract promises a list.
#
# THERE IS NO PER-USER TRUST TIER, AND THAT IS A DECISION.
#
# The concierge project carries one -- logger, contributor, superuser -- to
# govern how far a person's approved feedback may travel. It does not belong
# here, for four reasons that are about this product rather than about taste.
#
#   1. This codebase already has an authorisation system: PERMISSION_CODES,
#      Role, RolePermission and UserRole, with the segregation of duties the
#      security document rests on expressed as the absence of a row. A tier on
#      the user row would be a second vocabulary answering the same question,
#      and the day the two disagree the code path that asked the wrong one
#      wins. Project stores no counts and a run stores no copy of its graph for
#      the same reason: one fact, one home.
#   2. The tier governs a pipeline this product does not have. In concierge,
#      approved feedback flows onward and the tier is the ceiling on that flow.
#      Here a thumbs-down writes a row. A person triages it into an
#      ImprovementItem, and a person writes the change. Nothing a user submits
#      reaches the product without somebody deciding, so there is no distance
#      for a ceiling to limit.
#   3. What a regulated workspace actually needs is already stronger. A tier is
#      prior restraint -- decide in advance how far someone's input may go. The
#      control here is attribution after the fact: every row names a user and a
#      company, and every status change is an audit event on a hash-chained
#      log that cannot be quietly rewritten. That is what answers the auditor's
#      question, and it answers it for people no tier anticipated.
#   4. If the answer later is "some people may triage and others may not", that
#      is a permission code -- feedback.triage -- added to PERMISSION_CODES and
#      granted through the grid that already exists. Three tiers on a user row
#      cannot express "may triage but may not close"; the grid does it for
#      nothing.
#
# WHAT THIS CONCEDES. Nothing here rate-limits submission, so any signed-in
# person can flood the queue. The mitigation is that a submission costs nothing
# and changes nothing on its own, and a flood is visible in the queue and
# attributable per user. It is a real limit and it is stated rather than
# designed around.
#
# Ids follow the convention at the head of the workspace block, with one turn.
# ChatMessage is read in sequence like ResearchTurn, which would make it an
# autoincrement integer -- but a thumb attaches to a message id, so the id
# crosses to a browser and comes back on the feedback POST, and it takes a
# chosen string. Order then has to come from somewhere else, which is what
# ordinal is for.
# ---------------------------------------------------------------------------


# Imported here rather than in the block at the top of the file, so that
# appending this section touched no line anybody else wrote. It belongs up
# there, and whoever next edits the import list should move it.
from sqlalchemy import JSON  # noqa: E402


CHAT_ROLE_PERSON = "person"
CHAT_ROLE_CLERK = "clerk"
CHAT_ROLES = (CHAT_ROLE_PERSON, CHAT_ROLE_CLERK)

FEEDBACK_KIND_FEEDBACK = "feedback"
FEEDBACK_KIND_BUG_REPORT = "bug_report"
FEEDBACK_KINDS = (FEEDBACK_KIND_FEEDBACK, FEEDBACK_KIND_BUG_REPORT)

# A thumb, and nothing else. NULL is the third state and is not in this tuple:
# a bug report carries no rating, and no rating is not a neutral one.
RATING_UP = "up"
RATING_DOWN = "down"
FEEDBACK_RATINGS = (RATING_UP, RATING_DOWN)

FEEDBACK_NEW = "new"
FEEDBACK_TRIAGED = "triaged"
FEEDBACK_IN_PROGRESS = "in_progress"
FEEDBACK_RESOLVED = "resolved"
FEEDBACK_WONT_FIX = "wont_fix"
FEEDBACK_STATUSES = (
    FEEDBACK_NEW,
    FEEDBACK_TRIAGED,
    FEEDBACK_IN_PROGRESS,
    FEEDBACK_RESOLVED,
    FEEDBACK_WONT_FIX,
)

IMPROVEMENT_BUG = "bug"
IMPROVEMENT_ENHANCEMENT = "enhancement"
IMPROVEMENT_GUIDANCE = "guidance"
IMPROVEMENT_CATEGORIES = (
    IMPROVEMENT_BUG,
    IMPROVEMENT_ENHANCEMENT,
    IMPROVEMENT_GUIDANCE,
)

# Where the work got to. dropped is not rejected: a rejected item is one a
# reviewer refused, and a dropped one was approved and then abandoned. Keeping
# both words means "why is this not being done" has an answer.
IMPROVEMENT_OPEN = "open"
IMPROVEMENT_IN_PROGRESS = "in_progress"
IMPROVEMENT_DONE = "done"
IMPROVEMENT_DROPPED = "dropped"
IMPROVEMENT_STATUSES = (
    IMPROVEMENT_OPEN,
    IMPROVEMENT_IN_PROGRESS,
    IMPROVEMENT_DONE,
    IMPROVEMENT_DROPPED,
)

# What a person decided about it, which is a different axis from the one above.
# One spelling only: an IMPROVEMENT_REVIEW_* alias beside these would be two
# names for one vocabulary, and two names drift.
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_DECISIONS = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED)

# Three levels and no fourth. An urgent tier is where every item ends up within
# a month, and a scale nobody believes sorts nothing.
PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
IMPROVEMENT_PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH)


class ChatSession(Base):
    """One person's conversation with Clarke, in one company.

    A chosen string id, because a URL points at a transcript.

    last_turn_at is NULL until somebody speaks, following ScheduledRun's
    last_run_at: a session opened and never used must not render as a
    conversation that happened the moment the row was written. A list of recent
    conversations reads that NULL as "nothing here", not as a date.

    There is no title, no summary and no message count. A count on this row is
    a second copy of what chat_messages already says, and Project explains at
    length why this codebase does not keep those.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # NULL means nobody has spoken yet. Never a stand-in for started_at.
    last_turn_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


# "My conversations", scoped by company like every other read.
Index("ix_chat_sessions_company_user", ChatSession.company_id, ChatSession.user_id)


class ChatMessage(Base):
    """One turn: what the person asked, or what Clarke answered and on what.

    THE ID IS A CHOSEN STRING BECAUSE A THUMB ATTACHES TO IT. It goes out in
    the turn response as message_id and comes back on the feedback POST, so it
    crosses a trust boundary in both directions. That is also why company_id
    sits on this row rather than being reached through the session: the id
    arrives off the wire, and the scope check is then one filter on the row a
    caller already has, not a join a caller has to remember. Passage takes the
    opposite choice for the opposite reason -- nothing hands a passage id to a
    browser. The writer copies company_id from the session and nothing else may
    write it.

    ordinal, not the timestamp, is the order. A writer that stamps the question
    and the answer from one clock read gives both the same created_at, and a
    tied transcript renders the answer above the question. The unique index
    below refuses a repeated position rather than storing an order nobody can
    resolve.

    TWO COLUMNS ARE NULL ON A PERSON'S TURN AND MUST NOT BE READ AS ZERO.
    tools_used records what was consulted -- the wire's `used` list, straight
    out, one entry per tool with what it found. [] means no tool ran, which is
    what a refused turn looks like. withheld_count records how many claims the
    turn refused to assert. 0 is a measurement and NULL is the absence of one,
    and a clerk turn holding NULL is a defect in the writer: it means nothing
    counted, so nothing may report coverage for that turn. Absence is denial
    here as everywhere -- the reader escalates rather than assuming zero.

    surface is the screen the person was on when they said it, carried from the
    turn request. It is here because it is the only place Feedback.surface can
    come from for a thumbs-down: the feedback POST carries a message id and a
    rating, and nothing else. "" means the caller recorded no surface, which is
    a gap in the caller and not something to guess at.
    """

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id"), index=True
    )
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # 1, 2, 3 within the session. The transcript's order.
    ordinal: Mapped[int] = mapped_column(Integer)

    # One of CHAT_ROLES. person or clerk, and the words are the product's own:
    # "user" and "assistant" belong to a model API, and this row outlives the
    # provider it was written against.
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    surface: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    # [{"tool": name, "found": n}]. NULL on a person's turn.
    tools_used: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # NULL on a person's turn. On a clerk turn, 0 is a count and NULL is a bug.
    withheld_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


# One turn per position. A second row claiming a taken place is a double
# submit, and the database refusing it is cheaper than a transcript nobody can
# put in order afterwards.
Index(
    "uq_chat_messages_session_ordinal",
    ChatMessage.session_id,
    ChatMessage.ordinal,
    unique=True,
)


class Feedback(Base):
    """What a person said about a turn, or a bug they reported from a screen.

    Singular table name, because "feedbacks" is not a word.

    ONE TABLE FOR TWO THINGS, AND kind IS WHAT KEEPS THEM APART. A thumbs-down
    carries a rating, a chat message and a snapshot of the exchange. A bug
    report carries a title and none of those. They share a queue, a status
    lifecycle and a triage screen, which is what makes one table right; the
    columns that belong to only one of them are nullable and kind says which
    shape a row is. A reader that ignores kind and finds a NULL rating must not
    read it as neutral -- it means the row is a bug report.

    context is the exchange as it stood when the thumb went down, written once
    and never recomputed. A reviewer opening the row a week later reads what the
    person was complaining about rather than what the transcript says today.
    NULL where there was no chat -- a bug report has none.

    title exists for the bug report, whose wire shape is {title, detail,
    surface}; detail lands in comment beside a thumbs-down's words. NULL on a
    rating, because a thumb has nothing to title and inventing one from the
    first line of the comment would put a sentence nobody wrote at the top of a
    triage queue.

    improvement_item_id is where the complaint went. Many complaints become one
    backlog item -- that is the usual case, not the exception -- so the link
    lives here, on the row whose status already says "triaged", and answers what
    it was triaged into. NULL until somebody triages it.

    WHO CHANGED THE STATUS IS NOT A COLUMN HERE. It is the actor on the audit
    event the triage path appends, which is the same choice UserRole makes and
    for the same reason: one log, not two. Whoever builds that path needs an
    ACTION_ constant in app/state/audit.py, beside the others -- not a new
    string at the call site, and not a resolved_by column here that would then
    disagree with the chain.
    """

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # One of FEEDBACK_KINDS.
    kind: Mapped[str] = mapped_column(String(16))
    # One of FEEDBACK_RATINGS, or NULL on a bug report. Not a neutral value.
    rating: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # The bug report's title. NULL on a rating.
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # The comment box under a thumbs-down, or the bug report's detail. Empty is
    # ordinary: a thumbs-up with nothing to add is still worth counting.
    comment: Mapped[str] = mapped_column(Text, default="")

    # [{"role": ..., "text": ...}] for the turns before this one, frozen at the
    # moment the thumb went down. NULL where there was no chat.
    context: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The screen it came from. For a thumbs-down, copied from the message.
    surface: Mapped[str] = mapped_column(String(64), default="")

    # One of FEEDBACK_STATUSES. Defaulted, unlike ApprovalWorkflow.status,
    # because a row's state at birth is not a decision anybody took -- every
    # piece of feedback starts unread, and "new" is that fact rather than an
    # answer put in somebody's mouth.
    status: Mapped[str] = mapped_column(String(16), default=FEEDBACK_NEW)
    # What was done about it, in plain words. NULL means nobody has written
    # one, and a resolved row with no resolution is a row somebody closed
    # without saying why -- which the queue has to show rather than hide.
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)

    chat_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_messages.id"), nullable=True, index=True
    )
    improvement_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("improvement_items.id"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)


# The triage queue: one company, one status.
Index("ix_feedback_company_status", Feedback.company_id, Feedback.status)


class ImprovementItem(Base):
    """The backlog a reviewer triages feedback into.

    TWO AXES, TWO COLUMNS, AND THIS IS THE POINT OF THE TABLE. status is where
    the work got to: open, in progress, done, dropped. review_decision is what a
    person ruled: pending, approved, rejected. A machine lifecycle and a human
    decision are different facts and they move independently -- an approved item
    can sit open for a month, and an item can be in progress while the ruling on
    it is still pending, which is what happens when somebody starts work before
    the review meeting. One column holding both would force a lie at every
    combination that does not fit, and the first thing lost is why an item is
    not being done: refused, or accepted and then abandoned. IMPROVEMENT_DROPPED
    and REVIEW_REJECTED are separate words for exactly that.

    No owner column, and no raised_by. Who raised the item and who ruled on it
    are on the audit chain, the same choice Feedback makes above.

    An item is not scoped to a project. Feedback is about the product, not about
    a docket, and a backlog item that named a project would have to be copied
    for every tenant that hit the same defect.
    """

    __tablename__ = "improvement_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # One of IMPROVEMENT_CATEGORIES. guidance is the third one and it earns its
    # place: plenty of complaints are neither a defect nor a missing feature but
    # a person reading the product wrongly, and folding those into "bug" hides
    # the cheapest fixes there are.
    category: Mapped[str] = mapped_column(String(16))
    # One of IMPROVEMENT_STATUSES.
    status: Mapped[str] = mapped_column(String(16), default=IMPROVEMENT_OPEN)
    # One of REVIEW_DECISIONS. Held apart from status on purpose -- see above.
    review_decision: Mapped[str] = mapped_column(
        String(16), default=REVIEW_PENDING
    )
    # One of IMPROVEMENT_PRIORITIES.
    priority: Mapped[str] = mapped_column(String(16), default=PRIORITY_NORMAL)

    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)


# The backlog, and the review queue. Two reads, two indexes: "what is open" and
# "what is waiting on a ruling" are asked by different people.
Index(
    "ix_improvement_company_status",
    ImprovementItem.company_id,
    ImprovementItem.status,
)
Index(
    "ix_improvement_company_review",
    ImprovementItem.company_id,
    ImprovementItem.review_decision,
)


# ---------------------------------------------------------------------------
# Reaching people who are not users yet
#
# Everything above this line describes work done by people who already have an
# account in this tenant. This block is about the two moments the work leaves
# that circle, because that is what the analyst's day actually does: they work
# out which obligations a change touches, and then they have to reach whoever
# may approve acting on it -- and that person sits in Legal, or Rates, or
# Engineering, and often has no account here at all.
#
# A SHARE LINK carries one claim or one change to somebody with no login. An
# INVITATION carries one item to somebody who has to become a user to act on it.
# They are separate tables because they answer separate questions and must not
# be confused: a share link is reading, and an invitation is the first step to
# acting. The pricing boundary in PERMISSION_CODES runs between them. Nothing
# in this block grants a permission, and nothing in it may be read as granting
# one.
#
# Five rules run through the block.
#
# A SECRET IS STORED HASHED OR NOT AT ALL. Both tables hold token_hash and
# neither has anywhere to keep a token. LoginSession set the precedent and the
# argument is the same: reading the whole table must not hand anybody a working
# link. The token is shown once, to whoever created it, and is never written.
#
# THE OPEN PATH HAS NO TENANT AND TAKES ONE FROM THE ROW. Every other read in
# this codebase starts with a company_id the caller already holds. A visitor on
# /s/<token> holds nothing -- that is the point of the feature -- so the lookup
# is by token hash alone, which is why that hash is unique here rather than
# merely indexed. company_id then comes OUT of the row, and every read that
# follows is scoped by it through app/state/queries.py like any other. A share
# view that reaches past its link's company_id is a cross-tenant read with no
# session behind it, which is the worst shape this defect can take.
#
# WHAT THE RECIPIENT WAS SHOWN IS EVIDENCE, AND IT IS KEPT PER OPEN.
# ShareOpen.verified is not a cache of whether the claim is good today. It is
# the record of what the page actually did at that moment, which is how "what
# did Legal see on Tuesday" is answerable a year later. Verification re-runs at
# open time against the stored source, exactly as ADR-003 requires everywhere
# else, and a link that no longer verifies shows the refusal and the reason and
# never the statement.
#
# AN INVITE IS NOT A WAY TO GRANT ANYTHING. There is no role column on
# Invitation and there is deliberately nowhere to put one. Acceptance grants
# obligation_owner and nothing else, in the write layer, where a test can prove
# it. A column naming the role would make writing a row a way to choose one,
# and the invite would become the privilege-escalation path the design refuses.
#
# ABSENCE STAYS ABSENCE. A link that has never been opened has a NULL
# last_opened_at, not its creation time. An invitation queued for approval has
# a NULL invited_user_id, because no account exists yet -- so the item that
# prompted it stays visibly unrouted with its reason recorded, rather than
# pointing at a person who cannot be reached.
# ---------------------------------------------------------------------------


# One claim or one change. Never a project, never a proceeding, never a list:
# a link whose scope is a collection cannot state what it is showing, and the
# recipient cannot tell what was left out of it. The vocabulary is where that
# rule is written down, so a caller cannot widen the scope by typing a word.
SHARE_SUBJECT_CLAIM = "claim"
SHARE_SUBJECT_CHANGE = "change"
SHARE_SUBJECT_TYPES = (SHARE_SUBJECT_CLAIM, SHARE_SUBJECT_CHANGE)

# The token's strength and its life, named here rather than spelt at whichever
# call site happens to mint one. 32 bytes from secrets.token_urlsafe is the
# floor; the number is a minimum and not a target, and a caller asking for more
# is fine. Seven days by default and thirty at the most, because an
# unauthenticated link that outlives the question it answered is a standing
# hole nobody remembers opening.
SHARE_TOKEN_BYTES = 32
SHARE_DEFAULT_TTL_DAYS = 7
SHARE_MAX_TTL_DAYS = 30


class ShareLink(Base):
    """One claim or one change, readable by somebody with no account.

    A chosen string id, because an admin registry lists these and links to
    them. The token is not the id and the id is not a secret: an admin reading
    the registry must be able to name a link, and revoke it, without ever
    seeing what would open it.

    THE HASH IS UNIQUE, WHICH IS MORE THAN AN INDEX AND IS WHY IT IS DECLARED
    THAT WAY. The open path resolves a token to exactly one link with no tenant
    in hand, so a second row sharing a hash would be a link that opens
    somebody else's claim. LoginSession makes the same choice for the same
    reason.

    TWO COUNTS SIT HERE THAT THE ROWS BELOW ALREADY KNOW, AND THAT IS A
    DELIBERATE EXCEPTION. Project explains at length why this codebase stores
    no derived counts. open_count and last_opened_at break that rule for the
    reason ResearchThread.last_activity_at breaks it: the admin registry lists
    every link a company has and sorts by last use, and deriving both per row
    means a query per row on the one screen that exists to show many. The
    exception carries a hard condition -- share_opens is the record, this pair
    is a convenience, and anything evidential reads the rows. A reader that
    finds open_count at 0 with rows in share_opens has found a defect in the
    writer, and must believe the rows.

    REVOCATION IS A TIMESTAMP AND A PERSON, AND IT DELETES NOTHING. The row
    stays so the opens under it stay attached to something. revoked_by_user_id
    is an account and not a name, because the question asked of it is whether
    that account was entitled to revoke -- the sharer or an admin -- and a
    display string cannot answer it.

    WHAT THIS ROW DOES NOT DECIDE. Whether sharing is permitted in this tenant
    at all. That switch has no home in this schema yet; see the note at the
    foot of this file. Until it does, the write layer must ask one function and
    that function must announce that it is answering from a default.
    """

    __tablename__ = "share_links"

    # SHL-0001. A URL in the admin registry points at it.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # SHA-256 hex of the token. The token itself is shown once, to whoever
    # created the link, and is never stored anywhere. A plain hash is right
    # here for the reason it is right on LoginSession.token_hash: the input is
    # 32 random bytes this process generated, so there is no dictionary to run
    # against it and a slow KDF would only tax every open.
    token_hash: Mapped[str] = mapped_column(String(64))

    # One of SHARE_SUBJECT_TYPES, and the id of that row. Not a foreign key:
    # one column cannot point at two tables, and splitting it into claim_id and
    # change_id would let a row set both and mean nothing. The write layer
    # checks the subject exists and belongs to company_id before minting a
    # link, and the open path checks it again rather than trusting the row.
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(64))

    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # Not nullable and not defaulted. A link with no expiry is a permanent
    # unauthenticated door, and a default here would be an expiry nobody chose.
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)

    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    # A convenience over share_opens, never the evidence. See above.
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    # NULL until somebody opens it. Never the creation time: a link nobody
    # followed and a link opened an hour ago must not render alike, and the
    # registry says "not opened" rather than showing a date.
    last_opened_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, nullable=True
    )


# The open-time lookup, and the guarantee that it resolves to one row. Unique
# because a shared hash would be a link that opens the wrong tenant's claim.
Index("ix_share_links_token", ShareLink.token_hash, unique=True)
# "What has been shared of this claim, and by whom" -- the read behind the
# revoke button and behind the line on the claim page saying it went out.
Index(
    "ix_share_links_company_subject",
    ShareLink.company_id,
    ShareLink.subject_type,
    ShareLink.subject_id,
)


class ShareOpen(Base):
    """One open of one link, and what the page showed when it happened.

    An autoincrement integer, because these are only ever read in sequence --
    one link's opens, oldest first -- the same choice Passage and ResearchTurn
    make.

    NO company_id, ON PURPOSE. Tenancy lives on the link that owns the row and
    is reached by a join, the way passages_for_company reaches it through the
    version. A second copy of the company here would be a column two writers
    could let drift, and there is no read of this table that does not already
    have the link in hand. Any query over share_opens must therefore join
    share_links and filter company_id there; one that does not is an unscoped
    read of who opened what.

    verified IS THE COLUMN THIS TABLE EXISTS FOR. It records whether
    verification passed AT THIS OPEN, against the stored source, at the stored
    offsets. It is not a fact about the claim -- claims carry no such flag, and
    ADR-003 is the reason -- it is a fact about what a named recipient was
    shown at a named moment. False means the page showed the refusal and the
    reason and withheld the statement, which is the same rule every other
    surface follows and matters most here, on the one page nobody had to log
    in to see.

    IT HAS NO DEFAULT AND IT IS NOT NULLABLE, WHICH IS THE POINT. A writer that
    says nothing about verification has recorded nothing about what happened,
    and a default of either value would file that silence as a measurement. The
    insert fails instead. Absence is denial applied to the evidence itself.

    WHAT IT DOES NOT PROVE. That a particular person read the page. The link is
    a bearer secret, so this records that somebody holding it opened it from
    that address, and nothing more. Reading it as attendance would be a
    stronger claim than the mechanism supports.
    """

    __tablename__ = "share_opens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    share_id: Mapped[str] = mapped_column(ForeignKey("share_links.id"))

    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
    # 45 characters holds an IPv6 address with an embedded IPv4 one, as on
    # AuditEvent and LoginSession. Empty means the server recorded none, which
    # is a gap in the caller rather than a fact about the visitor. A proxy in
    # front of this makes it the proxy's address and nothing here can tell.
    ip: Mapped[str] = mapped_column(String(45), default="")

    verified: Mapped[bool] = mapped_column(Boolean)


# One link's opens, in the order they happened. This is the only read there is,
# so share_id needs no index of its own -- the same reasoning as
# ix_research_turns_thread_seq.
Index("ix_share_opens_share_seq", ShareOpen.share_id, ShareOpen.id)


# Where an invitation got to. Six words and each of them is a different fact.
#
#   pending             sent, same domain, nobody's approval needed. The
#                       account exists at STATUS_INVITED and the item routes.
#   awaiting_approval   another domain, so a holder of user.manage has to say
#                       yes. NO ACCOUNT EXISTS YET and the item stays unrouted.
#   approved            an admin said yes; the account now exists and the
#                       invitation is live. Kept apart from pending because
#                       "nobody had to approve this" and "somebody did" are
#                       different things to answer for afterwards.
#   accepted            the person set a password and became a user.
#   revoked             withdrawn before acceptance.
#   expired             nobody accepted in time.
#   superseded          a later invitation to the same person replaced this
#                       one, so its token stopped working the instant the new
#                       one was minted. See the resend rule below.
#
# revoked and expired are both dead and are not one word, for the reason
# IMPROVEMENT_DROPPED and REVIEW_REJECTED are not one word: "why is this person
# not here" has an answer, and it is either that somebody stopped it or that
# nobody acted.
#
# superseded is the third answer to that question and needed a third word for
# the same reason. "Somebody stopped it", "nobody acted in time" and "somebody
# sent them a new one" are three different things to answer for, and only the
# last is a normal Tuesday. Reusing revoked would have put an accusation in the
# log every time an admin pressed resend; reusing expired would have said the
# clock ran out when it did not.
INVITE_PENDING = "pending"
INVITE_AWAITING_APPROVAL = "awaiting_approval"
INVITE_APPROVED = "approved"
INVITE_ACCEPTED = "accepted"
INVITE_REVOKED = "revoked"
INVITE_EXPIRED = "expired"
INVITE_SUPERSEDED = "superseded"
INVITATION_STATUSES = (
    INVITE_PENDING,
    INVITE_AWAITING_APPROVAL,
    INVITE_APPROVED,
    INVITE_ACCEPTED,
    INVITE_REVOKED,
    INVITE_EXPIRED,
    INVITE_SUPERSEDED,
)

# What pulled the person in. Both are routing dead ends today: an escalation
# whose obligation owner has no account, and an obligation carrying a name from
# data/company_context.json and no owner_user_id. Acceptance lands the person
# on whichever one is named here, which is the whole reason the pair is stored
# rather than reconstructed.
INVITE_SUBJECT_ESCALATION = "escalation"
INVITE_SUBJECT_OBLIGATION = "obligation"
INVITE_SUBJECT_TYPES = (INVITE_SUBJECT_ESCALATION, INVITE_SUBJECT_OBLIGATION)

# WHY AN INVITATION EXISTS AT ALL. Two answers, and no third.
#
#   handoff     routing walked to a person with no account and stopped. The
#               invitation names the item that is waiting, and an admin reading
#               the queue reads the work rather than a bare address.
#   provision   an admin holding user.manage added a login. There is no item;
#               the justification is the named admin, recorded on the row.
#
# THIS PAIR IS WHAT LET subject_type AND subject_id BECOME NULLABLE WITHOUT
# LOSING THE CONTROL THEY CARRIED. The old rule was "every invitation names its
# item", enforced by two NOT NULLs, and the reason given for it was that no
# invitation may be unjustified. Admin provisioning is bare account creation, so
# the literal rule had to go; the reason behind it did not. The rule is now
# "every invitation is justified, and the kind says by what" -- by an item for a
# handoff, by a named admin who held user.manage for a provision -- and the
# check constraint on the table below is what makes the first half of that as
# hard as the NOT NULL was.
INVITE_KIND_HANDOFF = "handoff"
INVITE_KIND_PROVISION = "provision"
INVITE_KINDS = (INVITE_KIND_HANDOFF, INVITE_KIND_PROVISION)

# Mirrors the share link's policy because that is the only lifetime this
# product has actually decided on. Whoever sets the invite policy properly
# should change these two numbers here rather than at a call site.
#
# THESE ARE THE HANDOFF CLOCK AND THEY ARE DELIBERATELY NOT SHORTENED. A handoff
# invitation may sit until somebody notices the item it names, so a day would
# break routing: the escalation would fall back to the shared queue overnight
# and the person it was waiting for would never be reached.
INVITE_DEFAULT_TTL_DAYS = 7
INVITE_MAX_TTL_DAYS = 30

# THE PROVISIONING CLOCK IS A SEPARATE, SHORTER ONE, AND IT IS IN HOURS.
#
# An admin provisioning a login is a deliberate act with a named person waiting
# on the other end, usually told to expect it. A day is generous for that, and a
# credential-setting link is the most valuable thing in this product to steal:
# whoever holds it chooses the password on an account whose role has already
# been granted. The two clocks are separate numbers rather than one number with
# a caller-supplied override, because the override is how the short one quietly
# becomes the long one.
#
# NO CALL SITE TAKES THIS AS AN ARGUMENT. provision_login has no ttl parameter,
# so nothing outside this line can lengthen the life of a credential link.
INVITE_PROVISION_TTL_HOURS = 24


class Invitation(Base):
    """One person asked to join, and the reason somebody was entitled to ask.

    NO INVITATION IS UNJUSTIFIED, AND kind SAYS BY WHAT. This replaces an
    earlier rule and the earlier rule is worth stating, because the change is
    the interesting part. subject_type and subject_id used to be NOT NULL, so no
    invitation could be a bare account creation: every one named the work that
    justified it, and an admin reading the queue read the work rather than a
    bare address. Then admin provisioning arrived, which IS bare account
    creation, and the literal rule could not survive it.

    What survived is the reason behind it. A handoff invitation still names its
    item, exactly as before -- the check constraint below refuses one that does
    not, so the guarantee is as hard as the NOT NULL was. A provision invitation
    names no item and instead requires user.manage and records the admin who
    performed it in invited_by_user_id, which is NOT NULL and is therefore an
    answer somebody has to give. So the queue still never shows an address
    nobody can account for; for half the rows the account is an item and for the
    other half it is a person.

    invited_by_user_id IS THE ADMIN ON A PROVISION ROW, and there is no second
    column for that. A provisioned_by_user_id beside it would be one fact kept
    twice, free to disagree with itself, and the disagreement would be about who
    let somebody into the product.

    THERE IS NO ROLE COLUMN, AND PROVISIONING DID NOT ADD ONE. Acceptance of a
    handoff grants obligation_owner and nothing else. Putting the role on the
    row would make writing a row a way to pick one, and an invitation would
    become a privilege-escalation path with a friendly name. The narrowest reach
    that lets the person act on the item that pulled them in is a rule in the
    write layer, where a test can prove it, and it is not negotiable per
    invitation.

    A provisioned login does have a role the admin chose, and it is NOT here
    either. It is granted to the User at provision time, through the same
    grant_role path as every other grant, so the audit chain records who chose
    it at the moment they chose it. The account sits at STATUS_INVITED until it
    accepts and permissions_for_user ignores anything that is not active, so the
    grant is visible on the admin screen and confers nothing until a password
    exists. Acceptance therefore grants nothing at all on a provision row: there
    is no moment at which holding a token decides authority.

    invited_user_id IS NULL UNTIL AN ACCOUNT EXISTS, AND THE NULL IS THE
    SIGNAL. A same-domain invite writes a User at STATUS_INVITED immediately,
    so the escalation can route to a real id and the analyst sees "waiting on
    Priya Nandakumar -- invited 2 days ago, not yet accepted". A cross-domain
    invite waits for a holder of user.manage, writes no User, and the item it
    names stays visibly unrouted with the reason recorded, which is what
    app/state/routing.py already does correctly and must keep doing. The column
    also says which account THIS invitation created, which the email cannot:
    revoking an invitation may suspend the account it made, and must not touch
    an account that was already there.

    email is stored normalised -- trimmed and lower-cased -- because User.email
    is, and the two are compared. Storing one raw would mean the domain check
    that decides between the fast path and the approval queue ran against a
    different string from the one the account is keyed by.

    status HAS NO DEFAULT, following ApprovalWorkflow.status. Which path an
    invitation takes is a decision somebody made about a domain, not a state a
    row is born in, and a default of "pending" would quietly send a
    cross-domain invite down the fast path if a caller forgot to say.

    ONE LIVE TOKEN PER PERSON, AND EXPIRY IS NEVER EXTENDED. A resend writes a
    NEW row with a new token and moves the previous one to INVITE_SUPERSEDED. It
    does not move expires_at on the standing row, and the difference is the
    whole security property: a link that leaked into an inbox, a helpdesk ticket
    or a mail archive would otherwise gain another day of life every time
    somebody pressed resend, for ever. The superseded row keeps the expiry it
    was written with, because rewriting it would erase what that link's life
    actually was.

    WHAT THIS SCHEMA CANNOT ENFORCE. That a same-domain invite is really same
    domain, that the inviter held user.invite, that a provisioning admin really
    held user.manage, that at most one invitation to an address is live, and
    that a tenant with the approval switch on never wrote a pending row. All are
    write-layer rules. No constraint available here can compare an email's
    domain against a company's or read a permission grid, and stating the gap is
    better than implying it is covered. The one thing the constraint below DOES
    enforce is the half of the justification rule that is expressible in
    columns: a handoff names its item.
    """

    __tablename__ = "invitations"

    # INV-0001. The approval queue links to it. The acceptance URL carries the
    # token, never this.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(64), index=True)

    # 320 is the longest address RFC 5321 permits, as on User.email.
    email: Mapped[str] = mapped_column(String(320))

    invited_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    invited_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)

    # SHA-256 hex of the acceptance token. Never the token, for the reason
    # given on ShareLink.token_hash, and unique for the same reason: acceptance
    # arrives with nothing but the token and must resolve to one invitation.
    token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)

    # NULL until the person accepts. Never a stand-in for invited_at.
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # One of INVITATION_STATUSES. Checked on write, like every vocabulary here.
    status: Mapped[str] = mapped_column(String(16))
    # The holder of user.manage who released a cross-domain invite. NULL on
    # every same-domain one, where nobody was asked -- and NULL there means no
    # approval was required, which is exactly what the status says too.
    approved_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    # One of INVITE_KINDS. NO DEFAULT, following status directly above and for
    # the same reason: which kind an invitation is decides whether it needs an
    # item and who was allowed to write it, and a default would let a caller
    # that never thought about it pick the permissive answer by silence.
    kind: Mapped[str] = mapped_column(String(16))

    # One of INVITE_SUBJECT_TYPES, and the id of that row. Not a foreign key,
    # for the reason given on ShareLink.subject_type: one column, two tables.
    #
    # NULLABLE ONLY BECAUSE A PROVISION ROW HAS NO ITEM. On a handoff these are
    # as required as they ever were, and the constraint below is where that is
    # now written down. Making them nullable without the constraint would have
    # dropped the control rather than narrowed it.
    subject_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The account this invitation created, once one exists. See above.
    invited_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    # The justification rule, as far as columns can carry it. A handoff with no
    # item cannot be written by anything -- this module's write layer, a script,
    # a console, a future agent who never read the docstring. The literals are
    # built from the constants above so there is one spelling of each word.
    __table_args__ = (
        CheckConstraint(
            f"kind <> '{INVITE_KIND_HANDOFF}' OR ("
            "subject_type IS NOT NULL AND subject_type <> '' AND "
            "subject_id IS NOT NULL AND subject_id <> ''"
            ")",
            name="ck_invitations_handoff_names_its_item",
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{name}'" for name in INVITE_KINDS) + ")",
            name="ck_invitations_kind_is_known",
        ),
    )


# Acceptance resolves a token to one invitation, with no tenant in hand.
Index("ix_invitations_token", Invitation.token_hash, unique=True)
# "Has this person already been asked", per company. NOT unique: an invitation
# that was revoked or expired and a fresh one for the same address are two
# legitimate rows, the way a grant, a revoke and a re-grant are three on
# user_roles. Uniqueness applies only to the live one, and the write layer is
# where that is enforced.
Index("ix_invitations_company_email", Invitation.company_id, Invitation.email)
# The admin queue: one company, one status, and awaiting_approval is the read.
Index("ix_invitations_company_status", Invitation.company_id, Invitation.status)


# ---------------------------------------------------------------------------
# THE TWO TENANT SWITCHES HAVE NO HOME IN THIS SCHEMA, AND I HAVE NOT INVENTED
# ONE
#
# The design calls for two per-company switches: sharing enabled, and invites
# always need approval regardless of domain. There is nowhere to put them.
# There is no companies table -- company_id is a bare string on every row and
# no row describes a company -- and there is no settings table. The one setting
# the product already has, the confidence threshold ADR-006 requires be
# configuration, is read from an environment variable in app/state/claims.py
# and is therefore global rather than per company, which is a related gap.
#
# Adding a settings table here, alone, in the same hour three other agents are
# building against this file, would put a table in the schema that nobody else
# knows exists and that the threshold would then have to be migrated onto
# later. So this is the proposal rather than the code, and it is written down
# here so the next person finds it beside the tables that need it:
#
#     class CompanySetting(Base):
#         __tablename__ = "company_settings"
#         company_id: Mapped[str] = mapped_column(String(64), primary_key=True)
#         key: Mapped[str] = mapped_column(String(64), primary_key=True)
#         value: Mapped[str] = mapped_column(Text)
#         updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow)
#         updated_by_user_id: Mapped[str | None] = mapped_column(
#             ForeignKey("users.id"), nullable=True
#         )
#
#     SETTING_SHARING_ENABLED = "sharing.enabled"
#     SETTING_INVITES_NEED_APPROVAL = "invites.need_approval"
#
# A composite natural key rather than a surrogate id, the choice RolePermission
# makes: one value per key per company, and the database refuses a second.
#
# THE ABSENT ROW IS THE HARD PART. A missing row is not "off" and not "on" --
# it is nobody having decided -- so whatever reads these must hand back the
# value AND where it came from, and the caller must be able to show "sharing is
# on by default, nobody has set it" rather than an unqualified "on". A fallback
# that does not announce itself is the failure section 26 of
# docs/best-practices.html was written about, and a security switch that
# silently defaults to permissive is the worst instance of it.
#
# UNTIL THE TABLE EXISTS, the write layer must ask exactly one function --
# sharing_enabled(session, *, company_id) -- and that function must say it is
# answering from a default. One place to change when the table lands, rather
# than a literal True at every call site, which is what a half-migrated
# corpus looks like and why section 27 exists.
# ---------------------------------------------------------------------------
