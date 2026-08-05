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

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

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
from app.state.models import Change
from app.state.queries import passages_for_company, versions_for_company
from app.web.deps import company_name, current_company
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
        }

    return templates.TemplateResponse(request, "change.html", context)


__all__ = [
    "ALIGNMENT_LABEL",
    "ALIGNMENT_NOTE",
    "BADGE_MATERIAL",
    "BADGE_ROUTINE",
    "CONTEXT_CHARS",
    "JUDGED_BY",
    "JUDGEMENT_WITHHELD",
    "LOW_ALIGNMENT",
    "NOT_FOUND",
    "NOT_JUDGED",
    "VERDICT_MATERIAL",
    "VERDICT_NOT_MATERIAL",
    "JudgementView",
    "Side",
    "SourceWindow",
    "VerifiedView",
    "WithheldJudgementView",
    "WithheldView",
    "change_detail",
    "change_url",
    "claim_anchor",
    "coordinate",
    "panel_id",
    "proceeding_url",
    "router",
    "source_window",
    "transport_factory",
]
