"""The gate. Nothing becomes fact without passing through here.

This module calls no model and makes no network request, on purpose. The code
that decides whether the product may assert something must be auditable by a
reviewer who trusts nothing about the AI, and must run in CI with no API key.

It compares for equality after normalization, not for similarity. A paraphrase
is exactly what an auditor will not accept, so a threshold would defeat the
point of having the gate at all.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.text.normalize import normalize, normalized_projection

if TYPE_CHECKING:  # pragma: no cover - typing only, no import at runtime
    from sqlalchemy.orm import Session

REASON_OUT_OF_RANGE = "citation offsets fall outside the source text"
REASON_EMPTY_SPAN = "citation span is empty"
REASON_QUOTE_MISMATCH = "quoted text does not match the source at the cited offsets"
REASON_AMBIGUOUS_OCCURRENCE = (
    "quoted text appears more than once and the cited occurrence was not stated "
    "or does not match"
)
REASON_VERSION_UNREADABLE = "the cited version could not be read for this company"


@dataclass(frozen=True)
class Citation:
    version_id: str
    char_start: int
    char_end: int
    quoted_text: str


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: str | None = None
    actual_text: str | None = None


def _spans_of(quoted_text: str, source_text: str) -> list[tuple[int, int]]:
    """Every raw span of the source whose normalized text equals the quote's.

    The source is projected into normalized space once, carrying a map from
    each normalized character back to the raw characters that produced it. The
    search runs in normalized space, and each hit maps back to raw offsets.

    This replaced a raw substring search with a fixed-width window scan behind
    it, and the window scan was wrong rather than slow. It assumed
    normalization preserves length. Whitespace collapse does not, nor does
    deleting a soft hyphen, nor does expanding a ligature -- so on PDF-shaped
    text the scan found nothing, occurrence_count() answered zero, and the
    repeated-boilerplate guard switched itself off without a word. One pass
    over the source rather than one per offset: O(n), not O(n*m).
    """
    needle = normalize(quoted_text)
    if not needle:
        return []

    projection = normalized_projection(source_text)
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    position = projection.text.find(needle)
    while position != -1:
        span = projection.raw_span(position, position + len(needle))
        # Two normalized positions can land on the same raw span when a single
        # source character expanded into several. One span, counted once.
        if span not in seen:
            seen.add(span)
            spans.append(span)
        position = projection.text.find(needle, position + 1)
    return spans


def occurrence_count(quoted_text: str, source_text: str) -> int:
    """How many times this text appears in the source."""
    return len(_spans_of(quoted_text, source_text))


def occurrence_index(citation: Citation, source_text: str) -> int:
    """Zero-based index of the cited span among all occurrences. -1 if absent."""
    spans = _spans_of(citation.quoted_text, source_text)
    for index, (start, _end) in enumerate(spans):
        if start == citation.char_start:
            return index
    return -1


def verify_citation(
    citation: Citation,
    source_text: str,
    expected_occurrence: int | None = None,
) -> VerificationResult:
    """Re-read the source at the cited offsets and confirm the quote matches.

    Text equality is necessary and not sufficient. When a quote appears more
    than once, the same words in a different section mean a different thing, so
    the claim must also state which occurrence it relies on.

    WHAT THIS FUNCTION TRUSTS THE CALLER FOR. It does not check that
    source_text is the text of citation.version_id -- it has no way to. The
    pairing is the caller's guarantee, and it is a real one to make: a citation
    naming v2 that is handed v1's text verifies whenever the quote sits at the
    same offsets in both, which repeated boilerplate across versions makes
    reachable rather than theoretical. Callers that hold a session rather than
    a string should use verify_citation_for_version(), which loads the named
    version itself and cannot be given the wrong one.
    """
    start, end = citation.char_start, citation.char_end

    if start < 0 or end < 0 or end > len(source_text) or start > end:
        return VerificationResult(False, REASON_OUT_OF_RANGE, None)

    actual = source_text[start:end]

    if not normalize(actual) or not normalize(citation.quoted_text):
        return VerificationResult(False, REASON_EMPTY_SPAN, actual)

    if normalize(actual) != normalize(citation.quoted_text):
        return VerificationResult(False, REASON_QUOTE_MISMATCH, actual)

    if occurrence_count(citation.quoted_text, source_text) > 1:
        found = occurrence_index(citation, source_text)
        if expected_occurrence is None or expected_occurrence != found:
            return VerificationResult(False, REASON_AMBIGUOUS_OCCURRENCE, actual)

    return VerificationResult(True, None, actual)


def verify_citation_for_version(
    session: "Session",
    citation: Citation,
    company_id: str,
    *,
    expected_occurrence: int | None = None,
) -> VerificationResult:
    """Verify a citation against the version it names, not against a text handed in.

    citation.version_id was carried everywhere and checked nowhere. This is the
    entry point that closes it: the version named by the citation is loaded
    here, through the tenant chokepoint, and the quote is verified against that
    text. A citation naming v2 can no longer pass because its words happen to
    sit at those offsets in v1.

    A version this company cannot read is refused, not answered. That covers
    both a version id that does not exist and one belonging to another tenant;
    the two are deliberately indistinguishable from outside, since telling them
    apart tells a caller which ids exist.
    """
    # Imported here rather than at module scope. Everything above this function
    # is pure -- no model, no network, no database -- so a reviewer can read
    # and run the gate with nothing installed, and the dependency on persistence
    # stays visible at the one place that needs it instead of at import time.
    from app.state.queries import versions_for_company

    source_text = None
    for version in versions_for_company(session, company_id):
        if version.id == citation.version_id:
            source_text = version.source_text
            break

    if source_text is None:
        return VerificationResult(False, REASON_VERSION_UNREADABLE, None)

    return verify_citation(citation, source_text, expected_occurrence)
