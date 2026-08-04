"""What Clarke can actually do. This module is the security boundary; the model is not.

THE ISOLATION DESIGN, IN ONE SENTENCE. Every tool takes company_id and actor as
keyword-only arguments that the caller injects from the signed-in session, and
the model supplies neither. A model that can pass a company_id is a model that
can be talked into passing a different one, and a prompt injection buried in a
filing then reads another tenant. The model chooses WHICH tool and the
non-identity arguments. Nothing else.

THE DISPATCHER REFUSES IDENTITY ARGUMENTS; IT DOES NOT DROP THEM. An argument
dict carrying company_id, actor, user_id or session halts the turn and appends a
row to the audit chain. Ignoring the argument would produce a correct, correctly
scoped answer and destroy the only evidence that an injection reached the tool
layer -- and the next attempt would be invisible too. Silence here is the whole
attack. The refusal names the offending KEYS and never the values: the value is
attacker-controlled text, the chain cannot be redacted afterwards, and the key
is what makes the attempt countable.

TWO KINDS OF REFUSAL, KEPT APART. An identity argument or an actor who does not
belong to the scope is an attack signature and sets ``halt_turn``: the caller
stops the turn rather than letting the model try again. An unknown tool, an
undeclared argument or a value of the wrong type is a fumble, and the model may
pick something else. Folding the two together would bury the attack among the
typos, which is the argument app/state/audit.py already makes for keeping
approval.waived apart from action.approved.

THE ALLOWLIST IS PER TOOL AND IS CHECKED AGAINST THE SIGNATURE. Every tool
declares the arguments a model may supply, the registry refuses to build if that
set disagrees with the function's own keyword-only parameters, and the dispatcher
refuses anything outside it. Identity arguments are therefore refused twice over:
once by name, and again because no tool declares them.

WITHHELD AND OMITTED ARE DIFFERENT WORDS AND DIFFERENT KEYS. ``withheld`` counts
claims whose text is not shown because a citation did not verify -- the thing
this product exists to do. ``omitted`` counts rows a length cap left out. Folding
them into one number would let "we trimmed the list" read as "a citation failed",
and the count that matters would stop meaning anything. Both keys are present on
EVERY result, including refusals, so a caller summing them can never reach a
missing key and read the absence as zero.

WHAT COMES BACK FROM A WITHHELD CLAIM. The reason, the reason code, the claim id
and the escalation id. Never the statement, never the quote, never the source
excerpt. app/state/claims.py already makes the statement unreachable --
WithheldClaim has no such field and slots=True stops one being attached -- and
_withheld_payload below is the single place a WithheldClaim becomes wire form, so
there is one function to read when asking whether an unasserted claim can escape.
The quote and the excerpt are dropped on top of that, deliberately: they belong
in the citation viewer where a person compares them side by side, and chat is a
surface where a model paraphrases whatever it is handed.

WHAT IS AUDITED, AND WHAT IS NOT. Writes and refusals. Reads are not, following
app/auth/policy.py: auditing every read would fill the chain with rows nobody
decided, and a chain of noise is a chain nobody reads.

WHAT THIS DOES NOT PROTECT AGAINST, said plainly.

- Nothing here rate-limits. A model in a loop can call read tools as fast as the
  process allows.
- A tool result is text a model will summarise. Nothing in this file can stop it
  paraphrasing badly; persona.py's principle is the control there, and the
  counts above exist so a bad summary is checkable rather than invisible.
- The audit chain does not record that a project was created through chat rather
  than by clicking. projects.create_project writes the decision and writing a
  second row under a chat code would give one act two records, which this
  codebase refuses everywhere else. Closing it means a surface column on
  AuditEvent, which is another agent's file. It is in the handoff list.
- record_note has no permission code in front of it, because the product does
  not define one and the equivalent screen action has no gate either.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

# THE MODULE, NOT THE NAMES. app/auth/policy.py reads its approval mode at
# import, so tests reload it and a bound `PermissionDenied` would stop being the
# class the reloaded gate raises. Referring to policy.require and
# policy.PermissionDenied looks them up when they are used, so a reloaded gate is
# the gate that runs. app/web/views/admin.py takes the same care for the same
# reason.
from app.auth import policy
from app.pipeline import ACTION_RECORD_CHANGE
from app.state.audit import ACTOR_USER, record_event
from app.state.claims import (
    VerifiedClaim,
    WithheldClaim,
    change_for_company,
    changes_for_proceeding,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.identity import user_for_company
from app.state.models import (
    AUTHOR_SYSTEM,
    PERMISSION_CODES,
    STATUS_ACTIVE,
    AuditEvent,
    Change,
    User,
)
from app.state.projects import (
    add_turn,
    changes_for_project,
    create_project as write_project,
    open_thread,
    project_card,
    project_for_company,
    projects_for_company,
    threads_for_project,
)

# The one definition of an unscoped read, imported rather than restated. Two
# copies of a tenant guard drift, and the copy nobody audited is the one that
# leaks.
from app.state.queries import _require_scope, versions_for_company
from app.state.routing import (
    escalations_for_user,
    obligation_for_company,
    obligations_for_company,
    resolve_escalation_owner,
)

# THE ROUTE HAS ONE READER AND THIS IS NOT IT. app/web/views/admin.py holds the
# only reader of the workflow tables and app/web/views/workflow.py holds the only
# translation of that vocabulary into sentences. Building a second reader here
# would be a second answer to "what is the live route", and on the day the two
# disagreed there would be no way to say which surface was lying -- the argument
# workflow.py itself makes for embedding admin.py rather than rebuilding it.
#
# The cost is a layering inversion: the chat tool layer now imports a web view,
# and with it FastAPI. Both files say in their own docstrings that these reads
# belong in app/state/, and tests/test_workflow.py already imports an
# app.state.workflow that does not exist yet, so the move is coming. When it
# lands this import block changes and nothing else does. It is in the handoff
# list. _walk and _item_state are private names and are imported knowingly: a
# copy of either is a second ordering of the same graph.
from app.web.views.admin import active_workflow_for_company, steps_and_edges
from app.web.views.workflow import (
    NO_ROUTE,
    ORDER_FALLBACK,
    _item_state,
    _walk,
    describe_assignee,
    describe_timeout,
    run_for_company,
)

# ---------------------------------------------------------------------------
# Audited actions
#
# THESE BELONG IN app/state/audit.py beside the rest of the vocabulary, and they
# are here for the same reason app/state/routing.py and app/state/review.py
# declare their own: that file is owned by another agent in this build and a
# parallel edit would lose their work. Moving them is a handoff, not a rewrite.
# app/chat/persona.py already writes two chat codes as bare strings on its
# Screened verdict; they are deliberately not restated here, because a second
# spelling of a string another module owns is the drift these constants exist to
# stop. A caller passes screened.audit_action straight through.
# ---------------------------------------------------------------------------

#: A tool call arrived carrying company_id, actor, user_id or session. Its own
#: code, not folded into the one below, because this is the attack signature and
#: it has to be countable without grepping prose.
ACTION_CHAT_IDENTITY_INJECTED = "chat.identity_injected"

#: The dispatcher would not run a tool: unknown name, undeclared argument, a
#: value of the wrong type, an actor who does not belong to the scope.
ACTION_CHAT_TOOL_REFUSED = "chat.tool_refused"

#: A note reached the record through Clarke. Audited although the underlying
#: write is not: app/state/projects.py leaves a research turn out of the chain
#: because enquiry is not commitment, and that reasoning is about a person
#: typing. Text entering a tenant's record because a model chose to put it there
#: is a decision, and the row names the person whose turn it ran on.
ACTION_CHAT_NOTE_RECORDED = "chat.note_recorded"

# ---------------------------------------------------------------------------
# Refusal codes. For a caller to branch on; the reason beside them is for a
# person to read.
# ---------------------------------------------------------------------------

REASON_IDENTITY_ARGUMENT = "IDENTITY_ARGUMENT"
REASON_ACTOR_MISMATCH = "ACTOR_MISMATCH"
REASON_ACTOR_INACTIVE = "ACTOR_INACTIVE"
REASON_UNKNOWN_TOOL = "UNKNOWN_TOOL"
REASON_UNKNOWN_ARGUMENT = "UNKNOWN_ARGUMENT"
REASON_BAD_ARGUMENT = "BAD_ARGUMENT"
REASON_NOT_PERMITTED = "NOT_PERMITTED"
REASON_NOT_FOUND = "NOT_FOUND"

#: The four names the tool layer injects. A tool call carrying any of them is
#: refused whatever the value -- including the caller's own company id, because
#: what is being recorded is that the model supplied identity at all, and a check
#: that only fires on a mismatch misses the reconnaissance.
IDENTITY_ARGUMENTS = frozenset({"company_id", "actor", "user_id", "session"})

# The permission in front of the one gated write. Named here so a rename in
# models.py fails at import rather than leaving a gate nobody can pass.
PERMISSION_PROJECT_CREATE = "project.create"

# ---------------------------------------------------------------------------
# Caps. A tool that hands a model two hundred rows spends the turn on rows
# nobody asked for, so every list is capped and every cap is counted in
# ``omitted`` and said in ``note``.
# ---------------------------------------------------------------------------

MAX_ROWS = 25
DEFAULT_CHANGE_LIMIT = 10
MAX_CHANGE_LIMIT = 50
MAX_EXCERPT = 600
MAX_NOTE_CHARS = 4000

# ---------------------------------------------------------------------------
# Ordering, announced rather than assumed
# ---------------------------------------------------------------------------

#: Neither Change nor DocumentVersion carries a timestamp, so "newest first" has
#: no column to read. The audit chain does: the pipeline appends a record_change
#: row per change, per company, and seq is monotonic within a company. That is
#: the honest clock -- when the change entered the record -- and it is the
#: artefact this product already asks a regulator to trust.
ORDER_BY_CHAIN = "audit_chain"
#: Some changes have no recording event: seeded by hand, loaded by a migration,
#: or written before the pipeline audited. They sort last, by id, and the note
#: says so. A fallback must announce itself (best-practices §26).
ORDER_BY_CHAIN_THEN_ID = "audit_chain_then_id"
#: Nothing in scope has a recording event, so the order is id order and is not a
#: statement about time at all.
ORDER_BY_ID = "change_id"
ORDER_BASES = (ORDER_BY_CHAIN, ORDER_BY_CHAIN_THEN_ID, ORDER_BY_ID)

_ORDER_NOTES = {
    ORDER_BY_CHAIN: (
        "Newest first, by when the audit chain recorded each change."
    ),
    ORDER_BY_CHAIN_THEN_ID: (
        "Newest first by when the audit chain recorded each change. Some of "
        "these have no recording event, so they are listed last in id order and "
        "their position says nothing about when they arrived."
    ),
    ORDER_BY_ID: (
        "None of these has a recording event in the audit chain, so they are in "
        "id order. That is not a statement about which moved most recently."
    ),
}

#: The question field of the one thread per project that holds Clarke's notes.
#: A label rather than a question, which is the one place in this codebase a
#: column holds something other than what it is named for -- conceded in
#: record_note and in the handoff list.
NOTES_THREAD_LABEL = "Notes recorded through Clarke"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

_RESERVED = ("tool", "ok", "found", "withheld", "omitted", "note")


def _result(
    tool: str,
    *,
    found: int,
    withheld: int = 0,
    omitted: int = 0,
    note: str = "",
    **payload: Any,
) -> dict:
    """The one shape every tool returns. The six keys are always present.

    A tool that forgets ``withheld`` cannot exist, because the key is written
    here rather than by the tool -- which is the difference between a caller
    summing a number and a caller reaching ``.get("withheld", 0)`` and reading
    an absence as a measurement.
    """
    clash = sorted(set(payload) & set(_RESERVED))
    if clash:
        raise ValueError(
            f"{tool} tried to overwrite the reserved result keys {clash}. Those "
            "six keys mean the same thing on every tool and a payload cannot "
            "redefine them."
        )
    result = {
        "tool": tool,
        "ok": True,
        "found": found,
        "withheld": withheld,
        "omitted": omitted,
        "note": note,
    }
    result.update(payload)
    return result


def _refusal(
    tool: str, *, reason_code: str, reason: str, halt_turn: bool = False
) -> dict:
    """A refusal in the same shape, so a caller never branches on key presence."""
    return {
        "tool": tool,
        "ok": False,
        "found": 0,
        "withheld": 0,
        "omitted": 0,
        "note": reason,
        "reason_code": reason_code,
        "reason": reason,
        "halt_turn": halt_turn,
    }


def _refuse(
    session: Session,
    *,
    company_id: str,
    actor: User,
    tool: str,
    reason_code: str,
    reason: str,
    action: str = ACTION_CHAT_TOOL_REFUSED,
    halt_turn: bool = False,
) -> dict:
    """Refuse, and leave the attempt in the chain. The refusal is the evidence.

    The row is written under the CALLER's company, never under anything an
    argument named. Attribution carries the signed-in person, because they are
    the account the turn ran as; which tool the model chose is the subject.
    """
    record_event(
        session,
        company_id=company_id,
        actor=_actor_label(actor),
        action=action,
        subject_type="chat_tool",
        subject_id=tool,
        reason=reason,
        actor_user_id=getattr(actor, "id", None),
        actor_kind=ACTOR_USER,
    )
    return _refusal(tool, reason_code=reason_code, reason=reason, halt_turn=halt_turn)


def _actor_label(actor) -> str:
    """The name an audit row carries. Never an identity on its own."""
    email = getattr(actor, "email", "") or ""
    return email or "unknown user"


# ---------------------------------------------------------------------------
# Claims, in and out of the wire
# ---------------------------------------------------------------------------


def _verified_payload(claim: VerifiedClaim, change_id: str) -> dict:
    """A claim the source supports, with the citation that earned it."""
    return {
        "claim_id": claim.claim_id,
        "change_id": change_id,
        "statement": claim.statement,
        "confidence_bp": claim.confidence_bp,
        "citation": {
            "version_id": claim.citation_version_id,
            "start": claim.citation_start,
            "end": claim.citation_end,
            "quote": claim.citation_quote,
            "occurrence": claim.cited_occurrence,
        },
    }


def _withheld_payload(
    claim: WithheldClaim, change_id: str, escalation_id: str | None
) -> dict:
    """The ONE place a WithheldClaim becomes wire form. Field by field, on purpose.

    Never ``dataclasses.asdict``, never a loop over __slots__: a field added to
    WithheldClaim later would then appear here without anybody deciding it
    should. app/state/claims.py._withhold is the one place the statement is
    dropped; this is the one place to read when asking whether it can come back.

    citation_quote and source_excerpt are dropped as well, and that is a
    narrowing on top of what claims.py guarantees. They are source text rather
    than the claim, and the change view shows them side by side so an analyst can
    see the mismatch itself. A model handed a quote will fold it into a sentence,
    and a sentence built around the words a failed citation quoted is close
    enough to the assertion to do the harm the refusal exists to prevent.
    """
    return {
        "claim_id": claim.claim_id,
        "change_id": change_id,
        "reason_code": claim.reason_code,
        "reason_text": claim.reason_text,
        "escalation_id": escalation_id,
        "where": "the review queue",
    }


def _escalation_by_claim(session: Session, company_id: str) -> dict[str, str]:
    """claim id to escalation id, preferring the one still open.

    A claim can carry more than one escalation -- it was refused, resolved, and
    refused again. The open one is what a person can act on, so it wins; with
    none open the latest is named, because "it was refused and somebody closed
    it" is still the answer to where this went.
    """
    _require_scope(company_id)
    chosen: dict[str, str] = {}
    open_rows: set[str] = set()
    for row in escalations_for_company(session, company_id):
        if row.claim_id in open_rows and row.resolved_at is not None:
            continue
        chosen[row.claim_id] = row.id
        if row.resolved_at is None:
            open_rows.add(row.claim_id)
    return chosen


def _matches(text: str, query: str) -> bool:
    """Every word of the query appears somewhere in the text. Empty matches all.

    Deliberately dumb. A cleverer matcher would rank, and a ranked list of claims
    is a judgement about which one answers the question -- which is the analyst's
    call, not the clerk's.
    """
    words = (query or "").lower().split()
    if not words:
        return True
    haystack = (text or "").lower()
    return all(word in haystack for word in words)


# ---------------------------------------------------------------------------
# Scoped reads that have no home yet
#
# These two belong in app/state/queries.py beside versions_for_company, exactly
# as the four in app/web/views/admin.py do. They are here because that file is
# owned by another agent in this build. The tenant guard is IMPORTED rather than
# copied, so there is still one definition of what an unscoped read is, which is
# the property that file exists for. Moving them is a handoff.
# ---------------------------------------------------------------------------


def _recorded_seq(session: Session, company_id: str) -> dict[str, int]:
    """Change id to the audit sequence that recorded it. The only clock there is."""
    _require_scope(company_id)
    rows = (
        session.query(AuditEvent.subject_id, AuditEvent.seq)
        .filter(AuditEvent.company_id == company_id)
        .filter(AuditEvent.subject_type == "change")
        .filter(AuditEvent.action == ACTION_RECORD_CHANGE)
        .all()
    )
    seen: dict[str, int] = {}
    for subject_id, seq in rows:
        if subject_id:
            seen[subject_id] = max(seen.get(subject_id, 0), seq)
    return seen


def _changes_for_company(session: Session, company_id: str) -> list[Change]:
    """Every change this company owns, through the per-proceeding scoped read.

    Built from proceedings_for_company and changes_for_proceeding rather than one
    query over Change, so the join that makes the parent agree runs here too. A
    write bug that stamped the wrong company on a change is caught by the
    proceeding above it, which is the whole reason claims.py does it that way.
    """
    _require_scope(company_id)
    rows: list[Change] = []
    for proceeding in proceedings_for_company(session, company_id):
        rows.extend(changes_for_proceeding(session, company_id, proceeding.id))
    return rows


def _version_labels(session: Session, company_id: str) -> dict[str, str]:
    return {
        version.id: version.label
        for version in versions_for_company(session, company_id)
    }


def _source_by_version(session: Session, company_id: str) -> dict[str, str]:
    return {
        version.id: version.source_text
        for version in versions_for_company(session, company_id)
    }


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _cap(rows: list, limit: int) -> tuple[list, int]:
    return rows[:limit], max(0, len(rows) - limit)


def _omission_note(omitted: int) -> str:
    if omitted <= 0:
        return ""
    thing = "row" if omitted == 1 else "rows"
    return f" {omitted} more {thing} not shown; narrow the question to see them."


def _excerpt(text: str, start: int | None, end: int | None) -> tuple[str, bool]:
    """A span of the stored source, and whether it was cut short."""
    if start is None or end is None or not text:
        return "", False
    span = text[start:end]
    if len(span) <= MAX_EXCERPT:
        return span, False
    return span[:MAX_EXCERPT], True


# ---------------------------------------------------------------------------
# The read tools
# ---------------------------------------------------------------------------


def find_projects(
    session: Session, *, company_id: str, actor: User, query: str = ""
) -> dict:
    """Projects whose name, jurisdiction or docket carries the words asked for."""
    _require_scope(company_id)
    rows = [
        project
        for project in projects_for_company(session, company_id)
        if _matches(
            " ".join(
                filter(None, [project.name, project.jurisdiction, project.docket_ref])
            ),
            query,
        )
    ]
    shown, omitted = _cap(rows, MAX_ROWS)
    note = (
        "Nothing on the record matches that." if not rows else ""
    ) + _omission_note(omitted)
    return _result(
        "find_projects",
        found=len(shown),
        omitted=omitted,
        note=note.strip(),
        projects=[
            {
                "project_id": project.id,
                "name": project.name,
                "jurisdiction": project.jurisdiction,
                "docket_ref": project.docket_ref,
                "status": project.status,
                "owner": project.owner,
            }
            for project in shown
        ],
    )


def project_detail(
    session: Session, *, company_id: str, actor: User, project_id: str
) -> dict:
    """Status, owner, counts and the next re-run. Every number derived, none stored."""
    _require_scope(company_id)
    project = project_for_company(session, company_id, project_id)
    if project is None:
        return _result(
            "project_detail",
            found=0,
            note=f"There is no project {project_id!r} on this company's record.",
            project=None,
        )
    card = project_card(session, company_id, project_id)
    return _result(
        "project_detail",
        found=1,
        project={
            "project_id": project.id,
            "name": project.name,
            "jurisdiction": project.jurisdiction,
            "docket_ref": project.docket_ref,
            "status": project.status,
            "owner": project.owner,
            "summary": project.summary,
            "change_count": card.change_count if card else 0,
            "unreviewed_count": card.unreviewed_count if card else 0,
            "open_thread_count": card.open_thread_count if card else 0,
            "next_run_at": _stamp(card.next_run_at) if card else None,
            "last_activity_at": _stamp(card.last_activity_at) if card else None,
        },
    )


def latest_changes(
    session: Session,
    *,
    company_id: str,
    actor: User,
    project_id: str | None = None,
    limit: int | None = None,
) -> dict:
    """What moved, newest first, with the version pair each change sits between.

    Claim-level withholding is not measured here and the note says so. Measuring
    it would mean re-verifying every claim under every change on a list read,
    and reporting a zero without doing that work would be the silent count this
    whole module exists to refuse.
    """
    _require_scope(company_id)
    asked = None if limit is None else int(limit)
    wanted = DEFAULT_CHANGE_LIMIT if asked is None else max(1, min(asked, MAX_CHANGE_LIMIT))
    # A clamp in silence is a fallback that does not announce itself: the reply
    # would say "here are the changes" over a list the tool quietly resized.
    clamp_note = (
        ""
        if asked is None or asked == wanted
        else (
            f" You asked for {asked}; this tool reads between 1 and "
            f"{MAX_CHANGE_LIMIT}, so it read {wanted}."
        )
    )

    if project_id:
        if project_for_company(session, company_id, project_id) is None:
            return _result(
                "latest_changes",
                found=0,
                note=f"There is no project {project_id!r} on this company's record.",
                ordered_by=ORDER_BY_ID,
                changes=[],
            )
        rows = changes_for_project(session, company_id, project_id)
    else:
        rows = _changes_for_company(session, company_id)

    recorded = _recorded_seq(session, company_id)
    with_event = sum(1 for change in rows if change.id in recorded)
    if not rows or with_event == 0:
        basis = ORDER_BY_ID
    elif with_event == len(rows):
        basis = ORDER_BY_CHAIN
    else:
        basis = ORDER_BY_CHAIN_THEN_ID

    rows.sort(key=lambda change: (recorded.get(change.id, 0), change.id), reverse=True)
    shown, omitted = _cap(rows, wanted)

    labels = _version_labels(session, company_id)
    note = _ORDER_NOTES[basis] + _omission_note(omitted) + clamp_note
    note += (
        " Whether any claim under these changes is withheld is not measured here;"
        " open a change to see that."
    )

    return _result(
        "latest_changes",
        found=len(shown),
        omitted=omitted,
        note=note.strip(),
        ordered_by=basis,
        changes=[
            {
                "change_id": change.id,
                "proceeding_id": change.proceeding_id,
                "change_type": change.change_type,
                "section": change.section,
                "status": change.status,
                "alignment_confidence": change.alignment_confidence,
                "from_version_id": change.from_version_id,
                "to_version_id": change.to_version_id,
                "from_version_label": labels.get(change.from_version_id),
                "to_version_label": labels.get(change.to_version_id),
                "recorded_seq": recorded.get(change.id),
            }
            for change in shown
        ],
    )


def change_detail(
    session: Session, *, company_id: str, actor: User, change_id: str
) -> dict:
    """The diff, its alignment confidence, and its claims -- verified and withheld.

    The before and after text are slices of the STORED SOURCE at the offsets the
    change records, not a rendering of anything derived. That is what makes them
    checkable: a reader can open the version and find the same bytes.
    """
    _require_scope(company_id)
    change = change_for_company(session, company_id, change_id)
    if change is None:
        return _result(
            "change_detail",
            found=0,
            note=f"There is no change {change_id!r} on this company's record.",
            change=None,
            claims=[],
            withheld_claims=[],
        )

    sources = _source_by_version(session, company_id)
    labels = _version_labels(session, company_id)
    before, before_cut = _excerpt(
        sources.get(change.from_version_id, ""), change.before_start, change.before_end
    )
    after, after_cut = _excerpt(
        sources.get(change.to_version_id, ""), change.after_start, change.after_end
    )

    verified, withheld = verified_claims(session, company_id, change_id)
    escalations = _escalation_by_claim(session, company_id)

    note = ""
    if before_cut or after_cut:
        note += f" The quoted text is cut at {MAX_EXCERPT} characters."
    if change.materiality is None:
        note += " Nothing has judged how material this is; the column is empty."
    if withheld:
        held = "claim" if len(withheld) == 1 else "claims"
        note += (
            f" {len(withheld)} {held} on this change are not shown: their citations"
            " did not verify, and the reason is given in place of the statement."
        )

    return _result(
        "change_detail",
        found=len(verified),
        withheld=len(withheld),
        note=note.strip(),
        change={
            "change_id": change.id,
            "proceeding_id": change.proceeding_id,
            "change_type": change.change_type,
            "section": change.section,
            "status": change.status,
            "materiality": change.materiality,
            "alignment_confidence": change.alignment_confidence,
            "from_version_id": change.from_version_id,
            "to_version_id": change.to_version_id,
            "from_version_label": labels.get(change.from_version_id),
            "to_version_label": labels.get(change.to_version_id),
            "before_text": before,
            "after_text": after,
        },
        claims=[_verified_payload(claim, change.id) for claim in verified],
        withheld_claims=[
            _withheld_payload(claim, change.id, escalations.get(claim.claim_id))
            for claim in withheld
        ],
    )


def open_escalations(
    session: Session, *, company_id: str, actor: User, mine_only: bool | None = False
) -> dict:
    """What is withheld and why, and whose desk it is on.

    Every row here is a claim whose text is not being shown, so ``withheld``
    counts the whole queue rather than only the part that fitted. An escalation
    nobody holds carries the live reason it could not be routed, re-derived on
    this call: reinstating an account this afternoon changes the sentence without
    anything re-running.

    Escalation.detail is not returned. It quotes the source against what the
    citation claimed, which belongs beside the two texts on the change view.
    """
    _require_scope(company_id)
    mine = bool(mine_only)
    rows = (
        escalations_for_user(session, company_id, getattr(actor, "id", ""))
        if mine
        else escalations_for_company(session, company_id, unresolved_only=True)
    )
    shown, omitted = _cap(rows, MAX_ROWS)

    payload = []
    for row in shown:
        holder = (
            user_for_company(session, company_id, row.assigned_to_user_id)
            if row.assigned_to_user_id
            else None
        )
        if row.assigned_to_user_id and holder is None:
            held_by, why = None, (
                "This names an account this company does not have, so nobody "
                "holds it."
            )
        elif holder is not None:
            held_by, why = holder.display_name, ""
        else:
            resolution = resolve_escalation_owner(
                session, company_id, escalation_id=row.id
            )
            held_by, why = None, resolution.reason_text
        payload.append(
            {
                "escalation_id": row.id,
                "claim_id": row.claim_id,
                "reason_code": row.reason_code,
                "reason_text": row.reason_text,
                "assigned_to": held_by,
                "assigned_at": _stamp(row.assigned_at),
                "routing_note": why,
            }
        )

    scope = "assigned to you" if mine else "open for this company"
    note = f"Every item here is a claim whose text is not shown. Scope: {scope}."
    return _result(
        "open_escalations",
        found=len(payload),
        withheld=len(rows),
        omitted=omitted,
        note=note + _omission_note(omitted),
        escalations=payload,
    )


def obligation_owner(
    session: Session, *, company_id: str, actor: User, obligation_id: str | None = None
) -> dict:
    """Who owns what. An obligation with no owner reads as work, never as a blank."""
    _require_scope(company_id)
    if obligation_id:
        one = obligation_for_company(session, company_id, obligation_id)
        if one is None:
            return _result(
                "obligation_owner",
                found=0,
                note=f"There is no obligation {obligation_id!r} on this company's record.",
                obligations=[],
            )
        rows = [one]
    else:
        rows = obligations_for_company(session, company_id)

    shown, omitted = _cap(rows, MAX_ROWS)
    payload = []
    for row in shown:
        holder = (
            user_for_company(session, company_id, row.owner_user_id)
            if row.owner_user_id
            else None
        )
        if holder is not None:
            owner, why = holder.display_name, ""
        elif row.owner_user_id:
            owner, why = None, (
                "This names an account this company does not have, so nobody "
                "owns it in practice."
            )
        else:
            owner, why = None, (
                "Nobody owns this. An obligation with no owner is work the "
                "product has failed to route, not an empty column."
            )
        payload.append(
            {
                "obligation_id": row.id,
                "title": row.title,
                "owner": owner,
                "owner_user_id": row.owner_user_id,
                "project_id": row.project_id,
                "note": why,
            }
        )

    unowned = sum(1 for row in payload if row["owner"] is None)
    note = "" if not unowned else f"{unowned} of these have nobody holding them."
    return _result(
        "obligation_owner",
        found=len(payload),
        omitted=omitted,
        note=(note + _omission_note(omitted)).strip(),
        obligations=payload,
    )


def approval_route(
    session: Session, *, company_id: str, actor: User, item_id: str | None = None
) -> dict:
    """The live route in the order an item meets it, and where one item has reached.

    Every sentence here is composed by app/web/views/workflow.py, which is the
    only translation of the route vocabulary into words. A second one would be a
    second answer to what the route says.
    """
    _require_scope(company_id)
    workflow = active_workflow_for_company(session, company_id=company_id)
    if workflow is None:
        return _result(
            "approval_route", found=0, note=NO_ROUTE, route=None, steps=[], item=None
        )

    steps, edges = steps_and_edges(session, workflow)
    walked, ordered = _walk(steps, edges)
    item, latest, attempts = _item_state(
        session, company_id, workflow.id, item_id, None
    )

    current = None
    if item.known and item.run_id:
        run = run_for_company(session, company_id=company_id, run_id=item.run_id)
        current = run.current_step_id if run is not None else None

    payload = []
    for position, step in enumerate(walked, start=1):
        who = describe_assignee(session, company_id, step.assignee_rule)
        sentence, is_skip = describe_timeout(
            session,
            company_id,
            approval_hours=step.approval_hours,
            on_timeout=step.on_timeout,
            escalate_to=step.escalate_to,
            remind_every_hours=step.remind_every_hours,
        )
        state = latest.get(step.id)
        payload.append(
            {
                "position": position,
                "step_id": step.id,
                "label": step.label or step.id,
                "who": who.text,
                "who_certain": who.certain,
                "timeout": sentence,
                "timeout_is_skip": is_skip,
                "outcome": None if state is None else state.outcome,
                "attempts": attempts.get(step.id, 0),
                "is_current": step.id == current,
            }
        )

    note = "" if ordered else ORDER_FALLBACK
    if item_id and not item.known:
        note = (note + " " + item.note).strip()
    return _result(
        "approval_route",
        found=len(payload),
        note=note,
        route={"workflow_id": workflow.id, "name": workflow.name, "ordered": ordered},
        steps=payload,
        item={
            "item_id": item_id,
            "run_id": item.run_id,
            "known": item.known,
            "note": item.note,
        },
    )


def search_claims(
    session: Session,
    *,
    company_id: str,
    actor: User,
    query: str = "",
    project_id: str | None = None,
) -> dict:
    """Claims whose citation VERIFIES, searched by their own words.

    A WITHHELD CLAIM CANNOT BE SEARCHED, AND THAT IS THE DESIGN. Matching one
    would mean holding its statement long enough to compare it against the query,
    and the statement is not available here -- app/state/claims.py never hands it
    back. So every withheld claim in the searched scope is reported by count and
    by reason, whatever the query was. That is the honest answer to "is there
    anything about X": there may be, and here is what is stopping the product
    from saying so, and where to go and clear it.

    THE COST, PLAINLY. Verification re-runs against the stored source for every
    claim under every change in scope, and app/state/claims.py re-reads the
    company's sources once per change while doing it. On a corpus of dozens that
    is nothing. On thousands it is the first thing to fix, and the fix belongs in
    claims.py -- a verified_claims that takes a prepared source map -- not in a
    cache here, because a cached verdict is the stored verdict ADR-003 refuses.
    """
    _require_scope(company_id)

    scope_note = ""
    if project_id:
        if project_for_company(session, company_id, project_id) is None:
            return _result(
                "search_claims",
                found=0,
                note=f"There is no project {project_id!r} on this company's record.",
                claims=[],
                withheld_claims=[],
            )
        changes = changes_for_project(session, company_id, project_id)
        scope_note = f"Searched the changes attached to {project_id}."
    else:
        changes = _changes_for_company(session, company_id)
        scope_note = "Searched every change on this company's record."

    escalations = _escalation_by_claim(session, company_id)
    hits: list[dict] = []
    held: list[dict] = []
    for change in changes:
        verified, withheld = verified_claims(session, company_id, change.id)
        hits.extend(
            _verified_payload(claim, change.id)
            for claim in verified
            if _matches(claim.statement, query)
        )
        held.extend(
            _withheld_payload(claim, change.id, escalations.get(claim.claim_id))
            for claim in withheld
        )

    shown, omitted = _cap(hits, MAX_ROWS)
    held_shown, held_omitted = _cap(held, MAX_ROWS)

    note = scope_note
    if not hits:
        note += " No claim whose citation verifies matches that."
    if held:
        word = "claim" if len(held) == 1 else "claims"
        note += (
            f" {len(held)} {word} in this scope are withheld and could not be "
            "searched at all: a withheld claim's text is never read, so one of "
            "them may be the answer. The reasons are listed."
        )
    note += _omission_note(omitted + held_omitted)

    return _result(
        "search_claims",
        found=len(shown),
        withheld=len(held),
        omitted=omitted + held_omitted,
        note=note.strip(),
        claims=shown,
        withheld_claims=held_shown,
    )


# ---------------------------------------------------------------------------
# The two writes
# ---------------------------------------------------------------------------


def create_project(
    session: Session,
    *,
    company_id: str,
    actor: User,
    name: str,
    jurisdiction: str,
    docket: str | None = None,
) -> dict:
    """Open a project. Gated on project.create through app/auth/policy.py.

    The gate is not re-implemented and not consulted twice. policy.require
    refuses, writes the access.denied row itself, and raises; this catches the
    raise and turns it into a refusal the model can explain, because an exception
    reaching the turn handler becomes a stack trace rather than a sentence.

    No second audit row. projects.create_project appends project.created with the
    person as actor, and a chat-flavoured copy of the same decision would give
    one act two records -- the failure "one log, not two" is written against in
    three other modules here. What the chain therefore does not say is that this
    came through chat. That is conceded in the module docstring and is a handoff.
    """
    _require_scope(company_id)
    try:
        policy.require(
            session, company_id, getattr(actor, "id", ""), PERMISSION_PROJECT_CREATE
        )
    except policy.PermissionDenied as denied:
        # require() already wrote the refusal. A second row would double-count
        # one decision.
        return _refusal(
            "create_project",
            reason_code=REASON_NOT_PERMITTED,
            reason=(
                f"You do not hold {PERMISSION_PROJECT_CREATE}, so I cannot open a "
                f"project for you. {denied.reason}. An admin grants it."
            ),
        )

    owner = getattr(actor, "display_name", "") or _actor_label(actor)
    try:
        project = write_project(
            session,
            company_id,
            name=(name or "").strip(),
            jurisdiction=(jurisdiction or "").strip(),
            owner=owner,
            docket_ref=(docket or "").strip() or None,
            actor=_actor_label(actor),
        )
    except ValueError as bad:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool="create_project",
            reason_code=REASON_BAD_ARGUMENT,
            reason=str(bad),
        )

    return _result(
        "create_project",
        found=1,
        note=f"Opened {project.name} in {project.jurisdiction}, owned by {owner}.",
        project={
            "project_id": project.id,
            "name": project.name,
            "jurisdiction": project.jurisdiction,
            "docket_ref": project.docket_ref,
            "status": project.status,
            "owner": project.owner,
        },
    )


def record_note(
    session: Session, *, company_id: str, actor: User, project_id: str, text: str
) -> dict:
    """Write a note against a project. A note, never an approval.

    WHERE IT LANDS. ResearchTurn's own docstring says a turn that cites nothing
    is a note and the interface must read it as one, so that is the table. A turn
    needs a thread, and a note is not a question, so one thread per project
    carries them all under a label that says exactly what it is. That label sits
    in a column named `question`, which is the one place in this codebase a column
    holds something other than its name -- conceded here rather than discovered
    later. The fix is a kind column on ResearchThread, and models.py is another
    agent's file.

    WHY THE AUTHOR KIND IS NOT `analyst`. The vocabulary offers analyst or system
    and nothing else. A note relayed by a model is not the analyst's own
    keystrokes: the person said something, and what reached the column is what
    Clarke decided that meant. Recording it as an analyst turn claims a person
    typed words they may not have chosen. app/state/audit.py takes the same
    direction for the same reason -- record a machine rather than quietly claim a
    person. The audit row names the person whose turn it ran on, so attribution
    is not lost; a third author kind is the handoff.

    NO PERMISSION GATE, and this is a decision rather than an omission. The
    product defines no code for writing a note, the same act through the project
    screen has no gate, and inventing a code here would mean editing
    PERMISSION_CODES in models.py -- a second authorisation vocabulary rather
    than a use of the first. What that concedes: any signed-in person can fill a
    project with notes. They are attributed, they are append-only, and a note
    changes nothing on its own.

    THE NOTE TEXT IS NOT COPIED INTO THE AUDIT CHAIN. The chain is append-only
    and cannot be redacted, and a note is free text a person dictated. The row
    names the turn; the turn holds the words, once.
    """
    _require_scope(company_id)
    body = (text or "").strip()
    if not body:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool="record_note",
            reason_code=REASON_BAD_ARGUMENT,
            reason="A note needs words in it. Nothing was recorded.",
        )
    if len(body) > MAX_NOTE_CHARS:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool="record_note",
            reason_code=REASON_BAD_ARGUMENT,
            reason=(
                f"That note is longer than {MAX_NOTE_CHARS} characters. Nothing "
                "was recorded; shorten it or attach it to the project instead."
            ),
        )

    if project_for_company(session, company_id, project_id) is None:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool="record_note",
            reason_code=REASON_NOT_FOUND,
            reason=f"There is no project {project_id!r} on this company's record.",
        )

    threads = [
        thread
        for thread in threads_for_project(session, company_id, project_id)
        if thread.question == NOTES_THREAD_LABEL
    ]
    # Earliest by id when two exist. Two is possible -- nothing locks the read
    # against the write -- and picking the same one every time keeps the notes
    # together rather than splitting them across a pair.
    thread = min(threads, key=lambda row: row.id) if threads else open_thread(
        session,
        company_id,
        project_id,
        question=NOTES_THREAD_LABEL,
        opened_by=_actor_label(actor),
    )

    turn = add_turn(
        session,
        company_id,
        thread.id,
        author_kind=AUTHOR_SYSTEM,
        body=body,
        claim_id=None,
    )
    record_event(
        session,
        company_id=company_id,
        actor=_actor_label(actor),
        action=ACTION_CHAT_NOTE_RECORDED,
        subject_type="research_turn",
        subject_id=str(turn.id),
        reason=(
            f"note recorded on project {project_id} through chat, in thread "
            f"{thread.id}. The words are in the turn, not here."
        ),
        actor_user_id=getattr(actor, "id", None),
        actor_kind=ACTOR_USER,
    )
    return _result(
        "record_note",
        found=1,
        note="Recorded as a note. It is not an approval and nothing was decided.",
        project_id=project_id,
        thread_id=thread.id,
        turn_id=turn.id,
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool, exactly what a model may supply to it, and what it is gated on.

    ``permission`` is the code the tool checks before it writes, or "" where it
    checks none. It is carried here rather than left for each caller to look up,
    because "which tool is gated" would otherwise be spelt in two places -- here
    and in whatever offers the tools to a model -- and the copy that went stale
    would be the one deciding what a person is told they can do.

    It is a HINT AND NEVER THE GATE. The gate is policy.require inside the tool,
    on the call that is about to write. A caller reading this field to grey out a
    control is doing what app/auth/policy.py's has() is for; a caller reading it
    and then skipping the tool call is deciding an authorisation question from a
    string, and the grant can be revoked between the two.
    """

    name: str
    run: Callable[..., dict]
    arguments: frozenset[str]
    writes: bool
    permission: str = ""


#: One name, one type, across every tool. An argument called project_id means the
#: same thing wherever it appears, so the check lives here rather than being
#: repeated per tool. None is accepted everywhere: a model that means "not given"
#: writes null often enough that refusing it would be a refusal about JSON.
_ARGUMENT_TYPES: dict[str, tuple[type, ...]] = {
    "query": (str,),
    "project_id": (str,),
    "change_id": (str,),
    "obligation_id": (str,),
    "item_id": (str,),
    "name": (str,),
    "jurisdiction": (str,),
    "docket": (str,),
    "text": (str,),
    # bool before int, because bool IS an int in Python and a limit of True
    # would otherwise become a limit of 1.
    "limit": (int,),
    "mine_only": (bool,),
}


def _declare(
    run: Callable[..., dict], *, writes: bool = False, permission: str = ""
) -> Tool:
    """Build a tool, and refuse at import if its allowlist disagrees with itself.

    Three checks, all of them loud at import rather than quiet for ever:
    the identity arguments are keyword-only with no default, no tool declares an
    identity argument, and every declared argument has a type in the table above.
    A tool added later that gets any of this wrong stops the process at start.
    """
    signature = inspect.signature(run)
    parameters = list(signature.parameters.values())
    if not parameters or parameters[0].name != "session":
        raise ValueError(f"{run.__name__} must take the session first")
    for wanted in ("company_id", "actor"):
        parameter = signature.parameters.get(wanted)
        if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise ValueError(
                f"{run.__name__}.{wanted} must be keyword-only. The model must "
                "never be able to fill it positionally."
            )
        if parameter.default is not inspect.Parameter.empty:
            raise ValueError(
                f"{run.__name__}.{wanted} must have no default. A default is a "
                "scope nobody chose."
            )
    declared = {
        parameter.name
        for parameter in parameters[1:]
        if parameter.name not in ("company_id", "actor")
    }
    for parameter in parameters[1:]:
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise ValueError(
                f"{run.__name__}.{parameter.name} must be keyword-only, so a "
                "positional call cannot shift an argument into the scope slot."
            )
    overlap = declared & IDENTITY_ARGUMENTS
    if overlap:
        raise ValueError(f"{run.__name__} declares identity arguments {sorted(overlap)}")
    untyped = sorted(declared - set(_ARGUMENT_TYPES))
    if untyped:
        raise ValueError(
            f"{run.__name__} declares {untyped}, which _ARGUMENT_TYPES does not "
            "type. An argument nobody typed is a value the dispatcher waves "
            "through."
        )
    if permission and permission not in PERMISSION_CODES:
        # Loud at import beats quiet for ever. A gate naming a code the product
        # does not define would be a control nobody can pass, with nothing to
        # say why -- the same failure app/auth/policy.py refuses at import.
        raise ValueError(
            f"{run.__name__} is gated on {permission!r}, which is not a "
            "permission this product defines."
        )
    return Tool(run.__name__, run, frozenset(declared), writes, permission)


REGISTRY: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        _declare(find_projects),
        _declare(project_detail),
        _declare(latest_changes),
        _declare(change_detail),
        _declare(open_escalations),
        _declare(obligation_owner),
        _declare(approval_route),
        _declare(search_claims),
        _declare(create_project, writes=True, permission=PERMISSION_PROJECT_CREATE),
        _declare(record_note, writes=True),
    )
}


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def _typed(name: str, value: Any) -> str:
    """The reason a value is refused, or an empty string when it is fine."""
    if value is None:
        return ""
    wanted = _ARGUMENT_TYPES[name]
    if bool in wanted:
        return "" if isinstance(value, bool) else f"{name} has to be true or false"
    if isinstance(value, bool) or not isinstance(value, wanted):
        kinds = " or ".join(kind.__name__ for kind in wanted)
        return f"{name} has to be {kinds}"
    return ""


def dispatch(
    session: Session,
    *,
    company_id: str,
    actor: User,
    tool: str,
    arguments: dict | None = None,
) -> dict:
    """Run one tool the model chose, with identity the model never sees.

    company_id and actor come from the signed-in session and are injected here.
    ``arguments`` is whatever the model produced and is treated as hostile: it is
    checked for identity names, then against the tool's allowlist, then for
    types, and only then unpacked into the call. Because the allowlist can never
    contain an identity name, the ``**arguments`` below cannot collide with the
    keywords this function supplies.

    An unscoped call or a missing actor RAISES rather than refusing. Neither is
    something a model did; both are the caller failing to inject what only the
    caller has, and a refusal dict would let that ship as a polite message about
    nothing.
    """
    _require_scope(company_id)
    if actor is None:
        raise ValueError(
            "actor is required; the tool layer injects it from the signed-in "
            "session and the model never supplies one"
        )

    name = tool if isinstance(tool, str) else ""

    if getattr(actor, "company_id", None) != company_id:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name or "unknown",
            reason_code=REASON_ACTOR_MISMATCH,
            reason=(
                "The signed-in account does not belong to the company this turn "
                "is scoped to. Nothing was read."
            ),
            halt_turn=True,
        )
    if getattr(actor, "status", None) != STATUS_ACTIVE:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name or "unknown",
            reason_code=REASON_ACTOR_INACTIVE,
            reason="This account is not active, so it reads nothing and writes nothing.",
            halt_turn=True,
        )

    supplied = arguments if isinstance(arguments, dict) else {}
    if arguments is not None and not isinstance(arguments, dict):
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name or "unknown",
            reason_code=REASON_BAD_ARGUMENT,
            reason="The arguments have to be a mapping of names to values.",
        )

    # IDENTITY FIRST, before the tool name is even resolved. An injection aimed
    # at a tool that does not exist is still an injection, and reporting it as a
    # typo would file the attack under the wrong code.
    offending = sorted(key for key in supplied if key in IDENTITY_ARGUMENTS)
    if offending:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name or "unknown",
            reason_code=REASON_IDENTITY_ARGUMENT,
            reason=(
                "This call arrived carrying "
                + ", ".join(offending)
                + ". The tool layer supplies who you are and which company this "
                "is; a call that names them is refused and the attempt is on the "
                "record. Nothing was read or written."
            ),
            action=ACTION_CHAT_IDENTITY_INJECTED,
            halt_turn=True,
        )

    known = REGISTRY.get(name)
    if known is None:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name or "unknown",
            reason_code=REASON_UNKNOWN_TOOL,
            reason=(
                f"There is no tool called {name!r}. The tools are: "
                + ", ".join(sorted(REGISTRY))
                + "."
            ),
        )

    undeclared = sorted(set(supplied) - set(known.arguments))
    if undeclared:
        return _refuse(
            session,
            company_id=company_id,
            actor=actor,
            tool=name,
            reason_code=REASON_UNKNOWN_ARGUMENT,
            reason=(
                f"{name} does not take "
                + ", ".join(undeclared)
                + ". It takes: "
                + (", ".join(sorted(known.arguments)) or "nothing")
                + "."
            ),
        )

    for key, value in supplied.items():
        complaint = _typed(key, value)
        if complaint:
            return _refuse(
                session,
                company_id=company_id,
                actor=actor,
                tool=name,
                reason_code=REASON_BAD_ARGUMENT,
                reason=f"{complaint}. Nothing was read or written.",
            )

    return known.run(session, company_id=company_id, actor=actor, **supplied)


def used_entry(result: dict) -> dict:
    """One row of the wire contract's ``used`` list, built from a tool result.

    Here rather than in the turn handler so that "what was consulted" is derived
    from what the tool actually returned, not from what the caller remembers
    asking for. A refused tool still appears, with nothing found -- a transcript
    that hides the refusals is a transcript of a different conversation.
    """
    return {"tool": result["tool"], "found": result["found"]}


def withheld_total(results: Iterable[dict]) -> int:
    """The turn's withheld count. Every result carries the key, so this cannot
    read an absence as a zero."""
    return sum(result["withheld"] for result in results)


__all__ = [
    "ACTION_CHAT_IDENTITY_INJECTED",
    "ACTION_CHAT_NOTE_RECORDED",
    "ACTION_CHAT_TOOL_REFUSED",
    "IDENTITY_ARGUMENTS",
    "MAX_CHANGE_LIMIT",
    "MAX_ROWS",
    "NOTES_THREAD_LABEL",
    "ORDER_BASES",
    "ORDER_BY_CHAIN",
    "ORDER_BY_CHAIN_THEN_ID",
    "ORDER_BY_ID",
    "PERMISSION_PROJECT_CREATE",
    "REASON_ACTOR_INACTIVE",
    "REASON_ACTOR_MISMATCH",
    "REASON_BAD_ARGUMENT",
    "REASON_IDENTITY_ARGUMENT",
    "REASON_NOT_FOUND",
    "REASON_NOT_PERMITTED",
    "REASON_UNKNOWN_ARGUMENT",
    "REASON_UNKNOWN_TOOL",
    "REGISTRY",
    "Tool",
    "approval_route",
    "change_detail",
    "create_project",
    "dispatch",
    "find_projects",
    "latest_changes",
    "obligation_owner",
    "open_escalations",
    "project_detail",
    "record_note",
    "search_claims",
    "used_entry",
    "withheld_total",
]
