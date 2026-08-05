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
- record_note is gated on knowledge.write, which is the closest code the product
  defines and not the exact one. The missing code is note.write; see record_note
  for what the alias costs and who it shuts out. Adding the exact code means
  editing PERMISSION_CODES in models.py, which is another agent's file, and it
  is in the handoff list.
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

# Retrieval over the docket text itself, as an internal capability and not as a
# feature. See the docstring of app/state/search.py: ADR-002 rejected search as
# the product's wedge and nothing here reverses it. What this import buys is that
# Clarke can reach the PASSAGE a question is about instead of scanning the words
# the product happened to write about it, and every passage it reaches is a
# candidate that still has to pass the citation gate.
from app.state.search import (
    REASON_NO_TERMS,
    SOURCE_INDEX,
    VERSION_NO_MATCH,
    VERSION_NO_PASSAGES,
    VERSION_NOT_SEARCHED,
    CoverageMap,
    Retrieval,
    map_coverage,
    match_terms,
    search_passages,
)
from app.verification.verifier import Citation, occurrence_index, verify_citation

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

# The permissions in front of the writes. Named here so a rename in models.py
# fails at import rather than leaving a gate nobody can pass.
PERMISSION_PROJECT_CREATE = "project.create"

# AN ALIAS, AND IT IS NAMED AS ONE. The exact code for this act would be
# note.write and the product does not define it. knowledge.write -- "Write and
# supersede knowledge items" -- is the nearest thing in PERMISSION_CODES: it is
# the permission for putting the company's own words into the company's own
# record, which is what a note is. It is not a perfect fit and the mismatch is
# worth stating rather than papering over: the grid gives knowledge.write to the
# analyst alone, so an obligation owner who could dictate a note yesterday
# cannot today. That is the conservative side of the error -- an owner is an
# approver, and a surface that lets an approver write into the record without a
# code covering it is the shape this gate exists to refuse. Adding note.write
# means editing PERMISSION_CODES in models.py, which is another agent's file,
# and granting it to both roles; it is in the handoff list.
#
# The alternative was to leave it ungated, which is what it was. An ungated
# write reachable from a chat panel is the wrong shape in a product whose whole
# argument is that every act is gated and recorded -- and the old defence, that
# the same act through the project screen has no gate either, argues that the
# screen is missing a gate rather than that this one should not have one.
PERMISSION_NOTE_WRITE = "knowledge.write"

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

#: How many retrieved passages come back with their text. Far below MAX_ROWS on
#: purpose, and the two numbers do different jobs.
#:
#: Retrieval is asked for MAX_ROWS passages because their OFFSETS decide which
#: claims a question reaches, and an offset costs nothing. Only the top few come
#: back carrying their words, because a passage is up to MAX_EXCERPT characters
#: and sixteen of them is ten thousand characters of source text spent on a
#: question the analyst asked in six words. That is the context budget in
#: best-practices.html principle 21, and the real corpus reaches sixteen on an
#: ordinary two-word question -- measured, not feared.
#:
#: What the cap leaves out is counted into ``omitted`` and said in the note,
#: like every other cap in this file.
MAX_CANDIDATE_PASSAGES = 5

#: How many passages retrieval is asked for. Generous, because these are the
#: offsets that decide which claims a question reaches and each one costs a row.
#: "Utility shall" matches 43 passages of the seeded corpus and a one-word
#: question can match more, so a cap at MAX_ROWS would have quietly stopped
#: widening a third of the way through an ordinary question.
#:
#: It is a cap and not "everything", because a question must not be able to pull
#: a whole corpus into one turn. When it bites, the note says a claim cited
#: outside the passages that were used may be missing -- see _retrieval_note.
MAX_RETRIEVED_PASSAGES = 100

#: Retrieval did not run at all. Kept apart from the reasons app/state/search.py
#: returns, because every one of those describes an index that could not be
#: trusted and this describes a search that never happened -- a scope with
#: nothing in it. Folding them would make "the index is broken" and "you asked
#: about a project that does not exist" the same sentence.
RETRIEVAL_NOT_RUN = "RETRIEVAL_NOT_RUN"

#: The fourth kind of empty span list, and the only one this layer can produce.
#:
#: app/state/search.py names three -- searched and nothing matched, no passages
#: to search, never searched -- and it cannot name this one, because it knows
#: nothing about claims. This is a filing whose passages DID match and whose
#: spans were then not handed over: they carry the cited span of a withheld
#: claim, or they would not verify against the stored source.
#:
#: It is a separate code rather than a zero because reporting it as "nothing
#: matched" would be a false sentence built out of a true refusal -- the product
#: declining to repeat text, rendered as the filing having nothing to say. The
#: two counts that caused it travel in the same row, so a reader can tell a
#: policy from a defect without another call.
VERSION_NOT_OFFERED = "MATCHED_BUT_NOT_OFFERED"

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

    Deliberately dumb, and still the right rule for the two things it is asked
    about: a project's name and a claim's own statement. Both are short strings
    somebody wrote; there is nothing in them to rank, and a ranked list of claims
    would be a judgement about which one answers the question, which is the
    analyst's call and not the clerk's.

    WHAT IT IS NO LONGER ASKED TO DO. It used to be the only way search_claims
    could reach anything, so a claim was findable only by the words the product
    had written about it. Ask about a distribution system implementation plan and
    a claim reading "the plan is now due within sixty days" was invisible, though
    the passage it cites carries every word of the question. The docket text is
    searched through app/state/search.py now, and this stays as the test on the
    statement itself.
    """
    words = (query or "").lower().split()
    if not words:
        return True
    haystack = (text or "").lower()
    return all(word in haystack for word in words)


# ---------------------------------------------------------------------------
# Retrieval, folded into the claim gate rather than placed beside it
# ---------------------------------------------------------------------------


def _spans_by_version(retrieval: Retrieval) -> dict[str, list[tuple[int, int]]]:
    """Where the retrieved passages sit, keyed by the version they sit in."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for hit in retrieval.hits:
        spans.setdefault(hit.version_id, []).append((hit.char_start, hit.char_end))
    return spans


def _overlaps(
    version_id: str, start: int, end: int, spans: dict[str, list[tuple[int, int]]]
) -> bool:
    """Does this citation sit inside any retrieved passage of the same version?

    Overlap rather than containment. A citation can straddle a passage boundary
    -- the segmenter splits on blank lines and a quoted sentence can run past one
    -- and demanding containment would drop exactly the claims whose evidence
    spans two paragraphs, silently.

    THE VERSION HAS TO AGREE, not just the offsets. Two versions of one
    proceeding are mostly the same words at mostly the same places, so a span
    check that ignored the version id would match a claim cited to the draft
    against a passage retrieved from the final order. That is the confusion
    ADR-005 exists to prevent, and it would arrive here through the back door.
    """
    for hit_start, hit_end in spans.get(version_id, ()):
        if start < hit_end and hit_start < end:
            return True
    return False


def _candidate_payload(hit, source_text: str) -> dict | None:
    """A retrieved passage in wire form, or None if it will not verify.

    THE GATE RUNS ON A CANDIDATE TOO, and that is the point of the whole design.
    A hit arrives with a version id, two offsets and the text at them, which is
    a citation in everything but name -- so it is built into one and put through
    verify_citation, the same function every rendered claim goes through. A
    passage that will not verify is not offered, because offering it would put a
    span in front of the model that the product could not stand behind, and a
    good bm25 score is not evidence of anything.

    The occurrence is computed rather than left None. A claim that quotes
    repeated boilerplate is ambiguous and must say which copy it means; a
    retrieved passage has already said, by its offsets. Passing the occurrence
    the offsets name keeps the check honest -- an offset that does not land on
    an occurrence at all still fails -- without withholding every candidate that
    happens to sit in a repeated paragraph.
    """
    citation = Citation(
        version_id=hit.version_id,
        char_start=hit.char_start,
        char_end=hit.char_end,
        quoted_text=hit.text,
    )
    result = verify_citation(
        citation, source_text, occurrence_index(citation, source_text)
    )
    if not result.verified:
        return None

    excerpt, truncated = _excerpt(source_text, hit.char_start, hit.char_end)
    return {
        "version_id": hit.version_id,
        "section": hit.section,
        "start": hit.char_start,
        "end": hit.char_end,
        "text": excerpt,
        "truncated": truncated,
        # bm25, where lower is better. Carried so a reader can see the ordering
        # was computed rather than chosen, and named `rank` rather than `score`
        # so nobody reads it as a confidence.
        "rank": hit.rank,
        # THE KEY THAT STOPS A RANK BECOMING A VERDICT. It is False on every
        # candidate, always, and it is written here rather than left out because
        # a missing key reads as "not applicable" and a present False reads as
        # "asked and answered no".
        "is_evidence": False,
    }


def _candidates(
    session: Session,
    company_id: str,
    retrieval: Retrieval | None,
    held_spans: dict[str, list[tuple[int, int]]],
) -> tuple[list[dict], int, int]:
    """Retrieved passages worth handing over, and counts of the two kinds dropped.

    Returns (candidates, dropped for carrying a withheld claim's span, dropped
    for failing the verifier). Both counts are returned rather than folded into
    one, because they are different facts: the first is the product declining to
    repeat text beside a refusal, and the second is the stored passage
    disagreeing with the stored source, which is a defect somebody has to look
    at. A single number would let a bug read as a policy.

    The two drops happen BEFORE the MAX_CANDIDATE_PASSAGES cap, not after, so a
    passage the product refuses to show can never be the reason a passage it
    would have shown falls off the end of the list.
    """
    if retrieval is None or not retrieval.hits:
        return [], 0, 0

    sources = _source_by_version(session, company_id)
    candidates: list[dict] = []
    for_withholding = 0
    unverifiable = 0

    for hit in retrieval.hits:
        source_text = sources.get(hit.version_id)
        if source_text is None:
            # The version is not one this company can read. Reachable only
            # through a scoping bug upstream, and the answer to a scoping bug is
            # to drop the row, not to render it.
            unverifiable += 1
            continue
        if _overlaps(hit.version_id, hit.char_start, hit.char_end, held_spans):
            for_withholding += 1
            continue
        payload = _candidate_payload(hit, source_text)
        if payload is None:
            unverifiable += 1
            continue
        candidates.append(payload)

    return candidates, for_withholding, unverifiable


def _retrieval_payload(
    retrieval: Retrieval | CoverageMap | None,
    withheld_spans: int = 0,
    unverifiable: int = 0,
    *,
    not_run: str = RETRIEVAL_NOT_RUN,
    found: int | None = None,
) -> dict:
    """Which path answered, why, and what it searched for. Present on every result.

    Always the same keys, never a missing one. A caller reading `source` to find
    out whether an answer was ranked must not have to tell "the index answered"
    apart from "somebody forgot the key", and a turn that ran no retrieval at
    all is a fact worth carrying rather than an absence to interpret.

    ``not_run`` is the reason for the second of those, and the caller names it
    rather than this function guessing. There are two ways to reach it and they
    are different facts: a question with no searchable words in it, and a scope
    that holds nothing to search.

    IT TAKES EITHER READ, and ``found`` is how. Retrieval and CoverageMap agree
    on source, reason and terms and disagree on what they hold underneath -- a
    flat tuple of hits against a row per filing -- so the count comes from the
    caller when the shape is not a flat one. A second payload builder for the
    coverage map was the obvious alternative and it is the wrong one: these keys
    are read by the same transcript code, and two builders is two chances for
    one of them to stop writing a key the other still writes.
    """
    if retrieval is None:
        return {
            "source": "",
            "reason": not_run,
            "ranked": False,
            "terms": [],
            "found": 0,
            "withheld_spans": 0,
            "unverifiable": 0,
        }
    if found is None:
        hits = getattr(retrieval, "hits", None)
        if hits is None:
            # LOUD AT THE CALL SITE BEATS A ZERO IN THE TRANSCRIPT. A read with no
            # flat hit list -- the coverage map has rows per filing instead --
            # cannot have its count guessed here, and the plausible guess is 0,
            # which would report a search that found nothing. Both callers pass
            # `found` today; this is for the third.
            raise ValueError(
                f"{type(retrieval).__name__} has no flat list of hits, so `found` "
                "has to be supplied. Guessing would report a search that found "
                "nothing."
            )
        found = len(hits)
    return {
        "source": retrieval.source,
        "reason": retrieval.reason,
        "ranked": retrieval.source == SOURCE_INDEX,
        "terms": list(retrieval.terms),
        "found": found,
        "withheld_spans": withheld_spans,
        "unverifiable": unverifiable,
    }


def _retrieval_note(
    retrieval: Retrieval | None,
    candidates: list[dict],
    withheld_spans: int,
    unverifiable: int,
) -> str:
    """The sentences a person reads about how the passages were found.

    Every degraded path says so here, in the note the model is handed and the
    transcript keeps, and not only in a log nobody opens. A fallback that
    announces itself to the operator and not to the reader has announced itself
    to the wrong person -- best-practices.html section 26.
    """
    if retrieval is None:
        return ""

    parts: list[str] = []
    if retrieval.reason:
        parts.append(f" {retrieval.note}")
    if retrieval.omitted:
        # NOT a number, and not folded into ``omitted`` either. On the index
        # path the true remainder is unknown -- one row beyond the limit was
        # fetched, which says there are more and not how many -- and printing a
        # 1 that means "at least one" would be a precise-looking invention. It
        # is not an ``omitted`` row because nothing was cut from a list here:
        # these are passages that never got as far as widening the search, so
        # what they cost is a claim that may be absent rather than a row that
        # is. Different loss, different sentence.
        parts.append(
            " More passages matched than were used to look for claims, so a "
            "claim cited outside them will not appear above. Narrow the "
            "question to be sure of the ones that matter."
        )
    if candidates:
        word = "passage" if len(candidates) == 1 else "passages"
        parts.append(
            f" {len(candidates)} source {word} came back as a candidate, not as "
            "evidence: a passage using the question's words is where to look, "
            "and only a claim whose citation verifies may be asserted."
        )
    if withheld_spans:
        # THE SINGULAR CASE IS THE COMMON ONE, so it is the one worth reading.
        # This said "1 matching passage are not shown because they carry" until
        # the coverage map hit the same slip and it was fixed in both places at
        # once. A note assembled out of a count is one a reader stops trusting
        # the moment its grammar shows the seam, and this note is the sentence
        # that explains a refusal.
        parts.append(
            f" {withheld_spans} matching "
            f"{_agree(withheld_spans, 'passage', 'passages')} "
            f"{_agree(withheld_spans, 'is', 'are')} not shown because "
            f"{_agree(withheld_spans, 'it carries', 'they carry')} the span of a "
            "claim that was withheld. The reason is in the review queue; the "
            "words are in the citation viewer, beside what the source actually "
            "says."
        )
    if unverifiable:
        parts.append(
            f" {unverifiable} matching "
            f"{_agree(unverifiable, 'passage', 'passages')} did not verify against "
            f"the stored source and {_agree(unverifiable, 'was', 'were')} dropped. "
            "That is a defect in the stored corpus rather than an answer; it "
            "belongs in the review queue."
        )
    return "".join(parts)


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


def _agree(count: int, singular: str, plural: str) -> str:
    """The word that agrees with a count. For sentences built out of five of them.

    The rest of this file writes the ternary inline, and at one or two per note
    that is the right call. The coverage map's note counts five different things
    in one paragraph, and ten inline ternaries would bury the sentence being
    composed. A count of one reading "1 filings have" is the kind of seam that
    tells a reader the sentence was assembled rather than written, and this
    paragraph is one a person is meant to trust.
    """
    return singular if count == 1 else plural


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
    """Claims whose citation VERIFIES, reached by their own words or by the text they cite.

    A WITHHELD CLAIM CANNOT BE SEARCHED, AND THAT IS THE DESIGN. Matching one
    would mean holding its statement long enough to compare it against the query,
    and the statement is not available here -- app/state/claims.py never hands it
    back. So every withheld claim in the searched scope is reported by count and
    by reason, whatever the query was. That is the honest answer to "is there
    anything about X": there may be, and here is what is stopping the product
    from saying so, and where to go and clear it.

    TWO WAYS IN, AND THE SECOND IS WHY THE INDEX EXISTS. A claim matches if its
    own statement carries the query's words, or if the passage it cites does.
    Only the first was ever possible before, and it made the product findable
    only through the sentences it had already written: ask about a distribution
    system implementation plan and a claim reading "the plan is now due within
    sixty days" was invisible, though the paragraph it cites carries every word
    of the question. The docket text is searched through app/state/search.py.

    A RANK IS NOT A VERDICT, and this function is where that has to be true
    rather than merely said. Retrieval decides which claims are LOOKED AT. What
    may be asserted is decided afterwards, by verified_claims(), exactly as
    before -- so a passage that tops the ranking and carries a claim whose
    citation does not verify still yields a withheld count and no statement. The
    candidate passages returned beside the claims are put through the same
    verifier and carry is_evidence: False, because a span the model can read is
    one paraphrase away from a span the model will assert.

    AND ONE NARROWING ON TOP. A retrieved passage that carries the cited span of
    a WITHHELD claim is not handed over. _withheld_payload already refuses to
    send that claim's source excerpt to a model, on the argument that a sentence
    built around the words a failed citation quoted does the harm the refusal
    exists to prevent. Retrieval must not be the second door into the same text
    because the paragraph happened to rank well on its own. The count of what
    was dropped that way is reported; a silent drop would be the failure this
    file names in its own docstring.

    THE COST, PLAINLY. Verification re-runs against the stored source for every
    claim under every change in scope, and app/state/claims.py re-reads the
    company's sources once per change while doing it. On a corpus of dozens that
    is nothing. On thousands it is the first thing to fix, and the fix belongs in
    claims.py -- a verified_claims that takes a prepared source map -- not in a
    cache here, because a cached verdict is the stored verdict ADR-003 refuses.
    Retrieval adds one indexed read, or one whole-corpus read when the index
    cannot be trusted; which of the two happened is in the ``retrieval`` block
    of every result.
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
                candidate_passages=[],
                retrieval=_retrieval_payload(None, not_run=RETRIEVAL_NOT_RUN),
            )
        changes = changes_for_project(session, company_id, project_id)
        scope_note = f"Searched the changes attached to {project_id}."
    else:
        changes = _changes_for_company(session, company_id)
        scope_note = "Searched every change on this company's record."

    # THE QUESTION HAS TO HOLD A WORD BEFORE ANYTHING IS RANKED. An empty query
    # means "everything in scope" to the statement test above, and everything is
    # not a ranking -- retrieving the whole corpus as candidates would hand the
    # model a document dump wearing the clothes of an answer. So retrieval is
    # skipped, and the result says it was skipped rather than reporting nothing
    # found.
    retrieval = None
    if match_terms(query):
        retrieval = search_passages(
            session,
            company_id=company_id,
            query=query,
            limit=MAX_RETRIEVED_PASSAGES,
        )
    spans = _spans_by_version(retrieval) if retrieval else {}

    escalations = _escalation_by_claim(session, company_id)
    hits: list[dict] = []
    held: list[dict] = []
    held_spans: dict[str, list[tuple[int, int]]] = {}
    for change in changes:
        verified, withheld = verified_claims(session, company_id, change.id)
        hits.extend(
            _verified_payload(claim, change.id)
            for claim in verified
            if _matches(claim.statement, query)
            or _overlaps(
                claim.citation_version_id,
                claim.citation_start,
                claim.citation_end,
                spans,
            )
        )
        held.extend(
            _withheld_payload(claim, change.id, escalations.get(claim.claim_id))
            for claim in withheld
        )
        for claim in withheld:
            held_spans.setdefault(claim.citation_version_id, []).append(
                (claim.citation_start, claim.citation_end)
            )

    candidates, dropped_for_withholding, unverifiable = _candidates(
        session, company_id, retrieval, held_spans
    )
    candidates, candidates_omitted = _cap(candidates, MAX_CANDIDATE_PASSAGES)

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
    note += _retrieval_note(retrieval, candidates, dropped_for_withholding, unverifiable)
    note += _omission_note(omitted + held_omitted + candidates_omitted)

    return _result(
        "search_claims",
        found=len(shown),
        withheld=len(held),
        omitted=omitted + held_omitted + candidates_omitted,
        note=note.strip(),
        claims=shown,
        withheld_claims=held_shown,
        candidate_passages=candidates,
        retrieval=_retrieval_payload(
            retrieval,
            dropped_for_withholding,
            unverifiable,
            not_run=REASON_NO_TERMS,
        ),
    )


def _withheld_spans_for_company(
    session: Session, company_id: str
) -> tuple[dict[str, list[tuple[int, int]]], int]:
    """Where every withheld claim in this company cites, and how many there are.

    WHY COMPANY-WIDE RATHER THAN NARROWED TO THE DOCKET BEING MAPPED. A claim is
    attached to a change, a change to a proceeding, and reaching from a docket to
    its changes is another join to get right. A superset of the withheld spans
    drops MORE passages and never fewer, which is the safe direction for a rule
    about not repeating text the product has already refused to assert -- and the
    spans carry a version id, so a span from another docket cannot match a
    passage in this one anyway. It costs a wider read, not a wider answer.

    NOT CALLED FROM search_claims, ON PURPOSE. That tool needs the verified and
    the withheld claims from one pass over the same changes, and calling this
    would run verified_claims a second time over the whole scope. The spans are
    built the same way in both places and this is the shape to copy if a third
    caller appears; a third inline loop is not.

    WHERE THIS FAILS, AND IN WHICH DIRECTION, SAID PLAINLY BECAUSE IT IS THE
    WRONG DIRECTION. _changes_for_company reaches changes through their
    proceeding, which is deliberate in app/state/claims.py -- the join is what
    catches a change stamped with the wrong company. The cost is that a change
    whose proceeding row is absent is invisible to it, so a withheld claim under
    that change contributes no span, and this function would hand over the very
    passage the withholding rule exists to hold back. Nothing would say so: the
    count would read zero because zero were found.

    It is not reachable through this product today -- no path in app/ deletes a
    Proceeding, and SQLite is not running with foreign keys enforced, which is
    what would otherwise stop the row going away. So this is a concession about
    the direction of failure rather than a live defect: a guard about not
    repeating refused text degrades to repeating it. Closing it properly means a
    claims read that does not depend on the parent existing, which is
    app/state/claims.py's call to make, and it is in the handoff list.
    """
    _require_scope(company_id)
    spans: dict[str, list[tuple[int, int]]] = {}
    total = 0
    for change in _changes_for_company(session, company_id):
        _, withheld = verified_claims(session, company_id, change.id)
        for claim in withheld:
            total += 1
            spans.setdefault(claim.citation_version_id, []).append(
                (claim.citation_start, claim.citation_end)
            )
    return spans, total


def coverage_map(
    session: Session,
    *,
    company_id: str,
    actor: User,
    query: str = "",
    docket: str | None = None,
) -> dict:
    """Whether each filing in scope carries these words, and which ones do not.

    THE QUESTION A RANKED LIST CANNOT ANSWER. search_claims hands back the best
    passages for a question, ranked across the whole corpus, capped at
    MAX_CANDIDATE_PASSAGES. Ask it about curtailment across eight filings and all
    five candidates can come from the one filing that mentions it most -- and
    nothing in the answer distinguishes "the other seven do not mention it" from
    "the other seven lost on rank". This tool returns a row for EVERY filing in
    scope, with its own span budget, so that the second half of the answer -- the
    silence -- is reported rather than implied.

    THE SILENCE IS THE POINT. If seven filings address a subject and the eighth
    does not, the eighth is the interesting one, and a result that leaves it out
    is indistinguishable from one where it ranked ninth. So a filing with nothing
    to offer comes back as a present, explicit row carrying its own reason.
    app/state/search.py::refuse_a_short_map makes that an invariant rather than a
    habit: a map missing a filing raises instead of answering.

    ``found`` COUNTS THE FILINGS REPORTED, NOT THE PASSAGES, and that is the only
    honest choice here. A filing that says nothing is a row this tool exists to
    return, so counting only the ones that matched would make the half that
    matters invisible in every count the transcript keeps -- ``used`` in the wire
    contract reads this number.

    FOUR EMPTY SPAN LISTS, FOUR DIFFERENT FACTS, AND THEY ARE NEVER FOLDED. Three
    codes come from the retrieval layer -- searched and nothing matched, no
    stored passages to search, never searched -- and the fourth is
    VERSION_NOT_OFFERED, which is this layer's: the filing matched and its spans
    were then not handed over. Every row carries the code, the sentence, and the
    two drop counts that produced it.

    WHAT A ZERO DOES NOT MEAN, said in the wire as well as here. "No passage in
    this filing matches these words" is what the index can support. "This filing
    does not address the subject" is a judgement no deterministic read may make,
    and the note beside every zero says so, because the note is handed to a model
    that will paraphrase it.

    EVERY SPAN GOES THROUGH THE CITATION GATE, through _candidate_payload and so
    through verify_citation -- the same function every rendered claim goes
    through. A span that will not verify is not offered, is counted, and turns
    its filing's row into VERSION_NOT_OFFERED rather than into a silence. And a
    passage carrying the cited span of a WITHHELD claim is still not handed over
    here: this tool must not become a second door into text the product has
    already refused to assert because the paragraph ranked well on its own.

    NO MODEL RUNS IN ANY OF THIS. Every number in the result is a query, a
    comparison or a count, which is what makes the claim it makes checkable by
    hand against the corpus.

    THE COST, PLAINLY. One query per filing on the index path, one whole-corpus
    read on the degraded one, and a verification pass over every claim in the
    company to find the withheld spans. On this corpus that is nothing. On
    thousands it is the same fix search_claims needs -- a verified_claims that
    takes a prepared source map -- and not a cache, because a cached verdict is
    the stored verdict ADR-003 refuses.
    """
    _require_scope(company_id)

    # A BLANK DOCKET FROM A MODEL MEANS "I DID NOT NAME ONE", AND THE QUERY LAYER
    # REFUSES IT. Both are right at their own level. versions_for_company refuses
    # "" because a docket a caller COMPUTED and got blank means they meant one and
    # found none, and answering the whole estate would be a widening nobody
    # asked for. Here the blank came from a model filling a field it had no value
    # for, which _ARGUMENT_TYPES already says is not worth a refusal about JSON.
    # So it is folded to None once, at the boundary, and the note says which
    # scope actually answered -- a widening that announces itself is a different
    # thing from one that does not. Without this fold the ValueError travels up
    # as a stack trace instead of a sentence.
    docket = (docket or "").strip() or None

    coverage = map_coverage(
        session, company_id=company_id, query=query, docket=docket
    )

    if not coverage.versions:
        # A scope with no filings in it. Not a map of zeroes, and the sentence
        # has to say which of the two it is, or "that docket is not on your
        # record" reads as "none of those filings mention it".
        where = f"docket {docket!r}" if docket else "this company's record"
        return _result(
            "coverage_map",
            found=0,
            note=(
                f"There are no filings under {where}, so there was nothing to "
                "search. That is a fact about the scope and not about any filing."
            ),
            versions=[],
            in_scope=0,
            searched=0,
            covered=0,
            silent=0,
            no_passages=0,
            not_searched=0,
            not_offered=0,
            retrieval=_retrieval_payload(coverage, found=0),
        )

    # Skipped when nothing was searched -- an empty question or an empty scope --
    # because it is a verification pass over every claim in the company and there
    # is no span for it to guard. The count it produces is reported only when it
    # was actually taken; a zero printed without counting is the silent count
    # this module refuses everywhere else.
    held_spans: dict[str, list[tuple[int, int]]] = {}
    withheld_claims = 0
    if coverage.searched:
        held_spans, withheld_claims = _withheld_spans_for_company(session, company_id)

    sources = _source_by_version(session, company_id)
    rows: list[dict] = []
    total_withheld_spans = 0
    total_unverifiable = 0

    for row in coverage.versions:
        source_text = sources.get(row.version_id)
        spans: list[dict] = []
        withheld_here = 0
        unverifiable_here = 0
        for hit in row.hits:
            if source_text is None:
                # The version is not one this company can read. Reachable only
                # through a scoping bug upstream, and the answer to a scoping bug
                # is to drop the span, not to render it.
                unverifiable_here += 1
                continue
            if _overlaps(hit.version_id, hit.char_start, hit.char_end, held_spans):
                withheld_here += 1
                continue
            payload = _candidate_payload(hit, source_text)
            if payload is None:
                unverifiable_here += 1
                continue
            spans.append(payload)

        total_withheld_spans += withheld_here
        total_unverifiable += unverifiable_here

        reason, note = row.reason, row.note
        if row.hits and not spans:
            # It matched. What it matched was not ours to hand over. Saying
            # "nothing matched" here would be a false sentence assembled out of
            # a true refusal.
            reason = VERSION_NOT_OFFERED
            # NO COUNT OF WHAT MATCHED, BECAUSE THIS DOES NOT KNOW ONE. row.hits
            # is capped at MAX_SPANS_PER_VERSION, so a filing with eight matching
            # passages arrives here with two -- and "2 passages in this filing
            # match those words" would be a precise-looking invention,
            # contradicted by the `more` flag on the same row. The counts that
            # follow are of the passages actually examined, which is a number
            # this does know, and they are labelled as such.
            note = (
                "This filing carries passages matching those words and none of "
                "the ones examined here is shown. "
                + (
                    f"{withheld_here} "
                    f"{_agree(withheld_here, 'carries', 'carry')} the cited span "
                    "of a claim that was withheld: the reason is in the review "
                    "queue and the words are in the citation viewer, beside what "
                    "the source actually says. "
                    if withheld_here
                    else ""
                )
                + (
                    f"{unverifiable_here} did not verify against the stored "
                    "source, which is a defect in the corpus rather than an "
                    "answer. "
                    if unverifiable_here
                    else ""
                )
            ).strip()

        elif spans and (withheld_here or unverifiable_here):
            # A ROW THAT LOST SOME OF ITS SPANS STILL HAS TO SAY SO IN ITS OWN
            # SENTENCE. The drop is already in the two numeric fields and in the
            # note at the top of the result, so nothing here was false -- but the
            # per-row note is the string a renderer is most likely to show on its
            # own, and beside two spans it read as though two were all there was.
            note = (
                "Some passages matching those words in this filing are not shown: "
                + (
                    f"{withheld_here} "
                    f"{_agree(withheld_here, 'carries', 'carry')} the cited span "
                    "of a claim that was withheld. "
                    if withheld_here
                    else ""
                )
                + (
                    f"{unverifiable_here} did not verify against the stored "
                    "source. "
                    if unverifiable_here
                    else ""
                )
            ).strip()

        rows.append(
            {
                "version_id": row.version_id,
                "label": row.label,
                "status": row.status,
                "docket": row.docket,
                "spans": spans,
                "reason": reason,
                "note": note,
                # A bool rather than a count: the index path knows only THAT
                # there are more. app/state/search.py::VersionCoverage.more says
                # why at length.
                "more": row.more,
                "withheld_spans": withheld_here,
                "unverifiable": unverifiable_here,
            }
        )

    covered = sum(1 for row in rows if row["spans"])
    silent = sum(1 for row in rows if row["reason"] == VERSION_NO_MATCH)
    no_passages = sum(1 for row in rows if row["reason"] == VERSION_NO_PASSAGES)
    not_searched = sum(1 for row in rows if row["reason"] == VERSION_NOT_SEARCHED)
    not_offered = sum(1 for row in rows if row["reason"] == VERSION_NOT_OFFERED)

    where = f" for docket {docket}" if docket else " on this company's record"
    scope_sentence = (
        f"{coverage.in_scope} {_agree(coverage.in_scope, 'filing', 'filings')} in "
        f"scope{where}."
    )

    if not coverage.searched:
        # NOTHING WAS SEARCHED, SO THE COUNTS MAY NOT BE READ OUT. Every one of
        # them is zero here, and "0 carry a passage matching those words" is a
        # sentence about a search that did not happen -- the exact blur this
        # whole read exists to refuse, arriving in the first line of the
        # paragraph the model paraphrases. The scope is still a fact and is still
        # reported; what follows it is search.py's own reason for why no filing
        # was looked in.
        # The counts are read off the rows here as they are below, never written
        # as literal zeroes. They are all zero on this branch today; a literal
        # would keep saying so on the day that stops being true.
        return _result(
            "coverage_map",
            found=len(rows),
            withheld=withheld_claims,
            # Zero for the same reason as the branch below: every row is here.
            omitted=0,
            note=(
                f"{scope_sentence} None of them was searched, so nothing is known "
                f"about whether any carries those words. {coverage.note}"
            ).strip(),
            versions=rows,
            in_scope=coverage.in_scope,
            searched=coverage.searched,
            covered=covered,
            silent=silent,
            no_passages=no_passages,
            not_searched=not_searched,
            not_offered=not_offered,
            retrieval=_retrieval_payload(coverage, found=0),
        )

    # WHAT CARRIES A MATCHING PASSAGE AND WHAT IS SHOWN ARE DIFFERENT NUMBERS,
    # and the opening sentence is about the first. `covered` counts filings whose
    # spans SURVIVED the citation gate and the withholding rule; a filing whose
    # matches were all refused still carries them. Saying "2 carry a passage
    # matching those words" where three do put the undercount in the first line
    # of the paragraph a model paraphrases, with the correction two sentences
    # later -- and in the one-filing case it read "0 carry a passage matching
    # those words" about a docket that plainly does.
    matching = covered + not_offered
    note = (
        f"{scope_sentence} {matching} {_agree(matching, 'carries', 'carry')} a "
        f"passage matching those words; {silent} "
        f"{_agree(silent, 'was', 'were')} searched and "
        f"{_agree(silent, 'carries', 'carry')} none."
    )
    if silent:
        note += (
            " A filing carrying none of the words is not a filing that does not "
            "address the subject; it is one that does not use them."
        )
    if no_passages:
        note += (
            f" {no_passages} {_agree(no_passages, 'has', 'have')} no stored "
            f"passages at all, so nothing in {_agree(no_passages, 'it', 'them')} "
            "was searched and nothing follows about what "
            f"{_agree(no_passages, 'it holds', 'they hold')}."
        )
    if not_searched:
        note += (
            f" {not_searched} {_agree(not_searched, 'was', 'were')} not searched, "
            f"so nothing is known about {_agree(not_searched, 'it', 'them')} "
            "either way. Narrow the question to a docket to reach the rest."
        )
    if not_offered:
        note += (
            f" Of those, {not_offered} {_agree(not_offered, 'is', 'are')} not "
            "shown here at all; the row for each says which of the two reasons "
            "applies."
        )
    if covered:
        note += (
            " Every passage here is a candidate and not evidence: a filing using "
            "the words is where to look, and only a claim whose citation verifies "
            "may be asserted."
        )
    if withheld_claims:
        note += (
            f" {withheld_claims} {_agree(withheld_claims, 'claim', 'claims')} on "
            f"this company's record {_agree(withheld_claims, 'is', 'are')} "
            "withheld and could not be read at all, so "
            f"{_agree(withheld_claims, 'it', 'one of them')} may bear on this "
            "question."
        )
    if coverage.reason:
        # The degraded path announces itself in the transcript the person reads,
        # not only in a log nobody opens -- best-practices.html section 26. The
        # sentence is search.py's own; a second wording of the same fallback is a
        # second answer to what went wrong.
        note += f" {coverage.note}"
    if total_withheld_spans:
        note += (
            f" {total_withheld_spans} matching "
            f"{_agree(total_withheld_spans, 'passage', 'passages')} "
            f"{_agree(total_withheld_spans, 'is', 'are')} not shown because "
            f"{_agree(total_withheld_spans, 'it carries', 'they carry')} the span "
            "of a claim that was withheld."
        )
    if total_unverifiable:
        note += (
            f" {total_unverifiable} matching "
            f"{_agree(total_unverifiable, 'passage', 'passages')} did not verify "
            f"against the stored source and {_agree(total_unverifiable, 'was', 'were')} "
            "dropped. That is a defect in the stored corpus rather than an "
            "answer; it belongs in the review queue."
        )

    return _result(
        "coverage_map",
        found=len(rows),
        withheld=withheld_claims,
        # ZERO, ALWAYS, AND THAT IS THE PROPERTY THIS TOOL EXISTS FOR. ``omitted``
        # means "rows a length cap left out of this list" on every other tool in
        # this file -- _omission_note spells it "N more rows not shown" -- and
        # this list never cuts a row. The version cap limits what is SEARCHED,
        # not what is REPORTED: a filing past it comes back as a row saying it
        # was not searched, so it is present and counted in ``found``. It was
        # briefly set to the not-searched count, which told a reader that rows
        # were missing while handing them every one of them.
        #
        # What a per-filing span cap left out is carried on the row itself as
        # ``more``, because the index path knows only that there is more.
        omitted=0,
        note=note.strip(),
        versions=rows,
        in_scope=coverage.in_scope,
        searched=coverage.searched,
        covered=covered,
        silent=silent,
        no_passages=no_passages,
        not_searched=not_searched,
        not_offered=not_offered,
        retrieval=_retrieval_payload(
            coverage,
            total_withheld_spans,
            total_unverifiable,
            found=sum(len(row["spans"]) for row in rows),
        ),
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

    GATED ON knowledge.write, WHICH IS AN ALIAS AND IS LABELLED AS ONE. This
    write ran on no permission code at all, and the argument for that was the
    one directly above: the product defines no note.write, so rather than pick a
    near-miss the gate was left out. That was the wrong way round. An ungated
    write the assistant can call from a chat panel is the shape this product
    exists to refuse -- create_project sits beside it and is gated, and the
    difference between them was an accident of which code happened to exist.
    Ungated meant any signed-in person, holding no role whatever, could fill any
    project on their company's record with text a model composed.

    knowledge.write -- "Write and supersede knowledge items" -- is the nearest
    code the grid defines, and it is not the same thing. What the alias costs is
    written at PERMISSION_NOTE_WRITE above rather than left for somebody to
    discover: the analyst holds it, the obligation owner does not, so an owner
    can no longer dictate a note here. That is the conservative side of the
    error and it is the side to be on. note.write is the handoff.

    THE GATE RUNS FIRST, before the text and the project id are looked at. A
    person who may not write a note has to be told that, not told their note was
    empty: a gate discoverable by argument turns a refusal into a puzzle, and it
    lets somebody enumerate which project ids exist before being stopped.

    THE GATE IS NOT RE-IMPLEMENTED AND NOT CONSULTED TWICE. policy.require
    refuses, writes the access.denied row itself, and raises; this catches the
    raise and turns it into a refusal the model can explain -- the same shape
    create_project uses, for the same reason.

    THE NOTE TEXT IS NOT COPIED INTO THE AUDIT CHAIN. The chain is append-only
    and cannot be redacted, and a note is free text a person dictated. The row
    names the turn; the turn holds the words, once.
    """
    _require_scope(company_id)
    try:
        policy.require(
            session, company_id, getattr(actor, "id", ""), PERMISSION_NOTE_WRITE
        )
    except policy.PermissionDenied as denied:
        # require() already wrote the refusal. A second row would double-count
        # one decision.
        return _refusal(
            "record_note",
            reason_code=REASON_NOT_PERMITTED,
            reason=(
                f"You do not hold {PERMISSION_NOTE_WRITE}, so I cannot record a "
                f"note on your company's record. {denied.reason}. An admin "
                "grants it."
            ),
        )

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
        _declare(coverage_map),
        _declare(create_project, writes=True, permission=PERMISSION_PROJECT_CREATE),
        _declare(record_note, writes=True, permission=PERMISSION_NOTE_WRITE),
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
    "PERMISSION_NOTE_WRITE",
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
    "RETRIEVAL_NOT_RUN",
    "VERSION_NOT_OFFERED",
    "Tool",
    "approval_route",
    "change_detail",
    "coverage_map",
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
