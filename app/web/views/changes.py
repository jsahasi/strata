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
from fastapi.templating import Jinja2Templates

from app.diff.engine import RESTRUCTURE_CONFIDENCE_CEILING
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
from app.web import TEMPLATES_DIR
from app.web.deps import company_name, current_company

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

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

    Nothing is written. The verdict on each claim is computed from the stored
    source during this request and is not saved, which is what makes editing the
    source flip a claim to withheld on the next render with no job in between.
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
        }

    return templates.TemplateResponse(request, "change.html", context)


__all__ = [
    "ALIGNMENT_LABEL",
    "ALIGNMENT_NOTE",
    "CONTEXT_CHARS",
    "LOW_ALIGNMENT",
    "NOT_FOUND",
    "Side",
    "SourceWindow",
    "VerifiedView",
    "WithheldView",
    "change_detail",
    "change_url",
    "claim_anchor",
    "coordinate",
    "panel_id",
    "proceeding_url",
    "router",
    "source_window",
]
