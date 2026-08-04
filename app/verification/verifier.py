"""The gate. Nothing becomes fact without passing through here.

This module calls no model and makes no network request, on purpose. The code
that decides whether the product may assert something must be auditable by a
reviewer who trusts nothing about the AI, and must run in CI with no API key.

It compares for equality after normalization, not for similarity. A paraphrase
is exactly what an auditor will not accept, so a threshold would defeat the
point of having the gate at all.
"""

from dataclasses import dataclass

from app.text.normalize import normalize

REASON_OUT_OF_RANGE = "citation offsets fall outside the source text"
REASON_EMPTY_SPAN = "citation span is empty"
REASON_QUOTE_MISMATCH = "quoted text does not match the source at the cited offsets"
REASON_AMBIGUOUS_OCCURRENCE = (
    "quoted text appears more than once and the cited occurrence was not stated "
    "or does not match"
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


def _spans_of(quoted_text: str, source_text: str) -> list[tuple[int, int]]:
    """Every span whose normalized text equals the normalized quote.

    Scans candidate spans by raw substring search first, which is exact for
    this corpus. Normalization then decides equality, so the two paths agree.
    """
    needle = normalize(quoted_text)
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = source_text.find(quoted_text)
    while start != -1:
        spans.append((start, start + len(quoted_text)))
        start = source_text.find(quoted_text, start + 1)
    if spans:
        return spans
    # Fall back to a normalized scan for quotes that differ only in whitespace.
    width = len(quoted_text)
    for index in range(0, max(0, len(source_text) - width + 1)):
        window = source_text[index:index + width]
        if normalize(window) == needle:
            spans.append((index, index + width))
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
