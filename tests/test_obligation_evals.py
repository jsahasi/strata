"""Tests for the obligation scorecard, and for what it refuses to say.

Three of these are the load-bearing ones, and they are the three the module was
written around.

**A wrong assertion fails the build.** The system claiming a regulatory duty
that is not there is the catastrophic outcome, so it drives a non-zero exit
code with no threshold to argue about and no quantity of correct answers to
offset it.

**A refusal alone does not.** A missed obligation costs an analyst an
afternoon. It is reported, it makes its own metric read FAIL, and it does not
block a release. The two costs are different, so the two outcomes get different
consequences -- which is the whole reason this scorecard exists beside the one
in app/evals/run.py.

**An empty gold set is refused, never scored.** A scorer that answers "0 of 0,
100 per cent" over no labels is how a green build lies. Every empty shape
raises: no gold set files, no passages, no positive labels, no negative labels.

The rest guard the arithmetic around those three: that the run has to cover the
gold set, that a duty nobody adjudicated is credited to neither column, and
that no single combined score can be got out of this module by any route --
`combined_f1()` raises, and the reason is printed on the page rather than left
in a comment.

Nothing here touches `data/` for writing, opens a socket, or needs an API key.
"""

import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals import obligations as obl
from app.evals.report import MIN_SAMPLE_FOR_RATE

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDSET_DIR = REPO_ROOT / "data" / "evals"
CORPUS_ROOT = REPO_ROOT / "data" / "real"

# The counts the committed gold sets carry, written out longhand rather than
# derived, so a relabelling shows up as a diff in this file rather than as a
# scorecard that silently agrees with whatever the data now says.
TOTAL_PASSAGES = 51
POSITIVE_LABELS = 23
NEGATIVE_LABELS = 28
AMBIGUOUS_LABELS = 17
BY_JURISDICTION = {"GA": 19, "KY": 6, "MO": 13, "UT": 13}

_PERCENT = re.compile(r"\d+(?:\.\d+)?%")


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# A gold set small enough to reason about, written to a temp directory
# ---------------------------------------------------------------------------


def write_goldset(
    tmp_path: Path,
    rows,
    *,
    name: str = "goldset_synth",
    key: str = "passages",
    jurisdictions=("GA",),
    source_file: str = "ga-9999-synthetic-order.txt",
) -> tuple[Path, Path]:
    """Build a gold set whose offsets really do slice out of a real file.

    Returns (goldset directory, corpus directory). The source text is assembled
    from the quotes themselves, so the offsets metric passes for the right
    reason rather than because it was skipped.
    """
    goldset_dir = tmp_path / "evals"
    corpus_dir = tmp_path / "real"
    goldset_dir.mkdir(exist_ok=True)
    corpus_dir.mkdir(exist_ok=True)

    text = ""
    labels = []
    for index, row in enumerate(rows):
        quote = row.get("quote", f"passage {index} shall do the thing.")
        text += "\n\n"
        start = len(text)
        text += quote
        labels.append(
            {
                "id": row.get("id", f"synth-{index:02d}"),
                "source_file": row.get("source_file", source_file),
                "start": start,
                "end": start + len(quote),
                "quote": quote,
                "creates_obligation": row["creates_obligation"],
                "obligation_text": row.get("obligation_text"),
                "who_is_bound": row.get("who_is_bound"),
                "ambiguous": row.get("ambiguous", False),
                "why": row.get("why", "synthetic"),
            }
        )

    (corpus_dir / source_file).write_bytes(text.encode("utf-8"))
    for extra in {row.get("source_file") for row in rows} - {None, source_file}:
        (corpus_dir / extra).write_bytes(text.encode("utf-8"))
    (goldset_dir / f"{name}.json").write_bytes(
        json.dumps(
            {
                "name": name,
                "jurisdictions": list(jurisdictions),
                "known_limits": ["synthetic; nothing here is a real filing"],
                key: labels,
            }
        ).encode("utf-8")
    )
    return goldset_dir, corpus_dir


MIXED = (
    {"id": "s-pos-1", "creates_obligation": True, "obligation_text": "must file X"},
    {"id": "s-pos-2", "creates_obligation": True, "obligation_text": "must file Y"},
    {"id": "s-neg-1", "creates_obligation": False},
    {"id": "s-neg-2", "creates_obligation": False, "ambiguous": True},
)


@pytest.fixture
def synth(tmp_path: Path):
    return write_goldset(tmp_path, MIXED)


def score(goldset_dir: Path, corpus_dir: Path, extractions):
    goldsets = obl.load_goldsets(goldset_dir)
    return obl.score_obligations(goldsets, extractions, corpus_root=corpus_dir)


def asserted(passage_id: str, verdict: str | None = None) -> obl.Extraction:
    return obl.Extraction(
        passage_id=passage_id,
        asserted=True,
        obligation_text="the system's sentence",
        who_is_bound="the Company",
        duty_verdict=verdict,
        source="test",
    )


def refused(passage_id: str) -> obl.Extraction:
    return obl.Extraction(passage_id=passage_id, asserted=False, source="test")


# ---------------------------------------------------------------------------
# 1. A wrong assertion fails the build
# ---------------------------------------------------------------------------


def test_a_wrong_assertion_on_a_hard_negative_drives_exit_code_non_zero(synth):
    """The catastrophic case. One is enough, and there is no threshold."""
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (
            refused("s-pos-1"),
            refused("s-pos-2"),
            asserted("s-neg-1"),  # the gold set says no duty here
            refused("s-neg-2"),
        ),
    )
    assert card.wrong_assertions == 1
    assert card.exit_code == 1
    blocking = card.blocking_failures
    assert [metric.key for metric in blocking] == ["obligation_wrong_assertions"]
    assert len(blocking[0].failures) == 1
    assert "s-neg-1" in blocking[0].failures[0]


def test_asserting_the_wrong_duty_is_also_a_wrong_assertion(synth):
    """A right answer in the wrong words is still a false claim about a duty."""
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (
            asserted("s-pos-1", obl.DUTY_DIFFERENT),
            refused("s-pos-2"),
            refused("s-neg-1"),
            refused("s-neg-2"),
        ),
    )
    outcome = {row.gold.id: row.outcome for row in card.judgements}
    assert outcome["s-pos-1"] == obl.WRONG_ASSERTION
    assert card.exit_code == 1


def test_no_number_of_correct_answers_offsets_one_wrong_assertion(synth):
    """Three right and one wrong still fails. Nothing here averages."""
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (
            asserted("s-pos-1", obl.DUTY_SAME),
            asserted("s-pos-2", obl.DUTY_SAME),
            refused("s-neg-1"),
            asserted("s-neg-2"),
        ),
    )
    assert card.counts[obl.CORRECT_ASSERTION] == 2
    assert card.counts[obl.CORRECT_REFUSAL] == 1
    assert card.wrong_assertions == 1
    assert card.exit_code == 1


def test_the_blocking_metric_is_the_only_blocking_extraction_metric(synth):
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    blocking = {metric.key for metric in card.metrics if metric.blocking}
    assert blocking == {"obligation_wrong_assertions", "goldset_offsets"}


# ---------------------------------------------------------------------------
# 2. A refusal alone does not
# ---------------------------------------------------------------------------


def test_a_refusal_alone_does_not_drive_exit_code_non_zero(synth):
    """Every duty missed, nothing asserted. Reported, red, and not a blocker."""
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    assert card.counts[obl.MISSED] == 2
    assert card.counts[obl.CORRECT_REFUSAL] == 2
    assert card.wrong_assertions == 0
    assert card.exit_code == 0

    misses = next(m for m in card.metrics if m.key == "obligation_misses")
    assert not misses.passed, "a metric with two misses in it must not read PASS"
    assert not misses.blocking
    assert misses not in card.blocking_failures


def test_the_committed_gold_sets_score_with_nothing_asserted():
    """The real evidence, against a build with no extractor. Exit 0, and loud."""
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    passages = obl.all_passages(goldsets)
    card = obl.score_obligations(
        goldsets,
        obl.no_extractor_run(passages),
        corpus_root=CORPUS_ROOT,
        run_note=obl.NO_EXTRACTOR_ANNOUNCEMENT,
    )
    assert card.wrong_assertions == 0
    assert card.exit_code == 0
    assert card.counts[obl.MISSED] == POSITIVE_LABELS
    assert card.counts[obl.CORRECT_REFUSAL] == NEGATIVE_LABELS

    page = _flat(obl.render(card))
    assert "NO EXTRACTOR RAN" in page
    assert "that pass means nothing" in page


def test_the_no_extractor_run_records_a_refusal_rather_than_a_silence():
    """It is a decision with a name on it, not an absence the reader must spot."""
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    run = obl.no_extractor_run(obl.all_passages(goldsets))
    assert len(run) == TOTAL_PASSAGES
    assert all(row.source == obl.SOURCE_NO_EXTRACTOR for row in run)
    assert not any(row.asserted for row in run)


# ---------------------------------------------------------------------------
# 3. An empty gold set is refused, never scored at 100 per cent
# ---------------------------------------------------------------------------


def test_scoring_no_gold_sets_refuses(tmp_path: Path):
    with pytest.raises(obl.EmptyGoldSet) as raised:
        obl.score_obligations((), (), corpus_root=tmp_path)
    assert "green" in str(raised.value)


def test_a_gold_set_with_no_passages_refuses_rather_than_reporting_perfection(
    tmp_path: Path,
):
    """The one that matters. 0 of 0 is not 100 per cent; it is no measurement."""
    goldset_dir, corpus_dir = write_goldset(tmp_path, ())
    goldsets = obl.load_goldsets(goldset_dir)
    with pytest.raises(obl.EmptyGoldSet) as raised:
        obl.score_obligations(goldsets, (), corpus_root=corpus_dir)
    message = str(raised.value)
    assert "nothing to score" in message.casefold()
    assert "100 per cent" in message


def test_an_empty_gold_set_directory_refuses(tmp_path: Path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(obl.EmptyGoldSet):
        obl.load_goldsets(empty)


def test_a_gold_set_with_no_obligations_cannot_measure_a_miss(tmp_path: Path):
    goldset_dir, corpus_dir = write_goldset(
        tmp_path,
        ({"id": "n-1", "creates_obligation": False},),
    )
    with pytest.raises(obl.ScoreRefused) as raised:
        score(goldset_dir, corpus_dir, (refused("n-1"),))
    assert "empty denominator" in str(raised.value)


def test_a_gold_set_with_no_hard_negatives_cannot_measure_a_refusal(tmp_path: Path):
    goldset_dir, corpus_dir = write_goldset(
        tmp_path,
        ({"id": "p-1", "creates_obligation": True, "obligation_text": "must"},),
    )
    with pytest.raises(obl.ScoreRefused) as raised:
        score(goldset_dir, corpus_dir, (refused("p-1"),))
    assert "no negative labels" in str(raised.value)


# ---------------------------------------------------------------------------
# No single combined score, by any route
# ---------------------------------------------------------------------------


def test_combined_f1_raises_rather_than_returning_a_number():
    with pytest.raises(obl.SingleNumberRefused) as raised:
        obl.combined_f1(precision=1.0, recall=1.0)
    assert str(raised.value) == obl.NO_COMBINED_SCORE


def test_the_refusal_to_average_is_printed_on_the_page_not_only_in_the_code(synth):
    """The sentence has to reach a reader who never opens the module."""
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    page = _flat(obl.render(card))
    assert _flat(obl.NO_COMBINED_SCORE) in page
    assert "Averaging them hides the only number that matters" in page
    assert "take the wrong-assertion count" in page


def test_the_page_claims_no_f1_and_no_accuracy(synth):
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    page = _flat(obl.render(card))
    assert "F1 score" not in page
    assert "% accuracy" not in page
    assert "% precision" not in page
    assert "% recall" not in page
    # The words appear only where the page says it will not compute them.
    assert "emits no F1 and no single combined score" in page


def test_the_wrong_assertion_count_is_the_one_number_offered(synth):
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (refused("s-pos-1"), refused("s-pos-2"), asserted("s-neg-1"), refused("s-neg-2")),
    )
    assert card.wrong_assertions == 1
    assert "WRONG ASSERTIONS: 1. Target zero." in obl.render(card)


def test_no_extraction_metric_prints_a_rate(synth):
    """Four jurisdictions is under the floor, so counts and no percentages."""
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    for metric in card.metrics:
        if metric.key == "goldset_offsets":
            continue
        assert metric.sample_size < MIN_SAMPLE_FOR_RATE, metric.key
        assert metric.sample.unit == "jurisdictions"


def test_the_committed_run_prints_one_percentage_and_it_is_the_offsets_check():
    """Whatever percentage reaches the page must carry its sample and its scope."""
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    card = obl.score_obligations(
        goldsets,
        obl.no_extractor_run(obl.all_passages(goldsets)),
        corpus_root=CORPUS_ROOT,
    )
    page = _flat(obl.render(card))
    percentages = _PERCENT.findall(page)
    assert percentages == ["100%"], percentages
    assert "100%, n = 16 source documents over 51 labelled quotes" in page
    assert "The one percentage on this page belongs to the offsets check" in page


# ---------------------------------------------------------------------------
# The taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gold_says, extraction, expected",
    [
        (False, lambda i: asserted(i), obl.WRONG_ASSERTION),
        (True, lambda i: asserted(i, obl.DUTY_DIFFERENT), obl.WRONG_ASSERTION),
        (True, lambda i: refused(i), obl.MISSED),
        (True, lambda i: asserted(i, obl.DUTY_SAME), obl.CORRECT_ASSERTION),
        (False, lambda i: refused(i), obl.CORRECT_REFUSAL),
        (True, lambda i: asserted(i), obl.UNADJUDICATED_ASSERTION),
    ],
)
def test_the_five_outcomes(gold_says: bool, extraction, expected: str):
    gold = obl.GoldPassage(
        id="x-1",
        goldset="synth",
        source_file="ga-1-x.txt",
        jurisdiction="GA",
        start=0,
        end=1,
        quote="x",
        creates_obligation=gold_says,
        obligation_text="must file X" if gold_says else None,
        who_is_bound="the Company" if gold_says else None,
        ambiguous=False,
        why="synthetic",
    )
    assert obl.judge(gold, extraction("x-1")).outcome == expected


def test_an_unadjudicated_assertion_is_credited_to_neither_column(synth):
    """Nobody checked which duty it named, so it is not a win and not a loss."""
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (
            asserted("s-pos-1"),  # no duty_verdict: nobody adjudicated it
            refused("s-pos-2"),
            refused("s-neg-1"),
            refused("s-neg-2"),
        ),
    )
    assert card.counts[obl.UNADJUDICATED_ASSERTION] == 1
    assert card.counts[obl.CORRECT_ASSERTION] == 0
    assert card.wrong_assertions == 0
    assert card.exit_code == 0, "an unchecked assertion is not established as wrong"

    misses = next(m for m in card.metrics if m.key == "obligation_misses")
    assert misses.hits == 0, "an assertion nobody checked must not be credited"
    assert any("unadjudicated" in note for note in misses.notes)


def test_every_outcome_is_counted_even_at_zero(synth):
    """An outcome missing from a table reads as an outcome that cannot happen."""
    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    assert set(card.counts) == set(obl.OUTCOMES)
    page = _flat(obl.render(card))
    for label in ("WRONG ASSERTION", "MISSED", "CORRECT ASSERTION", "CORRECT REFUSAL"):
        assert label in page


def test_the_outcomes_partition_the_gold_set(synth):
    goldset_dir, corpus_dir = synth
    card = score(
        goldset_dir,
        corpus_dir,
        (
            asserted("s-pos-1", obl.DUTY_SAME),
            asserted("s-pos-2"),
            asserted("s-neg-1"),
            refused("s-neg-2"),
        ),
    )
    assert sum(card.counts.values()) == len(card.judgements) == len(MIXED)


# ---------------------------------------------------------------------------
# The run has to cover the gold set
# ---------------------------------------------------------------------------


def test_a_run_silent_about_a_passage_is_refused(synth):
    """Silence is not a refusal. A refusal is a decision the system made."""
    goldset_dir, corpus_dir = synth
    with pytest.raises(obl.ScoreRefused) as raised:
        score(
            goldset_dir,
            corpus_dir,
            (refused("s-pos-1"), refused("s-neg-1"), refused("s-neg-2")),
        )
    message = str(raised.value)
    assert "s-pos-2" in message
    assert "silence is a decision nobody made" in message


def test_a_run_naming_a_passage_nobody_labelled_is_refused(synth):
    goldset_dir, corpus_dir = synth
    with pytest.raises(obl.ScoreRefused) as raised:
        score(
            goldset_dir,
            corpus_dir,
            tuple(refused(row["id"]) for row in MIXED) + (refused("s-invented"),),
        )
    assert "s-invented" in str(raised.value)


def test_a_run_reporting_one_passage_twice_is_refused(synth):
    goldset_dir, corpus_dir = synth
    with pytest.raises(obl.ScoreRefused) as raised:
        score(
            goldset_dir,
            corpus_dir,
            tuple(refused(row["id"]) for row in MIXED) + (refused("s-neg-1"),),
        )
    assert "double-count" in str(raised.value)


def test_an_unrecognised_duty_verdict_is_refused():
    with pytest.raises(obl.ScoreRefused) as raised:
        obl.Extraction(passage_id="x", asserted=True, duty_verdict="probably")
    assert "nobody adjudicated it" in str(raised.value)


def test_a_refusal_carrying_a_duty_verdict_is_refused():
    with pytest.raises(obl.ScoreRefused):
        obl.Extraction(passage_id="x", asserted=False, duty_verdict=obl.DUTY_SAME)


# ---------------------------------------------------------------------------
# The gold sets themselves
# ---------------------------------------------------------------------------


def test_the_committed_gold_sets_load_and_carry_the_counts_written_here():
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    assert [g.name for g in goldsets] == ["goldset_ga_ky", "goldset_ut_mo"]
    passages = obl.all_passages(goldsets)
    assert len(passages) == TOTAL_PASSAGES
    assert sum(1 for p in passages if p.creates_obligation) == POSITIVE_LABELS
    assert sum(1 for p in passages if not p.creates_obligation) == NEGATIVE_LABELS
    assert sum(1 for p in passages if p.ambiguous) == AMBIGUOUS_LABELS


def test_both_gold_set_file_shapes_are_read():
    """One file lists its labels under 'passages', the other under 'labels'."""
    ga_ky = json.loads((GOLDSET_DIR / "goldset_ga_ky.json").read_bytes())
    ut_mo = json.loads((GOLDSET_DIR / "goldset_ut_mo.json").read_bytes())
    assert "passages" in ga_ky and "labels" not in ga_ky
    assert "labels" in ut_mo and "passages" not in ut_mo
    assert len(obl.load_goldset(GOLDSET_DIR / "goldset_ga_ky.json").passages) == 25
    assert len(obl.load_goldset(GOLDSET_DIR / "goldset_ut_mo.json").passages) == 26


def test_a_file_holding_its_labels_under_both_keys_is_refused(tmp_path: Path):
    path = tmp_path / "goldset_both.json"
    path.write_bytes(
        json.dumps({"name": "both", "passages": [], "labels": []}).encode("utf-8")
    )
    with pytest.raises(obl.ScoreRefused) as raised:
        obl.load_goldset(path)
    assert "which list is the gold set" in str(raised.value)


def test_a_label_missing_a_field_is_refused(tmp_path: Path):
    path = tmp_path / "goldset_short.json"
    path.write_bytes(
        json.dumps(
            {
                "name": "short",
                "passages": [{"id": "a", "source_file": "ga-1-x.txt", "start": 0}],
            }
        ).encode("utf-8")
    )
    with pytest.raises(obl.ScoreRefused) as raised:
        obl.load_goldset(path)
    assert "missing" in str(raised.value)


def test_a_passage_whose_state_contradicts_its_gold_set_is_refused(tmp_path: Path):
    goldset_dir, _corpus = write_goldset(
        tmp_path,
        ({"id": "wrong-state", "creates_obligation": True, "source_file": "mo-1-x.txt"},),
        jurisdictions=("GA",),
    )
    with pytest.raises(obl.ScoreRefused) as raised:
        obl.load_goldsets(goldset_dir)
    assert "skew the per-jurisdiction breakdown" in str(raised.value)


def test_an_unplaceable_jurisdiction_refuses_rather_than_guessing():
    with pytest.raises(obl.ScoreRefused) as raised:
        obl.jurisdiction_of("atlantis-1-order.txt")
    assert "bucketed under a guess" in str(raised.value)


@pytest.mark.parametrize(
    "name, code",
    [("GA", "GA"), ("ga", "GA"), ("Georgia", "GA"), ("Kentucky", "KY"), ("mo", "MO")],
)
def test_declared_jurisdictions_normalise_to_two_letter_codes(name: str, code: str):
    assert obl.normalise_jurisdiction(name) == code


# ---------------------------------------------------------------------------
# The gold set's own offsets
# ---------------------------------------------------------------------------


def test_every_labelled_quote_still_slices_out_of_its_source_file():
    """The oracle is checkable with plain slicing, not with the product."""
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    for passage in obl.all_passages(goldsets):
        text = (CORPUS_ROOT / passage.source_file).read_bytes().decode("utf-8")
        assert text[passage.start : passage.end] == passage.quote, passage.id


def test_a_moved_offset_is_a_release_blocker(tmp_path: Path):
    goldset_dir, corpus_dir = write_goldset(tmp_path, MIXED)
    source = corpus_dir / "ga-9999-synthetic-order.txt"
    source.write_bytes(("shifted " + source.read_bytes().decode("utf-8")).encode())

    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    offsets = next(m for m in card.metrics if m.key == "goldset_offsets")
    assert not offsets.passed
    assert offsets.blocking
    assert card.exit_code == 1
    assert "no longer slices" in offsets.failures[0]


def test_a_missing_source_file_is_a_blocker_rather_than_a_skip(tmp_path: Path):
    goldset_dir, corpus_dir = write_goldset(tmp_path, MIXED)
    (corpus_dir / "ga-9999-synthetic-order.txt").unlink()
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    offsets = next(m for m in card.metrics if m.key == "goldset_offsets")
    assert not offsets.passed
    assert card.exit_code == 1
    assert "could not be re-read" in offsets.failures[0]


# ---------------------------------------------------------------------------
# What the page says, and in what order
# ---------------------------------------------------------------------------


def test_the_ambiguous_share_is_stated_before_any_score():
    page = _rendered_committed_page()
    assert "AMBIGUOUS SHARE" in page
    assert f"{AMBIGUOUS_LABELS} of {TOTAL_PASSAGES} labelled passages are marked" in page
    assert page.index("AMBIGUOUS SHARE") < page.index("1. WRONG ASSERTIONS"), (
        "a caveat printed after the number is a caveat the reader has already "
        "decided not to need"
    )


def test_the_ambiguous_share_says_it_is_not_a_measured_disagreement_rate():
    page = _rendered_committed_page()
    assert "NOT a measured inter-annotator disagreement rate" in _flat(page)


def test_counts_are_reported_per_jurisdiction():
    page = _flat(_rendered_committed_page())
    assert "BY JURISDICTION" in page
    for code, count in BY_JURISDICTION.items():
        assert f"{code}: {count} passages" in page


def test_the_labellers_own_limits_are_printed_verbatim():
    page = _flat(_rendered_committed_page())
    for goldset in obl.load_goldsets(GOLDSET_DIR):
        for limit in goldset.known_limits:
            assert _flat(limit) in page


def test_the_page_stays_inside_eighty_columns():
    """A scorecard that wraps in a reviewer's terminal is harder to read."""
    for line in _rendered_committed_page().splitlines():
        assert len(line) <= obl.WIDTH or line.strip().startswith("goldset_"), line


def test_the_page_is_ascii():
    """Not our reviewer's terminal to choose. Same rule app/evals/run.py keeps."""
    _rendered_committed_page().encode("ascii")


def _rendered_committed_page() -> str:
    goldsets = obl.load_goldsets(GOLDSET_DIR)
    card = obl.score_obligations(
        goldsets,
        obl.no_extractor_run(obl.all_passages(goldsets)),
        corpus_root=CORPUS_ROOT,
        run_note=obl.NO_EXTRACTOR_ANNOUNCEMENT,
    )
    return obl.render(card)


# ---------------------------------------------------------------------------
# Predictions on disk, and the command line
# ---------------------------------------------------------------------------


def _predictions(tmp_path: Path, rows) -> Path:
    path = tmp_path / "predictions.json"
    path.write_bytes(
        json.dumps({"run": "test", "extractions": rows}).encode("utf-8")
    )
    return path


def test_a_predictions_file_round_trips(tmp_path: Path, synth):
    goldset_dir, corpus_dir = synth
    path = _predictions(
        tmp_path,
        [
            {"id": "s-pos-1", "asserted": True, "obligation_text": "must file X",
             "duty_verdict": "same"},
            {"id": "s-pos-2", "asserted": False},
            {"id": "s-neg-1", "asserted": False},
            {"id": "s-neg-2", "asserted": False},
        ],
    )
    card = score(goldset_dir, corpus_dir, obl.load_predictions(path))
    assert card.counts[obl.CORRECT_ASSERTION] == 1
    assert card.counts[obl.MISSED] == 1
    assert card.exit_code == 0


def test_a_predictions_file_with_no_extractions_list_is_refused(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_bytes(json.dumps({"run": "x"}).encode("utf-8"))
    with pytest.raises(obl.ScoreRefused):
        obl.load_predictions(path)


def test_the_command_line_refuses_when_no_run_was_given(capsys, synth):
    """No predictions is not an empty run. It is nothing to score."""
    goldset_dir, corpus_dir = synth
    code = obl.main(["--goldsets", str(goldset_dir), "--corpus", str(corpus_dir)])
    assert code == 1
    assert "Nothing to score" in capsys.readouterr().out


def test_the_command_line_exits_one_on_a_wrong_assertion(capsys, tmp_path, synth):
    goldset_dir, corpus_dir = synth
    path = _predictions(
        tmp_path,
        [
            {"id": "s-pos-1", "asserted": False},
            {"id": "s-pos-2", "asserted": False},
            {"id": "s-neg-1", "asserted": True, "obligation_text": "invented duty"},
            {"id": "s-neg-2", "asserted": False},
        ],
    )
    code = obl.main(
        [
            "--goldsets", str(goldset_dir),
            "--corpus", str(corpus_dir),
            "--predictions", str(path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "WRONG ASSERTIONS: 1. Target zero." in out
    assert "RELEASE BLOCKER" in out


def test_the_command_line_exits_zero_on_refusals_alone(capsys, tmp_path, synth):
    goldset_dir, corpus_dir = synth
    path = _predictions(
        tmp_path, [{"id": row["id"], "asserted": False} for row in MIXED]
    )
    code = obl.main(
        [
            "--goldsets", str(goldset_dir),
            "--corpus", str(corpus_dir),
            "--predictions", str(path),
        ]
    )
    assert code == 0
    assert "WRONG ASSERTIONS: 0. Target zero." in capsys.readouterr().out


def test_the_command_line_prints_a_refusal_rather_than_a_traceback(capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = obl.main(["--goldsets", str(empty), "--no-extractor"])
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSED" in out
    assert "No score was computed" in out


def test_the_no_extractor_flag_runs_against_the_committed_sets(capsys):
    assert obl.main(["--no-extractor"]) == 0
    out = capsys.readouterr().out
    assert "NO EXTRACTOR RAN" in _flat(out)
    assert "MISSED" in out


# ---------------------------------------------------------------------------
# It runs where `make eval` runs
# ---------------------------------------------------------------------------


def test_scoring_opens_no_socket(monkeypatch, synth):
    """Break the socket module, then score. Nothing should notice."""

    def refuse(*args, **kwargs):
        raise AssertionError("the obligation scorer opened a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    goldset_dir, corpus_dir = synth
    card = score(goldset_dir, corpus_dir, tuple(refused(row["id"]) for row in MIXED))
    assert card.exit_code == 0
    assert obl.render(card)


def test_the_scorer_imports_no_model_client_and_no_database_engine():
    """Checked in a fresh process, because this test's own imports are noisy."""
    probe = "import sys, app.evals.obligations; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(result.stdout.split())
    forbidden = {
        "anthropic",
        "openai",
        "requests",
        "httpx",
        "urllib.request",
        "http.client",
        "app.state.db",
    }
    assert not (loaded & forbidden), sorted(loaded & forbidden)


def test_make_eval_still_prints_five_metrics_and_this_module_is_not_in_it():
    """This scorecard is deliberately NOT wired into `make eval`.

    `make eval` scores the deterministic spine against data/manifest.json and
    prints five metrics; tests/test_evals.py pins that. Adding a sixth would
    change a number a reviewer has been told to expect, so this module ships
    with its own entry point and says so.
    """
    from app.evals.metrics import ALL_METRICS

    assert len(ALL_METRICS) == 5
    assert not any("obligation" in metric.__name__ for metric in ALL_METRICS)
