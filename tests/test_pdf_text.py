"""The occurrence check, run over text shaped the way a PDF extractor emits it.

WHY THIS FILE EXISTS. The repeated-boilerplate guard is one of the product's
strongest ideas: the same sentence appears three times in a proceeding, about
three different subject matters, so a citation must say which occurrence it
relies on. The guard is only as good as the occurrence count behind it, and
that count used to be computed by scanning fixed-width raw windows and
normalizing each one. That assumes normalization preserves length. It does not
-- whitespace collapses, soft hyphens vanish, ligatures expand -- so on real
extracted text the scan found nothing, the count came back zero, and the guard
switched itself off without a word. Every other test in the suite fed the
verifier clean text and so could not see it.

THE FIXTURE IS BUILT, NOT PARSED. The raw offsets asserted below are recorded
while the fixture string is assembled, before any code under test runs. They
are ground truth by construction rather than by agreement with the thing being
tested, which is the only way this file can fail honestly.

The three artefacts in the fixture are the three a PDF extractor really does
produce: the soft hyphen U+00AD left behind by justified typesetting, a hard
hyphen against a line break where the word genuinely carries one, and line
wrapping that turns single spaces into newlines and runs of indent.
"""

from app.text.normalize import normalize
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    Citation,
    occurrence_count,
    occurrence_index,
    verify_citation,
)
from app.verification.verifier import _spans_of

SOFT = "­"  # U+00AD, invisible in the source, so it is written as an escape
NBSP = " "
FFI = "ﬃ"  # the ffi ligature, which NFKC expands to three characters
WIDE_FIVE = "５"  # full-width digit five

BOILERPLATE = (
    "The Utility shall maintain records sufficient to demonstrate compliance "
    "with this Order for a period of not less than five (5) years."
)

# Four renderings of that one sentence, as four different pages of the same
# extracted document might carry it. The first three must still be found. The
# fourth must not, and that is the point of it -- see the test that names it.
_JUSTIFIED = (
    f"The  Utility shall main{SOFT}tain records sufficient to demonstrate compliance\n"
    "with this Order for a period of not less than five (5) years."
)
_LIGATURED = (
    f"The{NBSP}Utility shall maintain records su{FFI}cient to demon{SOFT}strate compliance\n"
    "        with this Order for a period of not less than five (5) years."
)
_WRAPPED = (
    f"The Utility shall main{SOFT}tain re{SOFT}cords sufficient to demonstrate "
    f"com{SOFT}pliance with this\r\n   Order for a period of not less than "
    f"five ({WIDE_FIVE}) years."
)
_WORD_BROKEN = (
    "The Utility shall maintain records sufficient to demon-\n"
    "strate compliance with this Order for a period of not less than five (5) years."
)


def _pdf_page() -> tuple[str, list[tuple[int, int]], tuple[int, int]]:
    """Assemble the fixture, recording each boilerplate span as it is written.

    Returns the raw text, the three raw spans that must be found, and the raw
    span of the fourth rendering, which must not be.
    """
    parts: list[str] = []
    cursor = 0
    found: list[tuple[int, int]] = []

    def write(text: str) -> tuple[int, int]:
        nonlocal cursor
        start = cursor
        parts.append(text)
        cursor += len(text)
        return start, cursor

    write(
        "IN THE MATTER OF LARGE LOAD INTER" + SOFT + "CONNECTION\n"
        "Docket No. MPUC-2026-0142\n\n"
        "4.4 Study Timelines.  The Utility shall complete each study within the\n"
        "   periods stated in Section 4.2.  "
    )
    found.append(write(_JUSTIFIED))

    write(
        "\n\n5.3 Cost-\nCausation Methodology.  Costs shall be assigned on a cost-\n"
        "   causation basis.\n\n"
        "6.3 Return of Collateral.  Collateral shall be returned within thirty\n"
        "(30) days.  "
    )
    found.append(write(_LIGATURED))

    write(
        "\n\n7.1 Record-\nKeeping Format.  Records may be kept in any form.\n\n"
        "7.3 Recordkeeping.  "
    )
    found.append(write(_WRAPPED))

    write("\n\nAPPENDIX A -- NOTE ON RETENTION.\n")
    broken = write(_WORD_BROKEN)
    write("\n")

    return "".join(parts), found, broken


PDF_TEXT, EXPECTED_SPANS, BROKEN_SPAN = _pdf_page()


# --- the fixture is what it claims to be -------------------------------------


def test_the_fixture_really_carries_all_three_extraction_artefacts():
    # A guard on the test rather than on the product. If a later edit tidied
    # these characters out of the fixture, every assertion below would still
    # pass while testing nothing.
    assert SOFT in PDF_TEXT
    assert "-\n" in PDF_TEXT
    assert FFI in PDF_TEXT
    assert NBSP in PDF_TEXT
    assert "  " in PDF_TEXT


def test_normalization_changes_the_length_of_every_recorded_span():
    # The precise assumption the old window scan made. If any span happened to
    # keep its length, that span would prove nothing.
    for start, end in EXPECTED_SPANS:
        assert end - start != len(BOILERPLATE)
        assert normalize(PDF_TEXT[start:end]) == BOILERPLATE


# --- the guard still works on this text --------------------------------------


def test_all_three_occurrences_are_found_in_pdf_shaped_text():
    assert occurrence_count(BOILERPLATE, PDF_TEXT) == 3


def test_the_spans_found_are_the_raw_offsets_recorded_when_the_fixture_was_built():
    assert _spans_of(BOILERPLATE, PDF_TEXT) == EXPECTED_SPANS


def test_each_occurrence_reports_its_own_index_in_document_order():
    indices = [
        occurrence_index(Citation("pdf-v1", start, end, BOILERPLATE), PDF_TEXT)
        for start, end in EXPECTED_SPANS
    ]
    assert indices == [0, 1, 2]


def test_a_repeated_quote_in_pdf_text_is_still_refused_without_an_occurrence():
    start, end = EXPECTED_SPANS[1]
    result = verify_citation(Citation("pdf-v1", start, end, BOILERPLATE), PDF_TEXT)
    assert result.verified is False
    assert result.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_stating_the_occurrence_verifies_it_and_stating_another_does_not():
    for index, (start, end) in enumerate(EXPECTED_SPANS):
        citation = Citation("pdf-v1", start, end, BOILERPLATE)
        assert verify_citation(citation, PDF_TEXT, expected_occurrence=index).verified
        wrong = (index + 1) % len(EXPECTED_SPANS)
        refused = verify_citation(citation, PDF_TEXT, expected_occurrence=wrong)
        assert refused.verified is False
        assert refused.reason == REASON_AMBIGUOUS_OCCURRENCE


# --- the rendering that must NOT be found ------------------------------------


def test_a_hyphenated_word_break_is_not_counted_as_a_fourth_occurrence():
    # "demon-\nstrate" could be a broken word or a hyphenated one. Deciding is
    # a guess, and this module exists so that nothing guesses. So the fourth
    # rendering does not match, the count stays three, and a citation aimed at
    # it goes to review instead of asserting a word nobody wrote.
    assert BROKEN_SPAN not in _spans_of(BOILERPLATE, PDF_TEXT)
    assert normalize(PDF_TEXT[slice(*BROKEN_SPAN)]) != BOILERPLATE
    assert "demon-strate" in normalize(PDF_TEXT[slice(*BROKEN_SPAN)])

    start, end = BROKEN_SPAN
    result = verify_citation(Citation("pdf-v1", start, end, BOILERPLATE), PDF_TEXT)
    assert result.verified is False


# --- a hyphen that is genuinely part of the word -----------------------------


def test_a_real_hyphen_split_across_a_line_break_still_verifies():
    # The other half of the same rule. "Cost-Causation" and "Record-Keeping"
    # carry their hyphens in the source, so losing only the break restores the
    # heading a citation would quote.
    for quote in ("5.3 Cost-Causation Methodology.", "7.1 Record-Keeping Format."):
        spans = _spans_of(quote, PDF_TEXT)
        assert len(spans) == 1, quote
        start, end = spans[0]
        assert verify_citation(Citation("pdf-v1", start, end, quote), PDF_TEXT).verified
