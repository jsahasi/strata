import json
from pathlib import Path

from app.text.normalize import normalize
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    Citation,
    occurrence_count,
    occurrence_index,
    verify_citation,
)
from app.verification.verifier import _spans_of

DATA = Path(__file__).resolve().parent.parent / "data"
BOILERPLATE = (
    "The Utility shall maintain records sufficient to demonstrate compliance "
    "with this Order for a period of not less than five (5) years."
)


def _v1_text() -> str:
    return (DATA / "v1_notice_of_proposed_rulemaking.txt").read_bytes().decode("utf-8")


def test_the_trap_sentence_occurs_exactly_three_times_in_v1():
    assert occurrence_count(BOILERPLATE, _v1_text()) == 3


def test_each_recorded_occurrence_reports_its_own_index():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    v1_spans = [
        o for o in manifest["repeated_boilerplate"]["occurrences"] if o["version"] == "v1"
    ]
    text = _v1_text()
    indices = [
        occurrence_index(Citation("v1", o["start"], o["end"], BOILERPLATE), text)
        for o in sorted(v1_spans, key=lambda o: o["start"])
    ]
    assert indices == [0, 1, 2]


def test_a_repeated_quote_without_a_stated_occurrence_is_not_verified():
    # Textually perfect, substantively ambiguous. The product must not assert it.
    text = _v1_text()
    result = verify_citation(Citation("v1", 4930, 5063, BOILERPLATE), text)
    assert result.verified is False
    assert result.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_stating_the_correct_occurrence_verifies():
    text = _v1_text()
    result = verify_citation(
        Citation("v1", 4930, 5063, BOILERPLATE), text, expected_occurrence=0
    )
    assert result.verified is True


def test_stating_the_wrong_occurrence_is_rejected():
    # Section 4.4 offsets, claimed as the Section 7.3 recordkeeping occurrence.
    text = _v1_text()
    result = verify_citation(
        Citation("v1", 4930, 5063, BOILERPLATE), text, expected_occurrence=2
    )
    assert result.verified is False
    assert result.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_the_spans_found_are_the_offsets_the_manifest_computed_independently():
    # The indices above only prove the spans are in the right order. This
    # checks the spans themselves against ground truth produced by a separate
    # script and re-verified against the bytes -- so the search is measured
    # against something that did not come from the search.
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    expected = sorted(
        (o["start"], o["end"])
        for o in manifest["repeated_boilerplate"]["occurrences"]
        if o["version"] == "v1"
    )
    assert _spans_of(BOILERPLATE, _v1_text()) == expected


def test_a_quote_beginning_part_way_through_an_expanded_character_reports_nothing():
    # Regression. A squared-metre glyph normalizes to the two characters "m2",
    # so a quote beginning at that "2" matches in normalized space while no raw
    # span stands behind it -- there is no such thing as half a source
    # character. The search used to answer with the whole glyph's span, which
    # normalizes to more than the quote. A reported span that is not the quote
    # shifts the index of every real occurrence after it, so a claim that
    # stated its occurrence correctly would then be refused.
    source = "Area of 30 ㎡ per site."
    assert normalize(source) == "Area of 30 m2 per site."
    assert "2 per site" in normalize(source)

    assert _spans_of("2 per site", source) == []
    assert occurrence_count("2 per site", source) == 0

    # Quoted from the glyph's own start, the span exists and is returned.
    spans = _spans_of("m2 per site", source)
    assert len(spans) == 1
    assert normalize(source[slice(*spans[0])]) == "m2 per site"


def test_every_span_reported_really_normalizes_to_the_quote():
    # The promise _spans_of makes, asserted directly rather than inferred.
    text = _v1_text()
    for quote in (
        BOILERPLATE,
        "Large Load Customer",
        "The Utility shall",
        "20 megawatts (MW)",
    ):
        spans = _spans_of(quote, text)
        assert spans, quote
        for start, end in spans:
            assert normalize(text[start:end]) == normalize(quote)


def test_a_unique_quote_needs_no_occurrence_and_still_verifies():
    text = _v1_text()
    result = verify_citation(
        Citation(
            "v1",
            1970,
            2066,
            '"Large Load Customer" means a Customer whose Requested Load equals or exceeds 20 megawatts (MW).',
        ),
        text,
    )
    assert result.verified is True
