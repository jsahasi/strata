"""Load data/ end to end. Idempotent: safe to run on every `make run`.

What this does, in order: creates the company's proceeding, ingests the three
versions through the pipeline chokepoint, persists the diff between them, writes
one claim per labelled change in the manifest, writes two claims that are
supposed to fail, then asks the product which of them may be asserted and
escalates the rest.

**The statements are built by code, not by a model.** The claim sentences here
are assembled from the manifest by the small extractors below: a date, a
percentage, a section label, a measure carried over unchanged. Each rule is
deterministic and tested. What that buys is a working demo with no API key and
no network. This module is a fixture standing in for the interpretation stage
that docs/tdd.html places between diff and state. When that stage exists,
these sentences come from it and this file loads data only.

**An earlier version of this paragraph said there was no model call anywhere in
this repository, which is why it is corrected rather than quietly replaced.**
Two modules call one now, and neither writes anything here: app/chat/agent.py
answers questions, and app/interpretation/propose.py judges whether one change
is material, on the change screen, at render time. Nothing seeded below is model
output and nothing below judges materiality -- `Change.materiality` is still
NULL on every row this file writes. The judgement is computed while the page
renders and is not stored, so the demo still runs with no key and says so when
it has none.

**The two failures are deliberate and are the point.** A demo where everything
verifies proves nothing about ADR-003. So the seed writes a claim whose quote was
altered at real offsets -- the failure a model produces fluently -- and a claim
citing a sentence that appears three times without saying which one it means.
Both are written the same way as the others and are refused at read time by the
same code that the workspace runs. Nothing marks them as the planted failures;
they fail because the source does not support them.

**Idempotency is real, not a truncate.** Running twice writes nothing the second
time: the pipeline reuses versions and changes by derived id, and every claim and
escalation is keyed by a stable id and skipped when present. An escalation is
never rewritten once created, because a person may have resolved it and the
resolution is the part worth keeping.

**The workspace is loaded from the same two files.** Projects, the changes
attached to them, threads, plans, re-runs, knowledge, sources, findings,
questions, the collective take, the deliverable and the steer directives are all
built by the code below from data/manifest.json and data/company_context.json.
Three rules keep that honest:

*Nothing is invented.* Every docket number, section label, date, share and
threshold in a seeded row is extracted from the manifest by the same rules that
build the claim sentences. Every person, obligation, document and internal
project comes from the company context. Where a row needs prose -- a thread
turn, a take, a memo -- the sentence is written here, and the facts inside it
are substituted from the corpus rather than typed.

*Machine writes and human writes are attributed differently.* Ingesting,
diffing and withholding a claim are decisions the system took, and they are
signed `system:seed` or `system:pipeline`. Opening a project, attaching a
change, issuing a steer and composing a take are decisions a person takes, and
they carry the name of the owner the company context gives for that obligation.
Those people are declared fictional in the data file. The distinction that
matters in the log is machine against human, and it is kept.

*Rows are backdated; audit entries never are.* A project created a month ago
and a thread opened last week make the demo readable, so the seed writes those
timestamps. It does not backdate an audit entry, because when the system decided
something is the one thing the chain exists to answer.

The obligations, projects and documents in data/company_context.json still have
no tables of their own -- impact mapping is designed and not built
(docs/.ai/state.json). They are read here as the source of names, owners and the
obligation-to-change mapping, and the gap stays visible rather than being
papered over with a half-table.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.interpretation.action import action_vocabulary
from app.pipeline import ingest_and_diff
from app.state import actions as action_store
from app.state.audit import record_event
from app.state.claims import (
    WithheldClaim,
    changes_for_proceeding,
    claims_for_change,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import get_engine, session_scope
from app.state.identity import (
    create_user,
    ensure_system_roles,
    grant_role,
    user_by_email,
)
from app.state.models import (
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    Base,
    Change,
    Claim,
    CollectiveTake,
    Deliverable,
    Escalation,
    Finding,
    KnowledgeItem,
    Proceeding,
    Project,
    ProjectChange,
    Question,
    ResearchThread,
    ResearchTurn,
    ScheduledRun,
    Source,
    SteerDirective,
    WorkPlan,
    WorkPlanStep,
)
from app.state.projects import (
    add_knowledge,
    add_step,
    add_turn,
    attach_change,
    create_project,
    create_work_plan,
    knowledge_item_for_company,
    open_thread,
    project_for_company,
    record_run,
    schedule_run,
    set_thread_status,
    supersede_knowledge,
)
from app.state.queries import versions_for_company
from app.state.review import (
    KIND_EXTERNAL,
    KIND_INTERNAL,
    collective_take_for_project,
    compose_take,
    coverage_for_project,
    issue_steer,
    steer_directives_for_project,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ACTOR = "system:seed"
ACTION_WITHHOLD = "withhold_claim"

# Deterministic assembly, so confidence is not a probability from anything. It
# is flat because nothing here is uncertain: the sentence was built by a rule
# and the citation either matches the stored bytes or it does not. The field
# exists for the day a model fills it, and the two planted failures carry the
# same number so that what withholds them is plainly the evidence and not a
# score (ADR-006).
SEED_CONFIDENCE_BP = 10000

CLAIM_MISQUOTE = "CLM-MISQUOTE"
CLAIM_AMBIGUOUS = "CLM-AMBIGUOUS"

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
# "March 1, 2027". A compliance date, the token an analyst most fears missing.
_DATE = re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b")
# "100%", "fifty percent (50%)", "not less than fifty percent (50%)".
_SHARE = re.compile(
    r"\b(?:not less than\s+|at least\s+)?(?:[a-z]+\s+percent\s+)?\(?\d{1,3}%\)?"
)
# "20 megawatts (MW)". A quantity with its unit spelled out and abbreviated.
_MEASURE = re.compile(r"\b\d+\s+[a-z]+\s+\([A-Z]{1,4}\)")
# The leading section number of a passage: "6", "5.4.1", "SECTION 6.".
_LABEL = re.compile(r"^\s*(?:SECTION\s+)?(\d+(?:\.\d+)*)")
# "not less than five (5) years".
_RETENTION = re.compile(r"not less than [a-z]+ \(\d+\) years")


@dataclass(frozen=True, slots=True)
class WorkspaceReport:
    """Row totals for the workspace, counted after the run rather than tallied.

    Every field is a count of what is stored, so comparing two reports is a
    complete idempotency check: a second seed that duplicated one thread turn
    would move one number here and nothing else would notice.

    take_included and take_withheld are the two counts on the current collective
    take. They are listed beside the row totals on purpose -- a take that lost
    its withheld count between runs is the failure this product exists to
    prevent, and it should be visible in the same glance.
    """

    projects: int
    attachments: int
    threads: int
    turns: int
    work_plan_steps: int
    scheduled_runs: int
    knowledge_items: int
    sources: int
    findings: int
    questions: int
    collective_takes: int
    deliverables: int
    steer_directives: int
    take_included: int
    take_withheld: int


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What is in the database after the run. Totals, not "rows I just wrote".

    Totals are what makes idempotency checkable: run the seed twice, compare two
    reports, and any duplication shows up as a number that moved.
    """

    company_id: str
    company_name: str
    proceeding_id: str
    versions: int
    changes: int
    claims: int
    escalations: int
    withheld: tuple[tuple[str, str], ...]
    workspace: WorkspaceReport


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def _read_source(path: Path) -> str:
    """Read as bytes and decode, never in text mode.

    The manifest's offsets were measured this way. Text mode translates newlines
    on some platforms, which would move every offset after the first line break
    and silently break every citation in the corpus.
    """
    return path.read_bytes().decode("utf-8")


def _plain(value: str) -> str:
    """Strip the corpus's own disclaimer suffix: "MPUC-2026-0142 -- invented"."""
    return value.split(" -- ")[0].strip()


# ---------------------------------------------------------------------------
# Building a claim sentence from a labelled change, by rule
# ---------------------------------------------------------------------------


def _first(matches: list[str], what: str, text: str) -> str:
    """The first match, or a refusal naming what was missing.

    A loader that shrugs and substitutes a blank writes a sentence with a hole
    in it and stores it as a claim. Failing here is loud, early and one line to
    read; the alternative is a plausible sentence nobody can trace.
    """
    if not matches:
        raise ValueError(
            f"expected {what} in {text[:60]!r}; the corpus and this loader disagree"
        )
    return matches[0]


def _section_ref(section_field: str) -> str:
    """Turn "5.2-5.3 (Cost Allocation ...)" into "Section 5.2-5.3"."""
    head = section_field.split(" (")[0].strip()
    if head.lower().startswith("section "):
        head = head[len("section "):].strip()
    return f"Section {head}"


def _subject(section_field: str) -> str:
    """The thing the section is about, as a noun phrase for the middle of a line.

    "7.1 (Compliance Timeline and Reporting -- Updated Load Forecasts)" gives
    "updated load forecast": the tail after the dash, lowercased, and the last
    word made singular so it reads as one obligation rather than a heading.
    """
    inner = section_field[section_field.find("(") + 1: section_field.rfind(")")]
    tail = inner.split(" -- ")[-1].strip().lower()
    words = tail.split()
    if words and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _label_of(text: str) -> str | None:
    match = _LABEL.match(text)
    return match.group(1) if match else None


def _heading_of(text: str) -> str:
    """The section's own name, from its first line, without number or full stop."""
    line = text.splitlines()[0].strip()
    label = _label_of(line)
    if label:
        line = line[line.find(label) + len(label):]
    return line.strip(" .")


def statement_for(change: dict, version_labels: dict[str, str]) -> str:
    """One plain sentence about one labelled change, chosen by rule.

    The rules run in order and the first that fires wins, so the sentence names
    the sharpest thing that moved: a date before a percentage, a percentage
    before a renumbering, and a bare "this changed" only when no rule found
    anything specific. Saying less is the correct failure here -- a sentence
    that invents a detail is exactly what this product exists to prevent.

    A limit worth stating: a "moved from X to Y" sentence carries one citation,
    and that citation is the Y side -- the text now in force. The X side is
    proved by the change's own before offsets, which the change screen renders
    beside it. The verifier checks that the quote is really there; it does not
    check that the passage supports the sentence (docs/security.html).
    """
    before = change["before"]["exact_text"]
    after = change["after"]["exact_text"]
    to_label = version_labels[change["after"]["version"]]

    before_dates, after_dates = _DATE.findall(before or ""), _DATE.findall(after)
    if before_dates and after_dates and before_dates != after_dates:
        return (
            f"The compliance date for the {_subject(change['section'])} moved "
            f"from {before_dates[0]} to {after_dates[0]}."
        )

    before_shares, after_shares = _SHARE.findall(before or ""), _SHARE.findall(after)
    if before_shares and after_shares and before_shares != after_shares:
        return (
            f"The share in {_section_ref(change['section'])} moved from "
            f"{before_shares[0]} to {after_shares[0]}."
        )

    if before is None:
        return (
            f"{_section_ref(change['section'])} appears for the first time in "
            f"the {to_label}."
        )

    before_label, after_label = _label_of(before), _label_of(after)
    if before_label and after_label and before_label != after_label:
        return (
            f"The {_heading_of(after)} section moved from Section {before_label} "
            f"to Section {after_label}."
        )

    held = [token for token in _MEASURE.findall(after) if token in before]
    if held:
        return (
            f"The wording of {_section_ref(change['section'])} changed and the "
            f"{held[0]} threshold did not."
        )

    from_label = version_labels[change["before"]["version"]]
    return (
        f"{_section_ref(change['section'])} changed between the {from_label} "
        f"and the {to_label}."
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _anchor(rows: list[Change], version_id: str, start: int, end: int) -> Change:
    """The first change this citation lands in.

    A citation can span several passages -- the cost-allocation edit covers two
    -- and the diff reports one change per passage. The claim attaches to the
    first change its citation covers, which is the earliest text a reader would
    look at. Deterministic, so a re-run attaches it to the same change.
    """
    covering = [
        row
        for row in rows
        if row.to_version_id == version_id
        and row.after_start is not None
        and row.after_start < end
        and start < row.after_end
    ]
    if not covering:
        raise ValueError(
            f"no change in {version_id} covers offsets {start}:{end}; "
            "the corpus and the manifest disagree about where a change is"
        )
    return min(covering, key=lambda row: (row.after_start, row.id))


def _add_claim(
    session: Session,
    *,
    claim_id: str,
    company_id: str,
    change: Change,
    statement: str,
    version_id: str,
    start: int,
    end: int,
    quote: str,
    occurrence: int | None = None,
) -> None:
    """Write one claim, or leave the stored one alone. Never a second copy."""
    if session.get(Claim, claim_id) is not None:
        return
    session.add(
        Claim(
            id=claim_id,
            company_id=company_id,
            change_id=change.id,
            statement=statement,
            citation_version_id=version_id,
            citation_start=start,
            citation_end=end,
            citation_quote=quote,
            cited_occurrence=occurrence,
            confidence_bp=SEED_CONFIDENCE_BP,
        )
    )
    session.flush()


def _escalate(session: Session, company_id: str, held: WithheldClaim) -> None:
    """Raise one escalation for a claim the product refused to assert.

    The reason text is the verifier's own words, unedited, and the reason code is
    the one app/state/claims.py branched on. Rewording it here would give the
    analyst a second vocabulary to learn and a second thing that can disagree
    with the code.

    Nothing is rewritten once written. A stored escalation may already carry a
    person's resolution, and reopening it on the next `make run` would erase
    somebody's work.
    """
    escalation_id = f"ESC-{held.claim_id}"
    if session.get(Escalation, escalation_id) is not None:
        return

    citation = (
        f"{held.citation_version_id}:{held.citation_start}:{held.citation_end}"
    )
    detail = f"quoted {held.citation_quote!r}"
    if held.source_excerpt:
        detail += f"; the source at {citation} reads {held.source_excerpt!r}"
    else:
        detail += f"; nothing could be read at {citation}"

    session.add(
        Escalation(
            id=escalation_id,
            company_id=company_id,
            claim_id=held.claim_id,
            reason_code=held.reason_code,
            reason_text=held.reason_text,
            detail=detail,
        )
    )
    session.flush()

    # An escalation row carries no timestamp of its own. The audit log is where
    # "when did we refuse this, and why" is answered, and a refusal is a
    # decision like any other.
    record_event(
        session,
        company_id=company_id,
        actor=ACTOR,
        action=ACTION_WITHHOLD,
        subject_type="claim",
        subject_id=held.claim_id,
        reason=f"{held.reason_code}: {held.reason_text}",
        citation=citation,
    )


# ---------------------------------------------------------------------------
# The workspace: projects, threads, plans, re-runs, knowledge, review centre
# ---------------------------------------------------------------------------


# A project id is in a URL, so it is chosen rather than generated. The three
# internal projects keep the ids company_context.json already gives them; the
# docket project takes the docket number, which is the only other name in the
# corpus guaranteed unique.
PROJECT_PREFIX = "PRJ-"

# company_context.json states a project's status in free text ("active,
# reprioritized after v3") and PROJECT_STATUSES is closed, so the narrowing is
# written here rather than guessed at read time. PRJ-2 is monitoring because
# this docket's only bearing on it is one date, which has already moved and been
# recorded: nothing on it waits for a person. The other two carry work in
# flight.
_CONTEXT_PROJECT_STATUS = {
    "PRJ-1": "active",
    "PRJ-2": "monitoring",
    "PRJ-3": "active",
}

# A finding's headline says where and what kind. What actually moved is in the
# claim behind it, and the claim only renders when its citation verifies. Two of
# the findings below rest on claims that fail, so a headline built from a claim
# sentence would put an unverifiable assertion on the page under a different
# name (ADR-003).
_FINDING_HEADLINES = {
    "material": "Material change at {ref}",
    "cosmetic": "Wording restated at {ref}",
    "deadline": "Compliance date moved at {ref}",
    "final-only": "New duty adopted at {ref}",
    "restructure": "{ref} renumbered in the final order",
}
_FINDING_HEADLINE_FALLBACK = "Change at {ref}"

# The reviewer's verdict, keyed on what the manifest says the change is. The
# cosmetic one is rejected on purpose: the corpus built it as the false positive
# a materiality step must not raise, and a review centre where nothing is ever
# rejected shows nobody reading. The restructure stays open because a
# renumbering is read slowly.
_FINDING_STATUSES = {"cosmetic": "rejected", "restructure": "open"}
_FINDING_STATUS_FALLBACK = "confirmed"

# Sources a person would stand behind. A board memo summarises the record and is
# not the record, so it is listed and not trusted -- and `trusted` still says
# nothing about whether a quote taken from it verifies.
_UNTRUSTED_DOCUMENT_TYPES = ("board_memo",)

_NO_PARENS = re.compile(r"\s*\([^()]*\)")


@dataclass(frozen=True, slots=True)
class _Loaded:
    """What the workspace loader needs from the corpus, gathered once.

    Passed around rather than re-read, so every surface below is built from the
    same rows the claims were built from. `anchors` maps a manifest change id to
    the persisted Change its citation lands in -- the same anchor the claim got,
    so a project, a plan step and a finding all point at one row.
    """

    company_id: str
    docket: str
    manifest: dict
    context: dict
    anchors: dict[str, Change]
    misquote_change: Change
    ambiguous_change: Change
    changes: tuple[Change, ...]
    now: datetime


def _absent(session: Session, model, row_id: str) -> bool:
    """True when this id is not yet stored. The one idempotency guard here.

    A primary-key lookup, deliberately unscoped. The question is whether the row
    exists at all, not whether this company may read it: an id already taken has
    to block the write rather than let a second row be created under one key.
    """
    return session.get(model, row_id) is None


def _no_parens(text: str) -> str:
    """Drop parenthetical asides. Corpus subjects carry one and cards do not."""
    return _NO_PARENS.sub("", text).strip()


def _jurisdiction(context: dict) -> str:
    """The primary jurisdiction, without the note about which body regulates it."""
    return context["company"]["jurisdictions"][0].split(" (")[0].strip()


def _obligation_owners(context: dict) -> dict[str, str]:
    return {row["id"]: row["owner_name"] for row in context["obligations"]}


def _obligation_by_id(context: dict) -> dict[str, dict]:
    return {row["id"]: row for row in context["obligations"]}


def _project_owner(context: dict, obligation_ids: list[str]) -> str:
    """Whoever holds most of a project's obligations. Ties go to the lowest id.

    A rule rather than a list, so adding an obligation to the company context
    moves the owner rather than leaving a name here that quietly disagrees with
    it.
    """
    owners = _obligation_owners(context)
    held: dict[str, int] = {}
    first: dict[str, str] = {}
    for obligation_id in sorted(obligation_ids):
        name = owners[obligation_id]
        held[name] = held.get(name, 0) + 1
        first.setdefault(name, obligation_id)
    return min(held, key=lambda name: (-held[name], first[name]))


def _docket_owner(manifest: dict, context: dict) -> str:
    """Who owns the obligation the material change lands on. The docket's owner."""
    material = next(c for c in manifest["changes"] if c["type"] == "material")
    return _obligation_owners(context)[material["maps_to_obligation_id"]]


def _labelled(manifest: dict, change_id: str) -> dict:
    return next(c for c in manifest["changes"] if c["id"] == change_id)


def _obligations_of(change: dict) -> list[str]:
    """Every obligation a labelled change bears on, primary one first."""
    ids = [change["maps_to_obligation_id"]]
    ids += change.get("also_related_obligation_ids", [])
    return ids


def _share_in(text: str) -> str:
    return _first(_SHARE.findall(text), "a share", text)


def _date_in(text: str) -> str:
    return _first(_DATE.findall(text), "a compliance date", text)


def _measure_in(text: str) -> str:
    return _first(_MEASURE.findall(text), "a measure", text)


def _document(context: dict, document_id: str) -> dict:
    return next(d for d in context["documents"] if d["id"] == document_id)


def _short_title(document: dict) -> str:
    """A document's name without its subtitle, for the middle of a sentence.

    The full title is what a Source row stores, because that is the record. A
    sentence that carries it whole reads as a catalogue entry, so prose takes
    the part before the colon and the record keeps all of it.
    """
    return document["title"].split(":")[0].strip()


def _docket_project_id(docket: str) -> str:
    return f"{PROJECT_PREFIX}{docket}"


# ---------------------------------------------------------------------------
# Projects and the changes attached to them
# ---------------------------------------------------------------------------


def _seed_projects(session: Session, loaded: _Loaded) -> str:
    """Open the docket project and the three the company context already names.

    The docket project is where the proceeding lives and every change from it
    attaches. The other three are the company's own work, and this docket only
    touches the parts of it that share an obligation -- which is the rule
    _seed_attachments applies rather than a list somebody kept.
    """
    docket_project = _docket_project_id(loaded.docket)
    subject = _no_parens(loaded.manifest["docket"]["subject"])
    jurisdiction = _jurisdiction(loaded.context)

    if project_for_company(session, loaded.company_id, docket_project) is None:
        create_project(
            session,
            loaded.company_id,
            project_id=docket_project,
            name=subject,
            docket_ref=loaded.docket,
            jurisdiction=jurisdiction,
            owner=_docket_owner(loaded.manifest, loaded.context),
            status="active",
            summary=(
                f"{loaded.manifest['docket']['commission'].split(' -- ')[0]}, "
                f"docket {loaded.docket}. Three versions ingested and diffed."
            ),
            created_at=loaded.now - timedelta(days=45),
        )

    for index, project in enumerate(loaded.context["projects"]):
        if project_for_company(session, loaded.company_id, project["id"]) is not None:
            continue
        create_project(
            session,
            loaded.company_id,
            project_id=project["id"],
            name=project["name"],
            # An internal programme tracks no docket of its own. NULL rather
            # than this docket's number, which would read as the project being
            # the proceeding.
            docket_ref=None,
            jurisdiction=jurisdiction,
            owner=_project_owner(loaded.context, project["related_obligation_ids"]),
            status=_CONTEXT_PROJECT_STATUS[project["id"]],
            summary=project["description"],
            created_at=loaded.now - timedelta(days=40 - index * 5),
        )

    return docket_project


def _seed_attachments(session: Session, loaded: _Loaded, docket_project: str) -> None:
    """Attach the corpus to the docket project, and the rest by obligation.

    Every change the diff found attaches to the docket project, because that is
    what the project is: the proceeding, tracked. An internal project gets a
    change only where the change's obligation is one of the project's own, which
    is the join company_context.json already describes -- so nothing here is a
    choice somebody made and nobody wrote down.
    """
    owner = _docket_owner(loaded.manifest, loaded.context)
    for change in sorted(loaded.changes, key=lambda row: row.id):
        attach_change(session, loaded.company_id, docket_project, change.id, owner)

    owners = _obligation_owners(loaded.context)
    for project in loaded.context["projects"]:
        held = set(project["related_obligation_ids"])
        for labelled in loaded.manifest["changes"]:
            touched = [o for o in _obligations_of(labelled) if o in held]
            if not touched:
                continue
            attach_change(
                session,
                loaded.company_id,
                project["id"],
                loaded.anchors[labelled["id"]].id,
                owners[touched[0]],
            )


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _seed_sources(session: Session, loaded: _Loaded, project_id: str) -> None:
    """The three filed versions as external sources, the company's own as internal.

    The split is what stops a synthesis resting entirely on the company writing
    about itself and looking well sourced. An external source carries the
    version id it was ingested as, which is what makes a claim on it citable at
    an offset; an internal document carries none, and so cannot.
    """
    for index, version in enumerate(loaded.manifest["versions"]):
        source_id = f"SRC-{version['id']}"
        if not _absent(session, Source, source_id):
            continue
        session.add(
            Source(
                id=source_id,
                company_id=loaded.company_id,
                project_id=project_id,
                kind=KIND_EXTERNAL,
                label=version["label"],
                locator=f"data/{version['file']}",
                version_id=version["id"],
                retrieved_at=loaded.now - timedelta(days=40 - index * 15),
                trusted=True,
            )
        )

    for document in loaded.context["documents"]:
        source_id = f"SRC-{document['id']}"
        if not _absent(session, Source, source_id):
            continue
        session.add(
            Source(
                id=source_id,
                company_id=loaded.company_id,
                project_id=project_id,
                kind=KIND_INTERNAL,
                label=document["title"],
                locator=f"data/company_context.json#{document['id']}",
                version_id=None,
                retrieved_at=loaded.now - timedelta(days=45),
                trusted=document["type"] not in _UNTRUSTED_DOCUMENT_TYPES,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _seed_findings(session: Session, loaded: _Loaded, project_id: str) -> None:
    """One finding per labelled change, plus the three that cannot be asserted.

    The last three are the point of the screen. Two name claims whose citations
    fail -- the misquote and the unnumbered occurrence -- and one was raised by
    a person from their own reading and names no claim at all. Coverage counts
    all three as withheld, and the take composed afterwards has to say so.

    Their headlines describe the trouble and never the content. A finding
    resting on a failed citation must not smuggle the assertion onto the page in
    a field the verifier does not read.
    """
    obligations = _obligation_by_id(loaded.context)

    for index, labelled in enumerate(loaded.manifest["changes"]):
        finding_id = f"FND-{labelled['id']}"
        if not _absent(session, Finding, finding_id):
            continue
        obligation = obligations[labelled["maps_to_obligation_id"]]
        template = _FINDING_HEADLINES.get(
            labelled["type"], _FINDING_HEADLINE_FALLBACK
        )
        session.add(
            Finding(
                id=finding_id,
                company_id=loaded.company_id,
                project_id=project_id,
                change_id=loaded.anchors[labelled["id"]].id,
                claim_id=f"CLM-{labelled['id']}",
                headline=template.format(ref=_section_ref(labelled["section"])),
                detail=(
                    f"Bears on {obligation['id']} ({obligation['owner_name']}): "
                    f"{obligation['internal_wording']}"
                ),
                raised_by=obligation["owner_name"],
                raised_at=loaded.now - timedelta(days=10 - index),
                status=_FINDING_STATUSES.get(
                    labelled["type"], _FINDING_STATUS_FALLBACK
                ),
            )
        )

    definition = _labelled(loaded.manifest, "CHG-2")
    if _absent(session, Finding, "FND-MISQUOTE"):
        session.add(
            Finding(
                id="FND-MISQUOTE",
                company_id=loaded.company_id,
                project_id=project_id,
                change_id=loaded.misquote_change.id,
                claim_id=CLAIM_MISQUOTE,
                headline=(
                    "Threshold restatement at "
                    f"{_section_ref(definition['section'])} cites words the "
                    "source does not carry"
                ),
                detail=(
                    f"Escalated as ESC-{CLAIM_MISQUOTE}. The offsets are inside "
                    "the document and the quote at them is not what the "
                    "document says, so nothing is asserted from this finding."
                ),
                raised_by=ACTOR,
                raised_at=loaded.now - timedelta(days=4),
                status="open",
            )
        )

    boilerplate = loaded.manifest["repeated_boilerplate"]
    if _absent(session, Finding, "FND-AMBIGUOUS"):
        repeats = sum(1 for o in boilerplate["occurrences"] if o["version"] == "v3")
        session.add(
            Finding(
                id="FND-AMBIGUOUS",
                company_id=loaded.company_id,
                project_id=project_id,
                change_id=loaded.ambiguous_change.id,
                claim_id=CLAIM_AMBIGUOUS,
                headline="Recordkeeping citation does not say which occurrence",
                detail=(
                    f"Escalated as ESC-{CLAIM_AMBIGUOUS}. The same sentence "
                    f"appears {repeats} times in the final order, in three "
                    "sections about three subjects. The citation names no "
                    "occurrence, so it could mean any of them."
                ),
                raised_by=ACTOR,
                raised_at=loaded.now - timedelta(days=3),
                status="open",
            )
        )

    retention = obligations["OBL-004"]
    if _absent(session, Finding, "FND-OBL-004"):
        session.add(
            Finding(
                id="FND-OBL-004",
                company_id=loaded.company_id,
                project_id=project_id,
                change_id=None,
                claim_id=None,
                headline="Internal retention period may not match the docket",
                detail=(
                    f"{retention['internal_wording']} Raised from a reading, "
                    "with no claim attached, so it counts against coverage "
                    "rather than for it."
                ),
                raised_by=retention["owner_name"],
                raised_at=loaded.now - timedelta(days=2),
                status="open",
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def _seed_questions(session: Session, loaded: _Loaded, project_id: str) -> None:
    """Three questions: one blocking and open, one answered, one merely open."""
    owners = _obligation_owners(loaded.context)
    allocation = _labelled(loaded.manifest, "CHG-1")
    definition = _labelled(loaded.manifest, "CHG-2")
    curtailment = _labelled(loaded.manifest, "CHG-4")

    rows = [
        (
            "QST-COST-ALLOCATION",
            (
                "Does the reliability-benefit finding in "
                f"{_section_ref(allocation['section'])} reach upgrades already "
                "under construction?"
            ),
            owners[allocation["maps_to_obligation_id"]],
            10,
            True,
            None,
            None,
            None,
        ),
        (
            "QST-THRESHOLD",
            (
                "Does the restated definition move the "
                f"{_measure_in(definition['after']['exact_text'])} screening "
                "cutoff we use at intake?"
            ),
            _docket_owner(loaded.manifest, loaded.context),
            9,
            False,
            "No. The threshold is the same in the revised rule and in the "
            "final order. Only the wording around it moved.",
            owners[definition["maps_to_obligation_id"]],
            8,
        ),
        (
            "QST-CURTAILMENT",
            (
                "Which tariff carries the curtailment compensation rate "
                f"{_section_ref(curtailment['section'])} points at?"
            ),
            owners[curtailment["maps_to_obligation_id"]],
            3,
            False,
            None,
            None,
            None,
        ),
    ]

    for row_id, body, asked_by, asked_days, blocking, answer, by, days in rows:
        if not _absent(session, Question, row_id):
            continue
        session.add(
            Question(
                id=row_id,
                company_id=loaded.company_id,
                project_id=project_id,
                body=body,
                asked_by=asked_by,
                asked_at=loaded.now - timedelta(days=asked_days),
                answered_at=None if days is None else loaded.now - timedelta(days=days),
                answer=answer,
                answered_by=by,
                blocking=blocking,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Research threads
# ---------------------------------------------------------------------------


def _seed_threads(session: Session, loaded: _Loaded, project_id: str) -> None:
    """Three threads: one answered on a verified claim, one open, one parked.

    The answered thread is the one worth reading. An analyst asks, the system
    replies citing a claim whose citation verifies, and the analyst turns the
    answer into work. The turn carries the claim id rather than the sentence, so
    what renders beside it is re-verified at read time and not copied here.
    """
    owners = _obligation_owners(loaded.context)
    allocation = _labelled(loaded.manifest, "CHG-1")
    curtailment = _labelled(loaded.manifest, "CHG-4")
    tariff = _document(loaded.context, "DOC-2")

    if _absent(session, ResearchThread, "THR-COST-ALLOCATION"):
        asker = owners[allocation["maps_to_obligation_id"]]
        open_thread(
            session,
            loaded.company_id,
            project_id,
            thread_id="THR-COST-ALLOCATION",
            question="Who bears the network upgrade cost under the revised rule?",
            opened_by=asker,
            opened_at=loaded.now - timedelta(days=6),
        )
        add_turn(
            session,
            loaded.company_id,
            "THR-COST-ALLOCATION",
            author_kind="analyst",
            body=(
                f"{_short_title(tariff)} still says the customer pays all of "
                "it. Find what the revised rule now says."
            ),
            created_at=loaded.now - timedelta(days=6),
        )
        add_turn(
            session,
            loaded.company_id,
            "THR-COST-ALLOCATION",
            author_kind="system",
            # The turn carries a claim id and no sentence copied from it. What
            # renders beside this line is re-verified on the read that shows it.
            body=(
                f"{_section_ref(allocation['section'])} sets a floor on the "
                "customer share and sends the rest to general rates, subject to "
                "a reliability finding."
            ),
            claim_id=f"CLM-{allocation['id']}",
            created_at=loaded.now - timedelta(days=5),
        )
        add_turn(
            session,
            loaded.company_id,
            "THR-COST-ALLOCATION",
            author_kind="analyst",
            body=(
                f"Then {_short_title(tariff)} is wrong from the effective date. "
                "Raised as a finding and put on the plan."
            ),
            created_at=loaded.now - timedelta(days=4),
        )
        set_thread_status(session, loaded.company_id, "THR-COST-ALLOCATION", "answered")

    if _absent(session, ResearchThread, "THR-CURTAILMENT-RATE"):
        asker = owners[curtailment["maps_to_obligation_id"]]
        open_thread(
            session,
            loaded.company_id,
            project_id,
            thread_id="THR-CURTAILMENT-RATE",
            question="What do we pay a customer we curtail?",
            opened_by=asker,
            opened_at=loaded.now - timedelta(days=2),
        )
        add_turn(
            session,
            loaded.company_id,
            "THR-CURTAILMENT-RATE",
            author_kind="analyst",
            body=(
                f"{_section_ref(curtailment['section'])} points at a "
                "Commission-approved curtailment tariff. I cannot find one on "
                "file for us."
            ),
            created_at=loaded.now - timedelta(days=2),
        )

    if _absent(session, ResearchThread, "THR-RETENTION"):
        retention = _obligation_by_id(loaded.context)["OBL-004"]
        open_thread(
            session,
            loaded.company_id,
            project_id,
            thread_id="THR-RETENTION",
            question="Does our study-file retention rule still cover the docket's?",
            opened_by=retention["owner_name"],
            opened_at=loaded.now - timedelta(days=7),
        )
        add_turn(
            session,
            loaded.company_id,
            "THR-RETENTION",
            author_kind="analyst",
            body=(
                "Parked until the recordkeeping citation says which occurrence "
                "it means. Answering on the wrong one is worse than waiting."
            ),
            created_at=loaded.now - timedelta(days=7),
        )
        set_thread_status(session, loaded.company_id, "THR-RETENTION", "parked")


# ---------------------------------------------------------------------------
# The work plan
# ---------------------------------------------------------------------------


def _seed_plan(session: Session, loaded: _Loaded, project_id: str) -> None:
    """One plan, five steps, four states, one of them blocked by a failed citation.

    The blocked step is the one to look at. It is blocked because the claim
    behind its change did not verify, which is a state the product can report
    and a person can act on -- not a fifth colour meaning "we are not sure".
    """
    plan_id = f"PLAN-{loaded.docket}"
    if not _absent(session, WorkPlan, plan_id):
        return

    owners = _obligation_owners(loaded.context)
    allocation = _labelled(loaded.manifest, "CHG-1")
    definition = _labelled(loaded.manifest, "CHG-2")
    deadline = _labelled(loaded.manifest, "CHG-3")
    curtailment = _labelled(loaded.manifest, "CHG-4")
    tariff = _document(loaded.context, "DOC-2")
    procedure = _document(loaded.context, "DOC-3")

    create_work_plan(
        session,
        loaded.company_id,
        project_id,
        plan_id=plan_id,
        title=f"Bring MEP into line with the final order in {loaded.docket}",
        created_at=loaded.now - timedelta(days=20),
        due_at=loaded.now + timedelta(days=40),
    )

    steps = [
        (
            "STEP-CHG-3",
            "Move the updated load forecast in the compliance calendar to "
            f"{_date_in(deadline['after']['exact_text'])}",
            owners[deadline["maps_to_obligation_id"]],
            "done",
            loaded.anchors[deadline["id"]].id,
            None,
        ),
        (
            "STEP-CHG-1",
            f"Refile {_short_title(tariff)} against the cost-causation method",
            owners[allocation["maps_to_obligation_id"]],
            "doing",
            loaded.anchors[allocation["id"]].id,
            30,
        ),
        (
            "STEP-CHG-2",
            f"Restate the intake screening cutoff in {_short_title(procedure)}",
            owners[definition["maps_to_obligation_id"]],
            "blocked",
            loaded.anchors[definition["id"]].id,
            21,
        ),
        (
            "STEP-CHG-4",
            "Write the curtailment notice and compensation process",
            owners[curtailment["maps_to_obligation_id"]],
            "todo",
            loaded.anchors[curtailment["id"]].id,
            60,
        ),
        (
            "STEP-BOARD",
            "Brief the board on the reliability findings in the final order",
            _docket_owner(loaded.manifest, loaded.context),
            "todo",
            # No change behind it. Real work with no diff to point at, which is
            # why WorkPlanStep.change_id is nullable.
            None,
            14,
        ),
    ]

    for ordinal, (step_id, description, owner, state, change, due) in enumerate(
        steps, start=1
    ):
        add_step(
            session,
            loaded.company_id,
            plan_id,
            step_id=step_id,
            description=description,
            owner=owner,
            ordinal=ordinal,
            state=state,
            change_id=change,
            due_at=None if due is None else loaded.now + timedelta(days=due),
        )


# ---------------------------------------------------------------------------
# Scheduled re-runs
# ---------------------------------------------------------------------------


def _seed_runs(session: Session, loaded: _Loaded, docket_project: str) -> None:
    """Four schedule states, because they must not render the same.

    A daily run that fired and found nothing, an on-filing run that has never
    fired, a weekly run somebody switched off, and a project with no schedule at
    all. The first and the third look identical on a screen that shows only the
    cadence, and the difference between them is whether anybody is watching.
    """
    if _absent(session, ScheduledRun, "RUN-DAILY"):
        schedule_run(
            session,
            loaded.company_id,
            docket_project,
            run_id="RUN-DAILY",
            cadence="daily",
            next_run_at=loaded.now + timedelta(hours=18),
            enabled=True,
        )
        record_run(
            session,
            loaded.company_id,
            "RUN-DAILY",
            ran_at=loaded.now - timedelta(hours=6),
            # Written even though it found nothing. A schedule that reports only
            # on the days it found something looks exactly like one that stopped.
            result=f"No new version of {loaded.docket} on the commission's docket.",
            next_run_at=loaded.now + timedelta(hours=18),
        )

    if _absent(session, ScheduledRun, "RUN-ON-FILING"):
        schedule_run(
            session,
            loaded.company_id,
            "PRJ-1",
            run_id="RUN-ON-FILING",
            cadence="on-filing",
            next_run_at=loaded.now + timedelta(days=30),
            enabled=True,
        )

    if _absent(session, ScheduledRun, "RUN-WEEKLY"):
        schedule_run(
            session,
            loaded.company_id,
            "PRJ-2",
            run_id="RUN-WEEKLY",
            cadence="weekly",
            next_run_at=loaded.now + timedelta(days=7),
            enabled=False,
        )


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


def _seed_knowledge(session: Session, loaded: _Loaded, project_id: str) -> None:
    """Four items, one of them superseded, so the store visibly has history.

    The superseded one is the flagship case in the corpus: MEP's own rule was
    correct against the first version of the docket and wrong against the
    second, without anybody editing it. Superseding writes a new row and leaves
    the old body untouched, so what the company believed in the spring is still
    readable in the autumn.
    """
    obligations = _obligation_by_id(loaded.context)
    allocation = _labelled(loaded.manifest, "CHG-1")
    definition = _labelled(loaded.manifest, "CHG-2")
    curtailment = _labelled(loaded.manifest, "CHG-4")

    collateral = obligations["OBL-001"]
    if _absent(session, KnowledgeItem, "KN-MAPPING-COLLATERAL"):
        add_knowledge(
            session,
            loaded.company_id,
            item_id="KN-MAPPING-COLLATERAL",
            project_id=project_id,
            kind="mapping",
            body=collateral["note_for_semantic_join"],
            confirmed_by=collateral["owner_name"],
            created_at=loaded.now - timedelta(days=25),
        )

    screening = obligations["OBL-003"]
    if _absent(session, KnowledgeItem, "KN-DEFINITION-THRESHOLD"):
        add_knowledge(
            session,
            loaded.company_id,
            item_id="KN-DEFINITION-THRESHOLD",
            project_id=project_id,
            kind="definition",
            body=(
                "A Large Load Customer is a customer whose requested load "
                f"reaches {_measure_in(definition['after']['exact_text'])}. The "
                "wording moved between the notice and the revised rule; the "
                "number did not."
            ),
            source_claim_id=f"CLM-{definition['id']}",
            confirmed_by=screening["owner_name"],
            created_at=loaded.now - timedelta(days=20),
        )

    if _absent(session, KnowledgeItem, "KN-PRECEDENT-FINAL-ONLY"):
        add_knowledge(
            session,
            loaded.company_id,
            item_id="KN-PRECEDENT-FINAL-ONLY",
            # Company-wide. How this commission behaves is true everywhere, and
            # copying it per project would give one fact several places to be
            # wrong.
            project_id=None,
            kind="precedent",
            body=(
                "This commission adopts duties in a final order that no draft "
                f"proposed. {_section_ref(curtailment['section'])} is one. Read "
                "every final order against both drafts, not against the last."
            ),
            source_claim_id=f"CLM-{curtailment['id']}",
            confirmed_by=_docket_owner(loaded.manifest, loaded.context),
            created_at=loaded.now - timedelta(days=15),
        )

    cost = obligations["OBL-005"]
    if _absent(session, KnowledgeItem, "KN-LESSON-COST-ALLOCATION"):
        add_knowledge(
            session,
            loaded.company_id,
            item_id="KN-LESSON-COST-ALLOCATION",
            project_id=project_id,
            kind="lesson",
            body=cost["internal_wording"],
            confirmed_by=cost["owner_name"],
            created_at=loaded.now - timedelta(days=35),
        )

    stored = knowledge_item_for_company(
        session, loaded.company_id, "KN-LESSON-COST-ALLOCATION"
    )
    if stored is not None and stored.superseded_by is None:
        share = _share_in(allocation["after"]["exact_text"])
        supersede_knowledge(
            session,
            loaded.company_id,
            "KN-LESSON-COST-ALLOCATION",
            (
                "Network upgrade costs are shared, not charged wholly to the "
                f"customer. The customer bears {share} and the Utility recovers "
                "the rest from general ratepayers where the Commission finds a "
                "reliability benefit."
            ),
            cost["owner_name"],
        )


# ---------------------------------------------------------------------------
# Steer
# ---------------------------------------------------------------------------


def _seed_steer(session: Session, loaded: _Loaded, project_id: str) -> None:
    """Two directives: one applied with a recorded effect, one still waiting.

    Both are rows with an issuer and a time rather than a setting, so the
    findings each produced can be reconciled with the instruction behind them.
    The second has no effect recorded, and a blank effect means nobody has
    written one down -- never that the directive did nothing.
    """
    existing = steer_directives_for_project(
        session, loaded.company_id, project_id, include_revoked=True
    )
    if existing:
        return

    owners = _obligation_owners(loaded.context)
    allocation = _labelled(loaded.manifest, "CHG-1")
    curtailment = _labelled(loaded.manifest, "CHG-4")
    tariff = _document(loaded.context, "DOC-2")

    applied = issue_steer(
        session,
        loaded.company_id,
        project_id,
        (
            "Check every cost allocation change in this docket against "
            f"{_short_title(tariff)}."
        ),
        owners[allocation["maps_to_obligation_id"]],
    )
    applied.applied_at = applied.issued_at
    applied.effect = (
        f"Raised the {_section_ref(allocation['section'])} finding against the "
        "filed tariff and put a refiling step on the plan."
    )

    issue_steer(
        session,
        loaded.company_id,
        project_id,
        (
            "Watch for the Commission-approved curtailment tariff that "
            f"{_section_ref(curtailment['section'])} points at."
        ),
        owners[curtailment["maps_to_obligation_id"]],
    )
    session.flush()


# ---------------------------------------------------------------------------
# The synthesis: one take, one deliverable, both carrying their coverage
# ---------------------------------------------------------------------------


def _seed_synthesis(session: Session, loaded: _Loaded, project_id: str) -> None:
    """Compose the take and draft the memo, both against the same coverage.

    This is the demo. The corpus contains two claims that fail verification and
    one finding nobody has cited, so coverage reports three withheld out of
    eight -- and compose_take() refuses to write a take that would round that to
    zero. The deliverable carries the same two numbers, taken from the same
    coverage, because the memo is the artefact that leaves the building and is
    the last place a silent omission can still be caught.

    The body states only what a verified claim supports. Nothing withheld is
    described here, in any words.
    """
    allocation = _labelled(loaded.manifest, "CHG-1")
    deadline = _labelled(loaded.manifest, "CHG-3")
    curtailment = _labelled(loaded.manifest, "CHG-4")
    tariff = _document(loaded.context, "DOC-2")
    composer = _docket_owner(loaded.manifest, loaded.context)

    share = _share_in(allocation["after"]["exact_text"])
    due = _date_in(deadline["after"]["exact_text"])
    new_section = _section_ref(curtailment["section"])

    if collective_take_for_project(session, loaded.company_id, project_id) is None:
        compose_take(
            session,
            loaded.company_id,
            project_id,
            (
                "The revised rule ends full customer recovery of network "
                f"upgrade costs. The customer bears {share} and the Utility "
                "recovers the rest from general ratepayers where the Commission "
                "finds a reliability benefit. The updated load forecast is due "
                f"{due}. The final order adds {new_section}, a curtailment duty "
                "with no draft-stage counterpart, and a payment duty running "
                f"back to the customer. {_short_title(tariff)} states the old "
                "allocation and is wrong from the effective date. What this "
                "take could not verify is counted beside it, not folded into it."
            ),
            composer,
        )

    deliverable_id = f"DLV-{loaded.docket}"
    if _absent(session, Deliverable, deliverable_id):
        coverage = coverage_for_project(session, loaded.company_id, project_id)
        session.add(
            Deliverable(
                id=deliverable_id,
                company_id=loaded.company_id,
                project_id=project_id,
                title=f"Compliance memo: {loaded.docket}",
                kind="memo",
                state="in_review",
                body=(
                    f"For review. The final order in {loaded.docket} changes "
                    "what MEP may recover and what it must do.\n\n"
                    f"1. Network upgrade costs are shared. The customer bears "
                    f"{share}; the rest is recoverable from general ratepayers "
                    "where the Commission finds a reliability benefit.\n"
                    f"2. The updated load forecast is due {due}.\n"
                    f"3. {new_section} adds a curtailment duty and a payment "
                    "duty. Neither draft proposed it.\n\n"
                    f"{_short_title(tariff)} states the old allocation and has "
                    "to be refiled."
                ),
                created_at=loaded.now - timedelta(days=1),
                # Nobody has signed it. Empty is not approved, and the interface
                # has to show which -- an unapproved draft that looks final is
                # how a draft gets filed.
                approved_by=None,
                findings_included=coverage.findings_verified,
                findings_withheld=coverage.findings_withheld,
            )
        )
        session.flush()


# ---------------------------------------------------------------------------
# Counting what is there
# ---------------------------------------------------------------------------


def _count(session: Session, model, company_id: str) -> int:
    return session.query(model).filter(model.company_id == company_id).count()


def _count_workspace(
    session: Session, company_id: str, project_id: str
) -> WorkspaceReport:
    take = collective_take_for_project(session, company_id, project_id)
    return WorkspaceReport(
        projects=_count(session, Project, company_id),
        attachments=_count(session, ProjectChange, company_id),
        threads=_count(session, ResearchThread, company_id),
        turns=_count(session, ResearchTurn, company_id),
        work_plan_steps=_count(session, WorkPlanStep, company_id),
        scheduled_runs=_count(session, ScheduledRun, company_id),
        # Includes superseded rows on purpose. The replacement is a row like any
        # other, and a second seed that wrote a second replacement would show up
        # here rather than hide behind a filter.
        knowledge_items=_count(session, KnowledgeItem, company_id),
        sources=_count(session, Source, company_id),
        findings=_count(session, Finding, company_id),
        questions=_count(session, Question, company_id),
        # Counted as rows, not as "the current one". A second compose would
        # supersede the first and leave take_included unchanged, so only the
        # row count would show it.
        collective_takes=_count(session, CollectiveTake, company_id),
        deliverables=_count(session, Deliverable, company_id),
        steer_directives=_count(session, SteerDirective, company_id),
        take_included=0 if take is None else take.findings_included,
        take_withheld=0 if take is None else take.findings_withheld,
    )


def _load_workspace(session: Session, loaded: _Loaded) -> WorkspaceReport:
    """Build every workspace surface, in the order they depend on each other.

    Sources, findings and questions come before the take, because the take
    counts them. Everything else could run in any order and does not, so that
    the audit chain a reviewer reads follows the same sequence every time.
    """
    docket_project = _seed_projects(session, loaded)
    _seed_attachments(session, loaded, docket_project)
    _seed_sources(session, loaded, docket_project)
    _seed_findings(session, loaded, docket_project)
    _seed_questions(session, loaded, docket_project)
    _seed_threads(session, loaded, docket_project)
    _seed_plan(session, loaded, docket_project)
    _seed_runs(session, loaded, docket_project)
    _seed_knowledge(session, loaded, docket_project)
    _seed_steer(session, loaded, docket_project)
    _seed_synthesis(session, loaded, docket_project)
    return _count_workspace(session, loaded.company_id, docket_project)


# ---------------------------------------------------------------------------
# Demo accounts
#
# There is a login now (app/auth/sessions.py), so a database with no users is a
# product a reviewer cannot get into. These accounts are what `make run` puts
# there, and every one of them is derived from data/company_context.json rather
# than invented -- the same rule the rest of this file follows.
#
# THEY SHARE A PUBLISHED PASSWORD, AND THAT IS A DOWNGRADE. It is written below
# in plain text, printed by the seed, and shown on the login page. That is
# correct for a demo of a synthetic corpus and wrong for anything else, which is
# why the login page says so on the page rather than in a comment nobody reads.
# A deployment that is not a demo seeds its own accounts and sets
# STRATA_DEMO_ACCOUNTS=0.
#
# ONE ROLE EACH, EVEN WHERE THE CORPUS GIVES A PERSON TWO. Denise Okoro owns
# OBL-003 and her title says Analyst; Sarah Lindqvist owns OBL-005 and runs
# regulatory affairs. Left alone, both would hold the analyst role and the
# obligation owner role, and a person holding both is exactly what segregation
# of duties forbids -- the demo would ship with its own load-bearing control
# switched off. So the rule is one role per person, title first, and it is
# stated here rather than discovered by a reviewer reading the grants.
# ---------------------------------------------------------------------------

# Twelve characters is the floor app/state/identity.py enforces. This is over
# it and is not a password anybody should reuse anywhere.
DEMO_PASSWORD = "strata-demo-2026"

# .example is reserved by RFC 2606 and resolves nowhere, so no seeded address
# can send or receive real mail by accident.
DEMO_EMAIL_DOMAIN = "example"


@dataclass(frozen=True, slots=True)
class DemoAccount:
    """One seeded account: a person from the corpus, and the role they hold."""

    email: str
    display_name: str
    title: str
    role: str


def _account_email(display_name: str, company_id: str) -> str:
    """first.last@<tenant>.example, lower-cased, from the name in the corpus."""
    parts = [
        "".join(character for character in word.lower() if character.isalnum())
        for word in display_name.split()
    ]
    local = ".".join(part for part in parts if part)
    return f"{local}@{company_id.lower()}.{DEMO_EMAIL_DOMAIN}"


def _role_for_title(title: str) -> str:
    """Title to role. Analyst first, then the regulatory lead, then owners.

    Deterministic and readable off the data file: a title saying Analyst is the
    ADR-001 persona; the VP of regulatory affairs is who would manage accounts
    in a real team, so admin lands there rather than on an invented service
    account; everyone else named on an obligation owns one.
    """
    lowered = title.lower()
    if "analyst" in lowered:
        return ROLE_ANALYST
    if lowered.startswith("vp"):
        return ROLE_ADMIN
    return ROLE_OBLIGATION_OWNER


def demo_accounts(context: dict, company_id: str) -> tuple[DemoAccount, ...]:
    """The accounts the corpus implies, in the order the corpus names them.

    Obligation owners first, then document owners, each person once. Pure: it
    reads the context and writes nothing, so the login page can call it to show
    what to type without touching the database.
    """
    seen: dict[str, DemoAccount] = {}
    people = [
        (row["owner_name"], row["owner_title"])
        for row in context.get("obligations", [])
    ] + [
        (row["owner_name"], row["owner_title"]) for row in context.get("documents", [])
    ]
    for name, title in people:
        email = _account_email(name, company_id)
        if email in seen:
            continue
        seen[email] = DemoAccount(
            email=email,
            display_name=name,
            title=title,
            role=_role_for_title(title),
        )
    return tuple(seen.values())


def demo_account_list(*, data_dir: Path = DATA_DIR) -> tuple[DemoAccount, ...]:
    """The accounts the corpus implies, read from the data file. Writes nothing.

    The login page calls this to know which addresses it may offer, and then
    checks each one against the database before showing it. Reading the file
    and reporting what it says would produce a panel of addresses that do not
    work on a database nobody seeded.
    """
    context = _read_json(data_dir / "company_context.json")
    return demo_accounts(context, context["company"]["short_name"])


def ensure_accounts(
    session: Session, *, data_dir: Path = DATA_DIR
) -> tuple[DemoAccount, ...]:
    """Create the demo accounts and their roles. Idempotent, like everything here.

    Also the only caller of ensure_system_roles() on the application path: the
    permission grid has to exist before a grant can name a role, and until this
    ran the three system roles were a table nothing wrote.

    Deliberately NOT called from load(). load() is the corpus, and it runs in
    every screen test's fixture; eight scrypt hashes per fixture is twelve
    seconds on the suite for accounts those tests do not use. `python -m
    app.seed` calls both, which is what `make run` runs, so the product a
    reviewer starts has its people. A test that needs them calls this.
    """
    context = _read_json(data_dir / "company_context.json")
    company_id = context["company"]["short_name"]

    ensure_system_roles(session)
    accounts = demo_accounts(context, company_id)
    for account in accounts:
        if user_by_email(session, company_id, account.email) is None:
            create_user(
                session,
                company_id,
                email=account.email,
                display_name=account.display_name,
                password=DEMO_PASSWORD,
                actor=ACTOR,
            )
        user = user_by_email(session, company_id, account.email)
        grant_role(
            session,
            company_id,
            user_id=user.id,
            role_name=account.role,
            actor=ACTOR,
        )
    session.flush()
    return accounts


# ---------------------------------------------------------------------------
# The seeded demonstration of the approval control
#
# WHY THIS EXISTS. app/auth/policy.py::can_approve refuses an approval from
# anybody the audit chain shows already working on the claim. Production runs
# SEGREGATED -- STRATA_APPROVAL_MODE is unset and unset means the safe value --
# so if every proposal in the seeded workspace were written by nobody, or by the
# person who also holds action.approve, the screen would be a dead end on the
# live demonstration: gate 4 would correctly refuse and a reviewer would see a
# control that looks broken rather than one that works.
#
# So the seeded proposals are written by the ANALYST, who holds action.propose
# and does not hold action.approve. Any of the five obligation owners can then
# approve them, because no audit row names them against those claims. That is
# the happy path a reviewer meets first, and it is the honest one: two people,
# two roles, one decision each.
#
# THE REFUSAL IS NOT SEEDED, AND THAT IS DELIBERATE. Reaching gate 4 needs one
# person holding action.approve who has also touched the claim, and the seeded
# grid gives nobody both -- the analyst proposes and cannot approve, the
# obligation owner approves and cannot propose. Manufacturing such a person here
# would mean shipping the demo with its own load-bearing control switched off,
# which is exactly what the "one role each" rule above refuses. The arrangement
# is one grant away and a reviewer can make it in the product: sign in as the
# admin, open /users, give the analyst the obligation owner role as well, then
# sign in as the analyst and try to approve their own proposal. Strata refuses
# and cites the audit row. tests/test_actions_screen.py drives exactly that.
# ---------------------------------------------------------------------------


def _demonstration_claims(session: Session, company_id: str) -> list[tuple[str, str]]:
    """One verified claim per version status, as (claim id, action).

    Derived from the corpus rather than named, so a change to the data file
    moves the demonstration instead of leaving it pointing at a row that is no
    longer there. Deterministic: proceedings, then changes, then claims, all in
    id order, first match per status wins.

    Only claims whose citation verifies are offered. Proposing that the company
    comply with a statement the product refuses to assert would be the product
    asserting it through the back door.
    """
    picked: dict[str, tuple[str, str]] = {}
    for proceeding in proceedings_for_company(session, company_id):
        for change in changes_for_proceeding(session, company_id, proceeding.id):
            if change.status in picked:
                continue
            verified, _held = verified_claims(session, company_id, change.id)
            if not verified:
                continue
            # The first word the status allows: monitor for a draft, comply for
            # a final order. app/interpretation/action.py owns the vocabulary
            # and this reads it rather than restating either word.
            picked[change.status] = (
                verified[0].claim_id,
                action_vocabulary(change.status)[0],
            )
    return [picked[status] for status in sorted(picked)]


#: What the seeded proposals say. Keyed by the action, because the sentence that
#: argues monitoring a draft is not the sentence that argues complying with an
#: order, and a single generic reason would make the screen's central column
#: worthless.
_RATIONALE = {
    "monitor": (
        "Draft language only. Watch this through to the final order before "
        "anybody changes a procedure on the strength of it."
    ),
    "comment": (
        "Draft language, and the threshold is the part that costs us. File a "
        "comment on it inside the window."
    ),
    "comply": (
        "Final order, so this binds. Work the obligation it lands on and get "
        "the filing calendar moved to match."
    ),
}


def ensure_demo_actions(
    session: Session, *, data_dir: Path = DATA_DIR
) -> tuple[str, ...]:
    """Propose the demonstration actions, as the analyst. Idempotent.

    Returns the proposal ids, existing or newly written, so a second run
    returns the same tuple and writes nothing. Idempotency is per claim rather
    than per id: a proposal already standing against a claim is the reason not
    to write a second one.

    Writes nothing at all when the accounts or the corpus are absent. Most test
    fixtures load one without the other, and a loader that raised there would
    turn a state the product handles into a failure in unrelated files.

    NOT called from ensure_accounts(), and the reason is a test rather than
    taste: the permissions register asserts that a freshly seeded workspace
    holds no segregation conflict, and several fixtures build accounts with no
    corpus behind them. main() calls both, which is what `make run` runs, so the
    product a reviewer starts has its proposals.
    """
    context = _read_json(data_dir / "company_context.json")
    company_id = context["company"]["short_name"]

    analyst = next(
        (
            account
            for account in demo_accounts(context, company_id)
            if account.role == ROLE_ANALYST
        ),
        None,
    )
    if analyst is None:
        return ()
    author = user_by_email(session, company_id, analyst.email)
    if author is None:
        return ()

    standing = {
        row.claim_id: row.id
        for row in action_store.proposed_actions_for_company(session, company_id)
    }

    written: list[str] = []
    for claim_id, kind in _demonstration_claims(session, company_id):
        if claim_id in standing:
            written.append(standing[claim_id])
            continue
        row = action_store.propose_action(
            session,
            company_id,
            claim_id=claim_id,
            kind=kind,
            rationale=_RATIONALE[kind],
            # The same shape every screen writes: app/web/views/actions.py
            # records "person:<email>", so a seeded proposal and one a person
            # made read the same way in the chain.
            actor=f"person:{analyst.email}",
            actor_user_id=author.id,
        )
        written.append(row.id)
    session.flush()
    return tuple(written)


def ensure_tables(engine=None) -> None:
    """Create any missing tables. Never drops.

    Deliberately not app.state.db.init_db(), which drops first. A loader that
    quietly emptied the database on every start would make idempotency
    unfalsifiable -- everything would look stable because nothing survived.
    """
    Base.metadata.create_all(engine or get_engine())


def load(session: Session, *, data_dir: Path = DATA_DIR) -> SeedReport:
    """Load the corpus into an open session. Returns the totals afterwards."""
    manifest = _read_json(data_dir / "manifest.json")
    context = _read_json(data_dir / "company_context.json")

    company_id = context["company"]["short_name"]
    company_name = context["company"]["name"]
    docket = _plain(manifest["docket"]["docket_number"])
    proceeding_id = docket

    if session.get(Proceeding, proceeding_id) is None:
        session.add(
            Proceeding(
                id=proceeding_id,
                company_id=company_id,
                docket=docket,
                commission=_plain(manifest["docket"]["commission"]),
                subject=manifest["docket"]["subject"],
            )
        )
        session.flush()

    sources = {
        version["id"]: _read_source(data_dir / version["file"])
        for version in manifest["versions"]
    }
    version_labels = {v["id"]: v["label"] for v in manifest["versions"]}

    rows: list[Change] = []
    previous: str | None = None
    for version in manifest["versions"]:
        rows += ingest_and_diff(
            session,
            company_id=company_id,
            proceeding_id=proceeding_id,
            version_id=version["id"],
            label=version["label"],
            status=version["status"],
            source_text=sources[version["id"]],
            previous_version_id=previous,
        )
        previous = version["id"]

    touched: list[str] = []
    # Kept so the workspace hangs off the same rows the claims do: a project
    # attachment, a plan step and a finding all point at the change a claim's
    # citation lands in, rather than at a second row that happens to be nearby.
    anchors: dict[str, Change] = {}

    # One claim per labelled change, cited at the manifest's own offsets. These
    # verify, and the test suite asserts that they do -- if the corpus is edited
    # without the manifest, this is where it shows.
    for change in manifest["changes"]:
        after = change["after"]
        anchor = _anchor(rows, after["version"], after["start"], after["end"])
        anchors[change["id"]] = anchor
        _add_claim(
            session,
            claim_id=f"CLM-{change['id']}",
            company_id=company_id,
            change=anchor,
            statement=statement_for(change, version_labels),
            version_id=after["version"],
            start=after["start"],
            end=after["end"],
            quote=after["exact_text"],
        )
        touched.append(anchor.id)

    # Failure one: real offsets, invented words. The quote was altered in place
    # so the span is still the right length and still inside the document --
    # everything about this citation looks correct except what it says.
    definition = next(c for c in manifest["changes"] if c["id"] == "CHG-2")["after"]
    fabricated = definition["exact_text"].replace("20 megawatts", "10 megawatts")
    anchor = _anchor(
        rows, definition["version"], definition["start"], definition["end"]
    )
    misquote_change = anchor
    _add_claim(
        session,
        claim_id=CLAIM_MISQUOTE,
        company_id=company_id,
        change=anchor,
        statement=(
            "The Large Load Customer threshold moved to "
            f"{_first(_MEASURE.findall(fabricated), 'a load threshold', fabricated)}."
        ),
        version_id=definition["version"],
        start=definition["start"],
        end=definition["end"],
        quote=fabricated,
    )
    touched.append(anchor.id)

    # Failure two: a sentence that appears three times in the version, cited
    # without saying which one. The words match the source exactly at these
    # offsets, so text equality alone would pass it. The claim is about study
    # records; the offsets point at the general recordkeeping section. Which
    # occurrence a citation means is part of what it means.
    boilerplate = manifest["repeated_boilerplate"]
    occurrence = next(
        o
        for o in boilerplate["occurrences"]
        if o["version"] == "v3" and o["section"].startswith("6.3")
    )
    anchor = _anchor(
        rows, occurrence["version"], occurrence["start"], occurrence["end"]
    )
    ambiguous_change = anchor
    _add_claim(
        session,
        claim_id=CLAIM_AMBIGUOUS,
        company_id=company_id,
        change=anchor,
        statement=(
            "The Utility must keep study records for "
            + _first(
                _RETENTION.findall(boilerplate["sentence"]),
                "a retention period",
                boilerplate["sentence"],
            )
            + "."
        ),
        version_id=occurrence["version"],
        start=occurrence["start"],
        end=occurrence["end"],
        quote=boilerplate["sentence"],
        occurrence=None,
    )
    touched.append(anchor.id)

    # Ask the product, rather than deciding here, which claims may be asserted.
    # The escalations then say exactly what the workspace will withhold, because
    # the same function produced both.
    withheld: list[tuple[str, str]] = []
    for change_id in sorted(set(touched)):
        _verified, held_back = verified_claims(session, company_id, change_id)
        for held in held_back:
            _escalate(session, company_id, held)
            withheld.append((held.claim_id, held.reason_code))

    persisted = changes_for_proceeding(session, company_id, proceeding_id)

    # The workspace is built last, from the rows above rather than from the
    # files again. Every project attachment, plan step and finding points at a
    # change a claim already anchored to, so the two halves of the seed cannot
    # drift into describing different rows.
    workspace = _load_workspace(
        session,
        _Loaded(
            company_id=company_id,
            docket=docket,
            manifest=manifest,
            context=context,
            anchors=anchors,
            misquote_change=misquote_change,
            ambiguous_change=ambiguous_change,
            changes=tuple(persisted),
            now=datetime.now(timezone.utc),
        ),
    )

    return SeedReport(
        company_id=company_id,
        company_name=company_name,
        proceeding_id=proceeding_id,
        versions=len(versions_for_company(session, company_id)),
        changes=len(persisted),
        claims=sum(
            len(claims_for_change(session, company_id, row.id)) for row in persisted
        ),
        escalations=len(escalations_for_company(session, company_id)),
        withheld=tuple(sorted(withheld)),
        workspace=workspace,
    )


def main() -> None:
    ensure_tables()
    with session_scope() as session:
        report = load(session)
        # The corpus, then the people who may read it. Both in one transaction,
        # so a half-seeded database is not a state anyone can start from.
        accounts = ensure_accounts(session)
        # Then what the analyst proposes doing about it, which is the decision
        # the approval gate exists to gate. Third and last, because it needs
        # both of the above: the claims from the corpus, the author from the
        # accounts.
        proposals = ensure_demo_actions(session)

    print(f"seed: company {report.company_id} ({report.company_name})")
    print(
        f"seed: docket {report.proceeding_id} -- {report.versions} versions, "
        f"{report.changes} changes"
    )
    print(f"seed: {report.claims} claims, {report.escalations} escalated")
    for claim_id, reason_code in report.withheld:
        print(f"seed:   withheld {claim_id} -- {reason_code}")

    workspace = report.workspace
    print(
        f"seed: {workspace.projects} projects, {workspace.attachments} changes "
        f"attached, {workspace.threads} threads, {workspace.work_plan_steps} "
        f"plan steps, {workspace.scheduled_runs} scheduled runs"
    )
    print(
        f"seed: {workspace.sources} sources, {workspace.findings} findings, "
        f"{workspace.questions} questions, {workspace.knowledge_items} "
        f"knowledge items, {workspace.steer_directives} steer directives"
    )
    print(
        f"seed: the collective take rests on {workspace.take_included} findings "
        f"and states {workspace.take_withheld} it could not verify"
    )
    print(
        f"seed: {len(proposals)} actions proposed by the analyst, waiting on an "
        "obligation owner at /actions"
    )
    print(f"seed: {len(accounts)} accounts, all with the password {DEMO_PASSWORD!r}")
    for account in accounts:
        print(f"seed:   {account.email} -- {account.role} ({account.title})")
    print(
        "seed: those passwords are published on the login page. Set "
        "STRATA_DEMO_ACCOUNTS=0 and seed your own accounts before this faces "
        "anyone."
    )
    print("seed: run it again and these numbers do not move.")


if __name__ == "__main__":
    main()
