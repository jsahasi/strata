import json
from pathlib import Path

from app.diff.engine import (
    RESTRUCTURE_CONFIDENCE_CEILING,
    Change,
    PassageRef,
    diff,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def _manifest():
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def _refs(version_id: str, chunks: list[tuple[int, str]]) -> list[PassageRef]:
    return [PassageRef(version_id, s, s + len(t), t) for s, t in chunks]


def test_identical_inputs_produce_no_changes():
    refs = _refs("v1", [(0, "alpha"), (10, "beta")])
    other = _refs("v2", [(0, "alpha"), (10, "beta")])
    assert diff(refs, other) == []


def test_is_deterministic_across_repeated_calls():
    before = _refs("v1", [(0, "alpha"), (10, "beta")])
    after = _refs("v2", [(0, "alpha"), (10, "gamma")])
    assert diff(before, after) == diff(before, after)


def test_finds_a_modified_passage_with_refs_on_both_sides():
    before = _refs("v1", [(0, "the threshold is 20 megawatts")])
    after = _refs("v2", [(0, "the threshold is 10 megawatts")])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == "modified"
    assert changes[0].before.text == "the threshold is 20 megawatts"
    assert changes[0].after.text == "the threshold is 10 megawatts"


def test_finds_an_addition_present_only_on_the_after_side():
    before = _refs("v1", [(0, "alpha")])
    after = _refs("v2", [(0, "alpha"), (10, "a wholly new obligation")])
    changes = diff(before, after)
    assert [c.change_type for c in changes] == ["added"]
    assert changes[0].before is None
    assert changes[0].after.text == "a wholly new obligation"


def test_finds_a_removal_present_only_on_the_before_side():
    before = _refs("v1", [(0, "alpha"), (10, "struck provision")])
    after = _refs("v2", [(0, "alpha")])
    changes = diff(before, after)
    assert [c.change_type for c in changes] == ["removed"]
    assert changes[0].after is None


def test_ignores_a_difference_that_normalization_folds():
    before = _refs("v1", [(0, "the  Utility’s system")])
    after = _refs("v2", [(0, "the Utility's system")])
    assert diff(before, after) == []


def test_detects_the_corpus_deadline_move_at_sub_sentence_granularity():
    # CHG-3: one date token moves inside an otherwise identical sentence.
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-3")
    before = _refs("v1", [(change["before"]["start"], change["before"]["exact_text"])])
    after = _refs("v2", [(change["after"]["start"], change["after"]["exact_text"])])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].change_type == "modified"
    assert "March 1, 2027" in changes[0].before.text
    assert "June 1, 2027" in changes[0].after.text


def test_wholesale_restructure_reports_low_alignment_confidence():
    # CHG-5: Section 6 becomes Section 5.4. Naive alignment sees delete+add.
    # The design does not claim to solve this; it must flag it rather than guess.
    manifest = _manifest()
    change = next(c for c in manifest["changes"] if c["id"] == "CHG-5")
    before = _refs("v2", [(change["before"]["start"], change["before"]["exact_text"])])
    after = _refs("v3", [(change["after"]["start"], change["after"]["exact_text"])])
    changes = diff(before, after)
    assert changes, "restructure must produce at least one change, never silence"
    assert any(c.alignment_confidence < 0.9 for c in changes)


def test_high_text_similarity_does_not_rescue_a_changed_section_number():
    # The regression guard for the defect this test file first exposed.
    # CHG-5's two passages are 94% textually identical, so similarity alone
    # reported a confident match on the one case ADR-004 says must escalate.
    # A renumbering is dangerous precisely BECAUSE the words barely move.
    before = _refs("v2", [(0, "SECTION 6. COLLATERAL. A customer shall post collateral.")])
    after = _refs("v3", [(0, "5.4 Collateral. A customer shall post collateral.")])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].alignment_confidence <= RESTRUCTURE_CONFIDENCE_CEILING


def test_same_section_number_keeps_its_true_similarity_score():
    # The cap must apply only to renumbering. An ordinary edit within a section
    # that keeps its number must still report how close the two versions are,
    # or every routine amendment would escalate and the queue becomes useless.
    before = _refs("v2", [(0, "5.2 Allocation. The Utility shall allocate 100% of costs.")])
    after = _refs("v3", [(0, "5.2 Allocation. The Utility shall allocate 50% of costs.")])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].alignment_confidence > RESTRUCTURE_CONFIDENCE_CEILING


def test_prose_without_a_section_number_is_not_penalised():
    # Recitals and unnumbered prose have no label to disagree about. They must
    # score on text alone rather than being treated as a restructure.
    before = _refs("v2", [(0, "The Commission gives notice of this proposed rulemaking.")])
    after = _refs("v3", [(0, "The Commission gives notice of this final rulemaking.")])
    changes = diff(before, after)
    assert len(changes) == 1
    assert changes[0].alignment_confidence > RESTRUCTURE_CONFIDENCE_CEILING
