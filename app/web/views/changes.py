"""The change screen: what moved, and what the product will say about it.

This is where ADR-003 becomes something a reviewer can see. The screen reads one
change and renders two lists that come back from `verified_claims()`: the claims
whose citations were re-checked against the stored source a moment ago, and the
claims the product refuses to make. They are iterated separately and there is no
flag to branch on, because `WithheldClaim` has no `statement` field and, being
slotted, cannot be given one. A template mistake here cannot leak an assertion;
it can only fail to render.

The citation viewer is server-rendered. Every verified claim's source extract is
already in the page when it arrives, with the cited characters wrapped in a
mark. app/web/static/citation.js only toggles visibility and moves focus. That
ordering is deliberate: the evidence for a claim must not depend on a script
running, so the panels ship open and the script closes them, never the reverse.

THIS IS ALSO THE ONE CALL SITE OF THE ONE MODEL JUDGEMENT IN THE PRODUCT. The
screen asks app/interpretation/propose.py whether this change is material, and
the answer goes through the same verifier every claim goes through. When the
model's citation does not verify, the verdict is withheld and the reason is
printed where the verdict would have gone -- so a reviewer can watch the gate
refuse the model's own output on the same screen it refuses a stored claim's.
That is the hardest decision in this product made visible rather than described.

THE CALL HAPPENS ON THE RENDER, AND THAT COST IS REAL. With no key the path is
off and says so, which is the state a reviewer will see. With a key, every load
of this page is a model call: it costs money, it takes seconds, and two loads
can disagree because nothing is stored. The fix is not a cache -- it is the
three things propose.py names as missing (an action code, columns for the reason
and the citation, a writer) and none of them is a change to this file. Naming the
cost here is better than a comment claiming it was considered.

Two things this module deliberately does not do.

It does not show a withheld claim's citation chip. The chip is the affordance
that says "there is source behind this"; giving one to a refusal would put the
two states back on the same axis, which is the failure docs/web-design.html
names.

It does not show alignment confidence on an added or removed passage. The diff
records 0.0 there because there is no pairing to be confident about, and
printing "confidence 0.00" beside a pure addition would report a doubt the
system does not hold.
"""

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# The MODULE, not the names on it, for the reason app/web/views/review.py gives:
# tests/test_policy.py reloads app.auth.policy, and a reload rebinds every class
# in it, so an imported PermissionDenied would stop catching the refusal that is
# actually raised.
from app.auth import policy
from app.diff.engine import RESTRUCTURE_CONFIDENCE_CEILING
from app.interpretation.propose import (
    MODEL_ID,
    judge_materiality_for_company,
    transport_from_environment,
)
from app.state.claims import (
    VerifiedClaim,
    WithheldClaim,
    change_for_company,
    escalations_for_company,
    proceedings_for_company,
    verified_claims,
)
from app.state.db import session_scope
from app.state.mapping import (
    Proposal,
    confirm_obligation_for_change,
    propose_obligations_for_change,
)
from app.state.models import AUTHOR_ANALYST, Change
from app.state.queries import passages_for_company, versions_for_company
from app.state.routing import (
    mappings_for_change,
    obligation_for_company,
    resolve_change_owner,
)
from app.web.deps import company_name, current_company, current_user
from app.web.templating import build_templates

router = APIRouter()
templates = build_templates()

# How much of the document to put either side of a cited span. Wide enough that
# the analyst reads the sentence in its section rather than on its own, small
# enough that the panel is not the whole filing. The window then snaps outward
# to the nearest line break, so it never opens or closes mid-word.
CONTEXT_CHARS = 700

# Shown only when the diff itself is unsure. The threshold is the diff engine's
# own ceiling, imported rather than restated: a renumbered section is capped at
# this value precisely so it cannot present itself as a settled match, and a
# second number here would be a second thing to keep in step.
LOW_ALIGNMENT = RESTRUCTURE_CONFIDENCE_CEILING

ALIGNMENT_LABEL = "Alignment confidence"
ALIGNMENT_NOTE = (
    "The diff is not sure these two passages are the same passage. Read the "
    "pairing above as a guess, not a fact."
)

# Both facts get the same answer and the same body. A 403 for one and a 404 for
# the other would tell anyone who asked which change ids exist somewhere in the
# system.
NOT_FOUND = "no change with that id for this company"

# The same treatment for the other end of a mapping. An id belonging to another
# tenant and an id belonging to nobody read alike here, so the refusal cannot be
# used to learn which obligations exist somewhere in the system.
NO_SUCH_OBLIGATION = "no obligation with that id for this company"

# The word for a citation whose section cannot be named, because the offsets
# fall outside every stored passage. It says less rather than guessing a
# section, which is the whole habit of this product.
UNPLACED_REFERENCE = "Cited text"

SOURCE_UNREADABLE = "nothing could be read at those offsets"

_STATUS_BADGE = {"DRAFT": "badge--draft", "FINAL": "badge--final"}
_STATUS_WORD = {"DRAFT": "Draft", "FINAL": "Final"}

# ---------------------------------------------------------------------------
# The model's judgement
# ---------------------------------------------------------------------------

#: How this screen gets a transport. A module attribute rather than a call
#: inside the handler, so a test can put a deterministic fake in its place --
#: every test of this screen drives one and none of them may reach the network.
#: The factory itself belongs to app/interpretation/propose.py and is imported
#: rather than re-spelt, so there is one definition of "is the model path on".
transport_factory = transport_from_environment

#: The two verdicts, in the product's own words rather than the schema's
#: booleans. "Not material" is a finding and is printed as one; it must never
#: read like the absence of a judgement, which is the sentence below it.
VERDICT_MATERIAL = "Material"
VERDICT_NOT_MATERIAL = "Not material"

#: The badge each verdict wears. Both classes already exist in strata.css and
#: already carry this meaning on the project screen -- material is the alarm
#: treatment, routine is its deliberate counterpart -- so materiality reads by
#: contrast rather than by remembering what one colour means.
BADGE_MATERIAL = "badge--material"
BADGE_ROUTINE = "badge--routine"

#: What stands in the verdict's place when the citation does not verify, and
#: also when the answer never reached the verifier. Deliberately the same four
#: words for both: the difference between them is a fact about our parser, not
#: about this change, and the reason underneath says which it was.
JUDGEMENT_WITHHELD = "No judgement made"

#: Printed under every verdict that survives the gate. Three facts a reader
#: needs and cannot get from the verdict itself: which model said it, that the
#: citation was re-read during this render, and that nothing was stored.
JUDGED_BY = (
    f"Judged by {MODEL_ID} while this page rendered. The citation was re-read "
    "against the stored source a moment ago, and nothing was written."
)

#: Unreachable today: a run with no verdict always carries an announcement.
#: Here so the label can never stand over an empty space if that stops being
#: true, because a blank beside "Materiality" reads as "not material".
NOT_JUDGED = "Nothing has judged this change."


# ---------------------------------------------------------------------------
# What this change bears on
#
# THE STEP THE PRODUCT IS NAMED FOR, AND IT HAD NO SCREEN. app/state/routing.py
# walks a change to an obligation to an owner; app/state/mapping.py proposes
# which obligations a change might bear on. Between them was nothing a person
# could press, so change_obligations stayed empty and every change in the
# product routed to ROUTE_NO_OBLIGATION. A module with tests and no route is a
# feature that cannot demonstrate, which is the shape this repository has
# shipped by accident more than once.
#
# THE ONE RULE THIS SECTION IS BUILT AROUND. A lexical overlap between the
# docket's words and the company's words is a CANDIDATE. It is not evidence that
# a change bears on a duty, and the interface may not let it read as one. So the
# two states are DIFFERENT DATACLASSES rather than one class and a flag, in
# exactly the way VerifiedView and WithheldView are above: ConfirmedMapping has
# no matched terms and CandidateMapping has no confirmer, both are slotted so
# neither can be given the other's field at runtime, and the template branches
# on which object it was handed. A mistake here cannot promote a guess to a
# finding; it can only fail to render.
# ---------------------------------------------------------------------------

#: The permission that gates confirming a mapping.
#:
#: NOT A NEW CODE, AND NOT THE PERFECTLY FITTING ONE EITHER, so the compromise is
#: written down rather than discovered. PERMISSION_CODES has no obligation.map,
#: and inventing one is a schema decision with a migration behind it: the strings
#: in that tuple are written into role grants and audit rows that outlive a
#: release, and adding one also means deciding which roles hold it, which is the
#: segregation-of-duties grid rather than a line of code.
#:
#: action.propose is the closest existing code and it is close on the argument
#: rather than by accident. PERMISSION_DESCRIPTIONS reads "Propose an action that
#: follows from a change", and a mapping is the thing an action follows FROM --
#: without it resolve_change_owner refuses and no action can be routed to
#: anybody. It sits on ROLE_ANALYST, which is the person whose job this is, and
#: deliberately NOT on ROLE_OBLIGATION_OWNER: an approver who could decide which
#: duties a change touches could route the approval to themselves, which is the
#: control the whole grid exists to express.
#:
#: What is genuinely missing is a code that says "decide what a change bears
#: on". It is named here so the next person adding to PERMISSION_CODES knows why
#: this one is borrowed.
PERMISSION_MAP = "action.propose"

#: The words on the page for each state. Held as constants because the tests
#: assert on them: a screen test comparing against a literal it typed itself
#: keeps passing after the page stops saying it.
LABEL_CONFIRMED = "Confirmed by"
LABEL_PROPOSED = "Proposed by the pipeline, not confirmed"
LABEL_CANDIDATE = "Candidate"

#: Printed over the candidate list. Says what a candidate is BEFORE the reader
#: sees one, because the alternative is a list of duties that reads like a
#: finding until somebody scrolls to the caveat.
CANDIDATE_NOTE = (
    "These are the duties whose own words appear in the passages this change "
    "touches. A shared word is a reason to look, not a finding: the docket and "
    "the company do not use the same vocabulary, so this list will miss duties "
    "that are affected and offer duties that are not. Nothing below is recorded "
    "until somebody confirms it."
)

#: Printed over the explicit zeroes. The sentence the whole section turns on.
MISSED_NOTE = (
    "The words in this change do not reach these duties. That is a statement "
    "about the words and not about the duties: a change can bear on an "
    "obligation without sharing any vocabulary with it. Map one by hand if you "
    "know it belongs here."
)

#: Printed under a routed owner whose mapping nobody has confirmed.
#:
#: THE PRODUCT'S CENTRAL ARGUMENT, APPLIED TO ITS OWN OUTPUT. Routing hands back
#: a name whether the mapping under it came from a person or from a word
#: overlap, so without this sentence the page states accountability it has not
#: earned. It is the same move the withheld claim makes: say what is missing
#: where the assertion would have gone.
UNCONFIRMED_ROUTING = (
    "Nobody has confirmed the mapping this name rests on. The pipeline proposed "
    "it from a word overlap, so read the owner above as a suggestion rather than "
    "as accountability until somebody confirms it below."
)

#: Shown where the company has recorded no duties at all. Kept apart from the
#: empty candidate list on purpose -- "your obligations are not loaded" must
#: never be able to read as "no obligation is affected".
NO_OBLIGATIONS_NOTE = (
    "This company has no obligations recorded, so there was nothing to map this "
    "change against. Load them before reading anything into the silence."
)


# ---------------------------------------------------------------------------
# What the template is handed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceWindow:
    """An extract of a stored source with the cited characters singled out.

    Three strings, not one with markup in it. The template wraps `marked` in a
    mark element and leaves the other two alone, so nothing is ever inserted
    into the document's own text -- annotating a quotation by altering it is the
    habit this product exists to remove.

    `partial` is true when the extract is not the whole document. The panel says
    so in its head rather than printing an ellipsis into the body, for the same
    reason.
    """

    version_id: str
    version_label: str
    start: int
    end: int
    before: str
    marked: str
    after: str
    partial: bool


@dataclass(frozen=True, slots=True)
class Side:
    """One side of a change: the text a version carried at those offsets.

    A pure addition has no before side and a pure removal has no after side, so
    `present` is false and the offsets are None. The template must not print a
    coordinate for a side that does not exist.
    """

    version_id: str
    version_label: str
    status: str
    present: bool
    start: int | None
    end: int | None
    text: str


@dataclass(frozen=True, slots=True)
class VerifiedView:
    """A claim the product may assert, plus what the citation chip needs."""

    claim: VerifiedClaim
    reference: str
    coordinate: str
    panel_id: str
    window: SourceWindow


@dataclass(frozen=True, slots=True)
class JudgementView:
    """The model's verdict, after its citation was re-read and matched.

    `source_reads` is the source's own bytes at the cited offsets, not the
    model's quote echoed back. Echoing the quote would prove nothing: the whole
    claim of this screen is that the words shown are the document's.
    """

    verdict: str
    badge: str
    why: str
    reference: str
    coordinate: str
    source_reads: str
    judged_by: str


@dataclass(frozen=True, slots=True)
class WithheldJudgementView:
    """A verdict the product refuses to show. It cannot carry the verdict.

    No `verdict` field and no `why`, and slotted so neither can be attached at
    runtime -- the same shape as WithheldView below, for the same reason. The
    template branches on which object it was handed, never on a flag, so a
    mistake here cannot leak a judgement; it can only fail to render.
    """

    reason: str
    reference: str
    coordinate: str
    source_reads: str
    quoted: str


@dataclass(frozen=True, slots=True)
class ConfirmedMapping:
    """A duty a person said this change bears on.

    Carries no matched terms and cannot be given any -- it is somebody's
    judgement, and printing the words that happened to overlap beside it would
    put the machine's reasoning where the person's belongs. Several of these
    were confirmed against no overlap at all, which is the case the whole design
    exists for.
    """

    obligation_id: str
    title: str
    document_ref: str | None
    who: str
    when: str


@dataclass(frozen=True, slots=True)
class CandidateMapping:
    """A duty the words point at, and the span they point from.

    Carries no confirmer and no confirmation time, because nobody has made one.
    `recorded` says the pipeline has already written this candidate into
    change_obligations -- which makes it a stored candidate and not a finding.
    That distinction is the reason the word "candidate" is on the page in both
    cases and the word "confirmed" in neither.
    """

    obligation_id: str
    title: str
    document_ref: str | None
    terms: tuple[str, ...]
    reference: str
    coordinate: str
    quote: str
    recorded: bool
    recorded_at: str


@dataclass(frozen=True, slots=True)
class MissedMapping:
    """A duty with no candidate, and the reason it has none.

    Present on the page, never left off it. A list of two candidates with the
    other six duties dropped reads exactly like a complete answer about a
    company with two duties -- ADR-79 makes this argument for filings and it is
    the same argument for obligations.
    """

    obligation_id: str
    title: str
    reason: str
    note: str


@dataclass(frozen=True, slots=True)
class RoutingView:
    """Who this change is the duty of, or the reason nobody can be named.

    text is app/state/routing.py's own sentence, not a second wording of it.
    Fifteen refusal codes each carry the fix for their own case, and rewriting
    them here would give an analyst a second vocabulary to learn.

    caveat IS THE HALF THAT WAS MISSING AND IT WAS FOUND ON A REAL PAGE.
    resolve_change_owner does not read mapped_by_kind: a mapping the pipeline
    proposed walks to an owner exactly as one a person confirmed does. So a
    Kentucky vegetation-management budget table, which shares the words
    "project" and "budget" with a cost-allocation duty, printed "Sarah
    Lindqvist owns OBL-005" directly above a block saying the same mapping was
    proposed and not confirmed. The page contradicted itself in two adjacent
    sentences, and the confident one was on top.

    The fix here is the screen's and not the routing layer's, deliberately.
    Adding a sixteenth refusal code would change a contract every caller of
    resolve_change_owner depends on, and that is a decision to take in the open
    rather than in passing -- see the foot of app/state/mapping.py. What this
    does is refuse to let an unconfirmed mapping read as accountability, which
    is a statement about how the page reads and is entirely ours to make.
    """

    code: str
    text: str
    routed: bool
    caveat: str


@dataclass(frozen=True, slots=True)
class WithheldView:
    """A claim the product refuses to make.

    Carries no statement and no panel id, because it has neither. `source_reads`
    is the real bytes at the cited offsets -- or a plain sentence saying nothing
    could be read there, which is the case when the offsets fall outside the
    document.
    """

    claim: WithheldClaim
    reference: str
    coordinate: str
    source_reads: str


# ---------------------------------------------------------------------------
# Small pure helpers. Testable without a request.
# ---------------------------------------------------------------------------


def panel_id(claim_id: str) -> str:
    """The id the citation chip points at with aria-controls."""
    return f"source-{claim_id}"


def claim_anchor(claim_id: str) -> str:
    return f"claim-{claim_id}"


def change_url(change_id: str) -> str:
    return f"/changes/{change_id}"


def confirm_url(change_id: str) -> str:
    """Where the confirm form posts. One spelling, shared by view and template."""
    return f"/changes/{change_id}/obligations"


def proceeding_url(proceeding_id: str) -> str:
    return f"/proceedings/{proceeding_id}"


def coordinate(version_id: str, start: int, end: int) -> str:
    """A citation address a person can copy and a machine can re-read."""
    return f"{version_id} {start}-{end}"


def _snap_forward(text: str, floor: int, limit: int) -> int:
    """The first paragraph or line break at or after `floor`, else `floor`.

    Opening the extract at a break means it never begins mid-word. Taking the
    first break rather than the last keeps as much context as the window allows.
    """
    segment = text[floor:limit]
    paragraph = segment.find("\n\n")
    if paragraph != -1:
        return floor + paragraph + 2
    line = segment.find("\n")
    if line != -1:
        return floor + line + 1
    return floor


def _snap_back(text: str, floor: int, ceiling: int) -> int:
    """The last paragraph or line break before `ceiling`, else `ceiling`."""
    segment = text[floor:ceiling]
    paragraph = segment.rfind("\n\n")
    if paragraph != -1:
        return floor + paragraph
    line = segment.rfind("\n")
    if line != -1:
        return floor + line
    return ceiling


def source_window(
    source_text: str,
    cite_start: int,
    cite_end: int,
    *,
    version_id: str = "",
    version_label: str = "",
    context: int = CONTEXT_CHARS,
) -> SourceWindow:
    """The extract the citation viewer shows, cut at line breaks around the span.

    The offsets are clamped into the document rather than trusted. A verified
    claim's offsets are always inside it -- the verifier refused them otherwise
    -- but this function is also the one a future caller will reach for, and a
    slice that silently ran off the end would show the wrong text with no sign
    that anything was wrong.
    """
    length = len(source_text)
    start = max(0, min(cite_start, length))
    end = max(start, min(cite_end, length))

    head = (
        0
        if start - context <= 0
        else _snap_forward(source_text, start - context, start)
    )
    tail = (
        length
        if end + context >= length
        else _snap_back(source_text, end, end + context)
    )

    return SourceWindow(
        version_id=version_id,
        version_label=version_label,
        start=head,
        end=tail,
        before=source_text[head:start],
        marked=source_text[start:end],
        after=source_text[end:tail],
        partial=head > 0 or tail < length,
    )


def _section_at(passages, offset: int) -> str | None:
    """The section label of the passage covering this offset, or None.

    None is a real answer and is rendered as one. Reaching for the nearest
    passage instead would name a section the citation is not in, which is the
    kind of small confident wrongness the product is built to refuse.
    """
    for passage in passages:
        if passage.char_start <= offset < passage.char_end:
            return passage.section
    return None


def _reference(section: str | None) -> str:
    return f"Sec {section}" if section else UNPLACED_REFERENCE


def _shows_alignment(change: Change) -> bool:
    """Whether the pairing behind this change is worth a caution.

    Only a modified passage has a pairing. An added or removed one carries 0.0
    because nothing was aligned, and reporting that as low confidence would
    invent a doubt.
    """
    return (
        change.change_type == "modified"
        and change.alignment_confidence <= LOW_ALIGNMENT
    )


def _stamp(moment: datetime | None) -> str:
    """A date a person can read, or nothing. Never an invented one."""
    if moment is None:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _mapping_views(
    proposal: Proposal, mapped_by: dict[str, tuple[str, datetime | None]]
) -> tuple[
    tuple[ConfirmedMapping, ...],
    tuple[CandidateMapping, ...],
    tuple[MissedMapping, ...],
]:
    """Sort one proposal into the three things a reader is shown.

    THE ORDER OF THE TESTS IS THE WHOLE OF THE SAFETY HERE. A row is confirmed
    only when a person's kind is on it; everything else with words behind it is
    a candidate whether or not the pipeline already stored it; and everything
    left over is an explicit zero. Reading it the other way round -- treating
    any stored row as settled -- is the one mistake that would turn the lexical
    overlap into an assertion, and it is one line of code away, which is why the
    two states are different types rather than a boolean on one.
    """
    confirmed: list[ConfirmedMapping] = []
    candidates: list[CandidateMapping] = []
    missed: list[MissedMapping] = []

    # EVERY OFFERED ROW IS RENDERED, NOT app.state.mapping.MAX_CANDIDATES OF
    # THEM. The shortlist exists for a caller that has to pick one -- the seed
    # does -- and a screen is not that caller: a duty the words reached and the
    # page did not show is a duty the reader was never told about, which is the
    # same short-list failure the missed block below exists to refuse. The cap
    # still decides the ORDER, because proposal.candidates is ranked and this
    # list follows it.
    #
    # THE SAME SPAN IS QUOTED ONCE. Several duties commonly match the same
    # paragraph -- the flagship change proposes three duties off one passage --
    # and three identical blockquotes is a page nobody reads carefully. Repeats
    # keep their reference and coordinate, which is what a reader needs to see
    # that it is the same paragraph, and lose the second copy of the words.
    quoted: set[str] = set()

    for row in proposal.obligations:
        if row.mapped and row.mapped_by_kind == AUTHOR_ANALYST:
            who, when = mapped_by.get(row.obligation_id, ("", None))
            confirmed.append(
                ConfirmedMapping(
                    obligation_id=row.obligation_id,
                    title=row.title,
                    document_ref=row.document_ref,
                    who=who,
                    when=_stamp(when),
                )
            )
            continue
        if row.mapped or row.reason == "":
            # A stored candidate the pipeline wrote, or one found on this read.
            # Both are offered for confirmation and both say what produced them.
            # A stored one with no words behind it -- possible, since a row can
            # be written by a loader -- still shows as a candidate with an empty
            # word list, which reads as thin because it is.
            _who, when = mapped_by.get(row.obligation_id, ("", None))
            span = row.span
            address = (
                coordinate(span.version_id, span.char_start, span.char_end)
                if span
                else ""
            )
            candidates.append(
                CandidateMapping(
                    obligation_id=row.obligation_id,
                    title=row.title,
                    document_ref=row.document_ref,
                    terms=row.matched_terms,
                    reference=_reference(span.section if span else None),
                    coordinate=address,
                    quote=span.text if span else "",
                    recorded=row.mapped,
                    recorded_at=_stamp(when) if row.mapped else "",
                )
            )
            continue
        missed.append(
            MissedMapping(
                obligation_id=row.obligation_id,
                title=row.title,
                reason=row.reason,
                note=row.note,
            )
        )

    # The candidates the proposer ranked come first and in its order; anything
    # else that is offered -- a stored row the words no longer reach -- follows
    # by id. Sorting the whole list here would replace the proposer's ranking
    # with the id order, and the ranking is the part a person reads first.
    ranked = [row.obligation_id for row in proposal.candidates]
    candidates.sort(
        key=lambda row: (
            ranked.index(row.obligation_id) if row.obligation_id in ranked else len(ranked),
            row.obligation_id,
        )
    )

    # The repeat check runs AFTER the sort and not during the build, or the copy
    # of the words would land on whichever row happened to be constructed first
    # rather than on the one a reader meets first -- so the page would show the
    # bare coordinate above and the paragraph below it.
    deduped = []
    for row in candidates:
        if row.coordinate and row.coordinate in quoted:
            row = dataclasses.replace(row, quote="")
        quoted.add(row.coordinate)
        deduped.append(row)
    return tuple(confirmed), tuple(deduped), tuple(missed)


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _side(
    versions: dict, version_id: str, start: int | None, end: int | None
) -> Side:
    version = versions.get(version_id)
    present = start is not None and end is not None and version is not None
    return Side(
        version_id=version_id,
        version_label=version.label if version else version_id,
        status=version.status if version else "",
        present=present,
        start=start if present else None,
        end=end if present else None,
        text=version.source_text[start:end] if present else "",
    )


@router.get("/changes/{change_id}", response_class=HTMLResponse)
def change_detail(
    request: Request,
    change_id: str,
    company_id: str = Depends(current_company),
) -> HTMLResponse:
    """One change, its two sides, and the claims that survive their citations.

    Every read below is scoped by `company_id` and goes through
    app/state/claims.py or app/state/queries.py. A change id belonging to
    another company resolves to None and answers 404 -- the row never reaches
    this function, so there is nothing here that could leak it.

    Nothing is written. Every verdict on this page -- the stored claims' and the
    model's -- is computed from the stored source during this request and is not
    saved, which is what makes editing the source flip a claim to withheld on
    the next render with no job in between.

    ONE THING HERE IS NOT A READ. The materiality call goes out to a model when
    a key is configured, so this GET is not free and is not repeatable. See the
    module docstring: that is a real cost, and the fix is persistence, which
    needs three things that do not exist yet.
    """
    with session_scope() as session:
        change = change_for_company(session, company_id, change_id)
        if change is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)

        versions = {
            version.id: version
            for version in versions_for_company(session, company_id)
        }

        before = _side(
            versions, change.from_version_id, change.before_start, change.before_end
        )
        after = _side(
            versions, change.to_version_id, change.after_start, change.after_end
        )

        docket = next(
            (
                proceeding.docket
                for proceeding in proceedings_for_company(session, company_id)
                if proceeding.id == change.proceeding_id
            ),
            change.proceeding_id,
        )

        verified, withheld = verified_claims(session, company_id, change_id)

        # Passages are read once per cited version, not once per claim. The
        # lookup is only ever used to name the section a citation sits in.
        sections: dict[str, list] = {}

        def _section_for(version_id: str, offset: int) -> str | None:
            if version_id not in sections:
                sections[version_id] = passages_for_company(
                    session, company_id, version_id
                )
            return _section_at(sections[version_id], offset)

        verified_views = [
            VerifiedView(
                claim=claim,
                reference=_reference(
                    _section_for(claim.citation_version_id, claim.citation_start)
                ),
                coordinate=coordinate(
                    claim.citation_version_id, claim.citation_start, claim.citation_end
                ),
                panel_id=panel_id(claim.claim_id),
                window=source_window(
                    versions[claim.citation_version_id].source_text,
                    claim.citation_start,
                    claim.citation_end,
                    version_id=claim.citation_version_id,
                    version_label=versions[claim.citation_version_id].label,
                ),
            )
            for claim in verified
        ]

        withheld_views = [
            WithheldView(
                claim=claim,
                reference=_reference(
                    _section_for(claim.citation_version_id, claim.citation_start)
                    if claim.citation_version_id in versions
                    else None
                ),
                coordinate=coordinate(
                    claim.citation_version_id, claim.citation_start, claim.citation_end
                ),
                source_reads=claim.source_excerpt or SOURCE_UNREADABLE,
            )
            for claim in withheld
        ]

        # The one model call in the product. It goes through the scoped entry
        # point rather than being assembled here, even though this function is
        # already holding the change and the versions: a second way to resolve
        # a change for a company is a second thing to get the tenancy wrong in,
        # and it would be the one nobody audited. The extra read is the price.
        run = judge_materiality_for_company(
            session, company_id, change_id, transport=transport_factory()
        )

        judgement = None
        judgement_withheld = None
        if run.judgement is not None:
            verdict = run.judgement
            judgement = JudgementView(
                verdict=(
                    VERDICT_MATERIAL if verdict.material else VERDICT_NOT_MATERIAL
                ),
                badge=BADGE_MATERIAL if verdict.material else BADGE_ROUTINE,
                why=verdict.why,
                reference=_reference(
                    _section_for(
                        verdict.citation_version_id, verdict.citation_start
                    )
                ),
                coordinate=coordinate(
                    verdict.citation_version_id,
                    verdict.citation_start,
                    verdict.citation_end,
                ),
                source_reads=verdict.actual_text,
                judged_by=JUDGED_BY,
            )
        elif run.withheld is not None:
            refused = run.withheld
            judgement_withheld = WithheldJudgementView(
                reason=refused.reason,
                reference=_reference(
                    _section_for(
                        refused.citation_version_id, refused.citation_start
                    )
                    if refused.citation_version_id in versions
                    else None
                ),
                coordinate=coordinate(
                    refused.citation_version_id,
                    refused.citation_start,
                    refused.citation_end,
                ),
                source_reads=refused.source_excerpt or SOURCE_UNREADABLE,
                quoted=refused.citation_quote,
            )

        review_count = len(
            escalations_for_company(session, company_id, unresolved_only=True)
        )

        # WHAT THIS CHANGE BEARS ON, AND WHO HOLDS IT. Two reads, no model, and
        # nothing written. The proposal is recomputed on every render for the
        # same reason the claims are: a duty mapped this morning has to change
        # what this page says this afternoon without a job having run, and a
        # stored candidate list would be a promise about rows that may have
        # moved since.
        proposal = propose_obligations_for_change(
            session, company_id, change_id=change_id
        )
        mapped_by = {
            row.obligation_id: (row.mapped_by, row.mapped_at)
            for row in mappings_for_change(session, company_id, change_id)
        }
        confirmed, candidates, missed = _mapping_views(proposal, mapped_by)

        resolution = resolve_change_owner(session, company_id, change_id=change_id)
        routing = RoutingView(
            code=resolution.reason_code,
            text=resolution.reason_text,
            routed=resolution.routed,
            # A name on the page with nobody's judgement behind it. See the
            # RoutingView docstring for the page this was found on.
            caveat=(
                UNCONFIRMED_ROUTING if resolution.routed and not confirmed else ""
            ),
        )

        # Whether to DRAW the confirm control, which is not the same question as
        # whether somebody may use it. has() answers the first and writes
        # nothing; require() is the gate on the POST and is the only thing that
        # decides the second. A page drawn a minute ago is a statement about the
        # past, and a grant can be revoked between the render and the click.
        principal = current_user(request)
        may_confirm = principal is not None and policy.has(
            session, company_id, principal.user_id, PERMISSION_MAP
        )

        section = change.section
        context = {
            "page_title": f"Section {section}" if section else f"Change {change.id}",
            "company_id": company_id,
            "company_name": company_name(company_id),
            "nav_active": "change",
            "nav_proceeding_url": proceeding_url(change.proceeding_id),
            "nav_change_url": change_url(change.id),
            "review_count": review_count,
            "change": change,
            "heading": f"Section {section}" if section else "Unlabelled passage",
            "docket": docket,
            "status_badge": _STATUS_BADGE.get(change.status, ""),
            "status_word": _STATUS_WORD.get(change.status, change.status),
            "before": before,
            "after": after,
            "show_alignment": _shows_alignment(change),
            "alignment_label": ALIGNMENT_LABEL,
            "alignment_note": ALIGNMENT_NOTE,
            "alignment_value": f"{change.alignment_confidence:.2f}",
            "verified": verified_views,
            "withheld": withheld_views,
            "claim_anchor": claim_anchor,
            # Four keys, at most one of which is not None. The template picks
            # the first one it finds rather than reading a state word, so a
            # branch added later cannot render a verdict object that is missing
            # its verdict.
            "judgement": judgement,
            "judgement_withheld": judgement_withheld,
            "judgement_dropped": run.dropped,
            "judgement_absence": run.announcement or NOT_JUDGED,
            "judgement_withheld_label": JUDGEMENT_WITHHELD,
            # The mapping step. Four keys and none of them a state word: the
            # template iterates whichever lists it was handed.
            "routing": routing,
            "confirmed_mappings": confirmed,
            "candidate_mappings": candidates,
            "missed_mappings": missed,
            # Each distinct code once, in the order it first appears. The rows
            # carry the code; this carries the sentence behind it.
            "missed_reasons": tuple(
                dict((row.reason, row.note) for row in missed).items()
            ),
            "mapping_reason": proposal.reason,
            "mapping_in_scope": proposal.in_scope,
            "may_confirm": may_confirm,
            "confirm_url": confirm_url(change.id),
            "label_confirmed": LABEL_CONFIRMED,
            "label_proposed": LABEL_PROPOSED,
            "label_candidate": LABEL_CANDIDATE,
            "candidate_note": CANDIDATE_NOTE,
            "missed_note": MISSED_NOTE,
            "no_obligations_note": NO_OBLIGATIONS_NOTE,
        }

    return templates.TemplateResponse(request, "change.html", context)


@router.post("/changes/{change_id}/obligations")
def confirm_obligation(
    request: Request,
    change_id: str,
    obligation_id: str = Form(default=""),
    company_id: str = Depends(current_company),
):
    """A person accepts a candidate: this change bears on this duty.

    The one write on this screen, and it is the load-bearing judgement in the
    product -- everything downstream, the owner handoff and the approval route,
    hangs off the row it appends. So it is gated, it is audited, and the row it
    writes says a person made it rather than the pipeline.

    THE ORDER OF THE CHECKS IS DELIBERATE and it follows app/auth/policy.py's
    own argument. Permission first, existence second, so somebody without the
    permission gets the same answer for a change that exists and one that does
    not, and cannot use this route to find out which ids are real.

    THE REFUSAL IS CAUGHT AND ANSWERED. policy.require raises PermissionDenied,
    which is not an HTTPException: an uncaught one is a 500 and a traceback
    where a decision belongs. tests/test_tenancy_derived.py posts to every route
    in the product as somebody holding no roles, and that is how the same
    omission was found in the escalation resolver.

    Another company's change id is a 404 with the same body an unknown id gets.
    Nothing about it is read and nothing about it is written.
    """
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status_code=403, detail="sign in to map a change")

    with session_scope() as session:
        try:
            policy.require(session, company_id, principal.user_id, PERMISSION_MAP)
        except policy.PermissionDenied as denied:
            raise HTTPException(status_code=403, detail=denied.reason) from None

        if change_for_company(session, company_id, change_id) is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        if obligation_for_company(session, company_id, obligation_id) is None:
            raise HTTPException(status_code=404, detail=NO_SUCH_OBLIGATION)

        confirm_obligation_for_change(
            session,
            company_id,
            change_id=change_id,
            obligation_id=obligation_id,
            actor=f"person:{principal.email}",
            actor_user_id=principal.user_id,
        )

    # 303, so a reload of the change screen does not repost the confirmation.
    return RedirectResponse(url=change_url(change_id), status_code=303)


__all__ = [
    "ALIGNMENT_LABEL",
    "ALIGNMENT_NOTE",
    "BADGE_MATERIAL",
    "BADGE_ROUTINE",
    "CANDIDATE_NOTE",
    "CONTEXT_CHARS",
    "JUDGED_BY",
    "JUDGEMENT_WITHHELD",
    "LABEL_CANDIDATE",
    "LABEL_CONFIRMED",
    "LABEL_PROPOSED",
    "LOW_ALIGNMENT",
    "MISSED_NOTE",
    "NOT_FOUND",
    "NOT_JUDGED",
    "NO_OBLIGATIONS_NOTE",
    "NO_SUCH_OBLIGATION",
    "PERMISSION_MAP",
    "UNCONFIRMED_ROUTING",
    "VERDICT_MATERIAL",
    "VERDICT_NOT_MATERIAL",
    "CandidateMapping",
    "ConfirmedMapping",
    "JudgementView",
    "MissedMapping",
    "RoutingView",
    "Side",
    "SourceWindow",
    "VerifiedView",
    "WithheldJudgementView",
    "WithheldView",
    "change_detail",
    "change_url",
    "claim_anchor",
    "confirm_obligation",
    "confirm_url",
    "coordinate",
    "panel_id",
    "proceeding_url",
    "router",
    "source_window",
    "transport_factory",
]
