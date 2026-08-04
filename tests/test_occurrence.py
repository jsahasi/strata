import json
from pathlib import Path

from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    Citation,
    occurrence_count,
    occurrence_index,
    verify_citation,
)

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
