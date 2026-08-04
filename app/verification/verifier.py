"""The gate. Nothing becomes fact without passing through here.

This module calls no model and makes no network request, on purpose. The code
that decides whether the product may assert something must be auditable by a
reviewer who trusts nothing about the AI, and must run in CI with no API key.

It compares for equality after normalization, not for similarity. A paraphrase
is exactly what an auditor will not accept, so a threshold would defeat the
point of having the gate at all.
"""

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.text.normalize import Projection, normalize, normalized_projection

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
REASON_VERSION_CHANGED = (
    "the cited version's stored text no longer matches the hash recorded when it "
    "was ingested"
)


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


def _covers_whole_characters(projection: Projection, start: int, end: int) -> bool:
    """Does the normalized span [start, end) begin and end on a source character?

    One source character can produce several normalized ones -- a ligature, a
    squared-metre glyph -- and every character it produced carries the same raw
    start and the same raw end. So a hit begins part-way through an expansion
    exactly when the character before it shares its start, and ends part-way
    through one exactly when the character after it shares its end. Two
    comparisons, and no re-reading of the source.

    SOUND, NOT EXACT. A False here does not prove the span is wrong. Fifty
    characters in Unicode -- the spacing accents, U+00A8 and its relatives --
    fold to a space followed by a combining mark, and a hit starting at that
    mark begins part-way through the expansion yet still normalizes to the
    quote, because normalize() strips the leading space off a substring. This
    function answers False there. The caller re-reads what it refuses; it never
    re-reads what it clears, and tests/test_verification.py measures that
    direction over randomized adversarial text rather than asserting it.
    """
    starts, ends = projection.starts, projection.ends
    return (start == 0 or starts[start] != starts[start - 1]) and (
        end == len(projection.text) or ends[end - 1] != ends[end]
    )


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
    repeated-boilerplate guard switched itself off without a word.

    WHAT IT COSTS. One pass over the source, then a constant-time boundary test
    per hit. The docstring used to claim O(n) while re-normalizing every hit,
    which is O(m) each, and hits scale with the source: a 144,000-character rule
    line against a ten-character quote found 143,991 hits and took 357 ms, and
    doubling either the source or the quote doubled it. The boundary test takes
    that case to 68 ms and makes it flat in the quote's length -- 40,000
    characters of dotted leaders against a 400-character quote went from 1.46 s
    to 48 ms. Prose is barely touched: a real filing gains nothing measurable,
    because a sentence-length quote hits three times, not a hundred thousand.

    It is still not O(n) in the worst case, and saying otherwise would repeat
    the mistake. The exact re-read survives for the hits the boundary test
    refuses, so a source made entirely of expanding characters still costs
    O(n*m). Filing text is not that; a page of accented capitals would be.
    """
    needle = normalize(quoted_text)
    if not needle:
        return []

    projection = normalized_projection(source_text)
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    position = projection.text.find(needle)
    while position != -1:
        end = position + len(needle)
        span = projection.raw_span(position, end)
        # Two normalized positions can land on the same raw span when a single
        # source character expanded into several. One span, counted once.
        if span not in seen:
            seen.add(span)
            # And a hit can begin or end part-way through one such expansion --
            # a quote starting at the "2" of a squared-metre glyph. There is no
            # raw span for half a character, so the span mapped back is wider
            # than the quote and must not be returned. The boundary test settles
            # that from the projection alone. Where it refuses, the span is
            # re-read, because the test is stricter than the truth on the
            # spacing accents and dropping an occurrence is what switches the
            # repeated-boilerplate guard off.
            if (
                _covers_whole_characters(projection, position, end)
                or normalize(source_text[span[0] : span[1]]) == needle
            ):
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
    reachable rather than theoretical. Nor can it tell that the text it was
    handed is still the text that was ingested, since it holds no hash to
    compare against.

    WHICH ENTRY POINT IS CANONICAL. This one. Every claim the product renders
    goes through verified_claims() in app/state/claims.py, which reads the
    company's versions once through the tenant chokepoint and calls this
    function with the text of the version each claim names -- so the pairing is
    made against a map keyed on version id, not assumed, and there is one place
    to check the map rather than one per call site. The eval harness calls this
    function directly against a fixed corpus, which is the same guarantee made
    by hand. verify_citation_for_version() below makes the pairing itself, for a
    caller holding a session and a single citation, and adds the hash check this
    function cannot make. Nothing in app/ calls it, so do not read it as the
    path in use.
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

    THE VERDICT IS BOUND TO THE BYTES, NOT TO THE ID. The row's text is hashed
    and compared with the digest ingest recorded, before the quote is looked at.
    Without that, a clean verdict says only that the quote sits somewhere in
    whatever the row holds today: edit twenty characters at the top of a version
    and every citation into it still verifies, while the stored hash disagrees
    with the stored text. That is best-practices.html principle 28 -- a verified
    fact has a shelf life. One hash over text already in memory buys it.

    NOT A CALLER YOU HAVE. Nothing in app/ calls this. verified_claims() in
    app/state/claims.py is the path every rendered claim takes, and it does not
    use this function; see the note there. Two consequences follow and neither
    is hidden: the hash check above does not yet run in production, and this
    function finds its version by reading every version the company owns, which
    is a whole-corpus load per call. Both are fixed by the same change --
    a one-row scoped read in app/state/queries.py, which does not exist yet.
    """
    # Imported here rather than at module scope. Everything above this function
    # is pure -- no model, no network, no database -- so a reviewer can read
    # and run the gate with nothing installed, and the dependency on persistence
    # stays visible at the one place that needs it instead of at import time.
    from app.state.queries import versions_for_company

    version = None
    for candidate in versions_for_company(session, company_id):
        if candidate.id == citation.version_id:
            version = candidate
            break

    if version is None:
        return VerificationResult(False, REASON_VERSION_UNREADABLE, None)

    digest = hashlib.sha256(version.source_text.encode("utf-8")).hexdigest()
    if digest != version.source_sha256:
        # No excerpt on this refusal. Bytes from a version that has drifted from
        # its hash are not evidence, and putting them beside a refusal invites
        # the reader to treat them as the source anyway.
        return VerificationResult(False, REASON_VERSION_CHANGED, None)

    return verify_citation(citation, version.source_text, expected_occurrence)
