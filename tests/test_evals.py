"""Tests for the eval harness -- including tests on how it is allowed to talk.

Three things are asserted here, and the third is the unusual one.

**It runs offline.** No model, no socket, no database. A reviewer executes
`make eval` on a clean checkout with no API key, so any dependency that would
break that has to break a test first.

**It reports the counts the current code earns.** The expected numbers are
written out longhand rather than derived from the harness, so a change in
behaviour shows up as a diff in this file rather than as a scorecard that
silently agrees with whatever the code now does.

**It refuses false precision.** The corpus labels five changes. A percentage
over five items is a claim the evidence cannot carry, and this repository
exists to catch claims that assert more than their evidence supports. So there
is a test that reads the rendered scorecard the way a reader would, finds every
percentage in it, and fails unless each one prints its sample size beside it and
that sample reaches ten. A future metric that prints "100% recall" over the five
changes fails here, not in review.

That test passed while the scorecard printed "20 of 20 manifest offsets verify
[100%, n = 20 offsets]", because the harness handed it 20 and 20 clears the
floor. Nine of those twenty offsets quote the same boilerplate sentence. The
sample is now derived by de-duplicating the evidence rather than declared by the
metric, so the tests below pin the derivation as well as the printed line: what
identity folds two rows into one, that a `Metric` has no slot to be handed a
sample size through, and that the floor still lets a real sample through.

The failure paths are exercised against a copy of the corpus in a temp
directory. Nothing in this file writes to `data/`.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals import run
from app.evals.corpus import Corpus
from app.evals.metrics import ALL_METRICS, FABRICATION_FROM, Metric
from app.evals.report import (
    CAVEAT,
    MIN_SAMPLE_FOR_RATE,
    RateRefused,
    Sample,
    count_phrase,
    rate,
)

# The counts the current code earns against the committed corpus. Written out
# rather than computed, so a regression appears as a diff in this file.
#
# Read the third column against the second. Every metric here gathers more rows
# of evidence than it has independent samples, and the gap is the point: 20
# offsets over 6 subjects, 27 probes over 9 spans, 7 checks over 1 change. Not
# one of them reaches ten, so not one of them prints a rate.
EXPECTED = {
    "citation_verification": (20, 20, 6, "subjects over 20 recorded offsets"),
    "corruption_rejection": (2, 2, 2, "probes"),
    "diff_completeness": (5, 5, 5, "changes"),
    "occurrence_disambiguation": (27, 27, 9, "spans over 27 probes"),
    "draft_final_routing": (7, 7, 1, "change (CHG-4) over 7 routing checks"),
}

_PERCENT = re.compile(r"\d+(?:\.\d+)?%")
_RATE_WITH_SAMPLE = re.compile(r"\d+(?:\.\d+)?%,\s*n\s*=\s*(\d+)\b")


def _flat(text: str) -> str:
    """Whitespace-normalised, so an assertion survives the 80-column wrapping."""
    return " ".join(text.split())


@pytest.fixture
def card(data_dir: Path):
    return run.score(Corpus.load(data_dir))


@pytest.fixture
def output(card) -> str:
    return run.render(card)


@pytest.fixture
def corpus_copy(tmp_path: Path, data_dir: Path) -> Path:
    """A writable copy of the corpus. The committed one is never touched."""
    target = tmp_path / "corpus"
    target.mkdir()
    shutil.copy(data_dir / "manifest.json", target / "manifest.json")
    for text_file in sorted(data_dir.glob("*.txt")):
        shutil.copy(text_file, target / text_file.name)
    return target


def _edit_manifest(corpus_dir: Path, mutate) -> None:
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    mutate(manifest)
    (corpus_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The oracle is independent of the code it scores
# ---------------------------------------------------------------------------


def test_every_manifest_offset_slices_to_its_quoted_text(data_dir: Path):
    """The oracle is checkable with plain Python, not with the product.

    If this needed the verifier to pass, the scorecard's first metric would be
    the code agreeing with itself. It needs only string slicing, so the
    citation-verification metric measures the verifier against an answer that
    was computed without it.
    """
    corpus = Corpus.load(data_dir)
    sites = corpus.citation_sites()
    assert len(sites) == 20
    for site in sites:
        raw = corpus.text(site.version_id)
        assert raw[site.start : site.end] == site.quoted_text, site.label


def test_missing_manifest_refuses_rather_than_scoring_an_empty_corpus(tmp_path: Path):
    """A harness with nothing to score must say so, not pass with zero items."""
    with pytest.raises(FileNotFoundError) as caught:
        Corpus.load(tmp_path)
    assert "manifest.json" in str(caught.value)


# ---------------------------------------------------------------------------
# The counts
# ---------------------------------------------------------------------------


def test_scorecard_passes_against_the_committed_corpus(card):
    assert card.exit_code == 0
    assert card.failures == ()
    assert card.blocking_failures == ()
    assert len(card.metrics) == len(ALL_METRICS) == 5


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_expected_counts(card, key: str):
    metric = next(m for m in card.metrics if m.key == key)
    hits, total, sample_size, sample_unit = EXPECTED[key]
    assert (metric.hits, metric.total) == (hits, total)
    assert metric.sample_size == sample_size
    assert metric.sample_unit == sample_unit
    assert metric.failures == ()
    assert metric.passed


def test_all_five_metrics_block_a_release(card):
    """Each of the five is a gate. `make eval` is usable in CI only if it is."""
    assert [m.blocking for m in card.metrics] == [True] * 5


def test_every_metric_states_what_it_measures_how_and_its_threshold(card):
    """A score without those three is a number nobody can argue with."""
    for metric in card.metrics:
        for field in (metric.measures, metric.method, metric.threshold):
            assert field.strip()
        assert metric.unit.strip() and metric.sample_unit.strip()


def test_counts_are_printed_with_their_denominators(card, output: str):
    flat = _flat(output)
    for metric in card.metrics:
        expected = count_phrase(
            metric.hits,
            metric.total,
            metric.unit,
            sample_size=metric.sample_size,
            sample_unit=metric.sample_unit,
        )
        assert _flat(expected) in flat
        assert f"{metric.hits} of {metric.total}" in flat


def test_the_five_labelled_changes_are_reported_as_five(output: str):
    assert "5 of 5 labelled changes found" in _flat(output)
    assert "n = 5 changes; no rate" in _flat(output)


def test_the_corpus_digests_bind_the_verdict_to_the_bytes(card, output: str, data_dir):
    """Section 28: a verdict belongs to a version, so name the version."""
    assert len(card.versions) == 3
    for version_id, _label, _status, digest in card.versions:
        assert digest in output
        raw = Corpus.load(data_dir).text(version_id).encode("utf-8")
        assert digest == hashlib.sha256(raw).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The honesty rule
# ---------------------------------------------------------------------------


def test_prints_no_percentage_over_a_denominator_under_ten(output: str):
    """Read the scorecard the way a reader does and audit every percentage.

    A percentage is allowed only where the sample reaches ten, and only when the
    sample size is printed on the same line. Anyone quoting the number then
    carries its denominator with it.
    """
    flat = _flat(output)
    with_sample = {m.start(): int(m.group(1)) for m in _RATE_WITH_SAMPLE.finditer(flat)}
    for match in _PERCENT.finditer(flat):
        sample = with_sample.get(match.start())
        assert sample is not None, (
            f"{match.group(0)} is printed with no sample size beside it: "
            f"...{flat[max(0, match.start() - 70):match.end() + 20]}..."
        )
        assert sample >= MIN_SAMPLE_FOR_RATE, (
            f"{match.group(0)} is printed over a sample of {sample}"
        )


def test_no_recall_or_precision_figure_is_claimed(output: str):
    flat = _flat(output)
    assert "% recall" not in flat
    assert "% precision" not in flat
    # The words appear only where the scorecard says it cannot compute them.
    assert "Any recall figure -- and precision cannot be computed at all" in flat


def test_rate_refuses_a_small_sample_rather_than_softening():
    """A fallback that returns 'n/a' hides the refusal. This one raises."""
    with pytest.raises(RateRefused) as caught:
        rate(5, 5, sample_size=5)
    assert "5 of 5" in str(caught.value)
    with pytest.raises(RateRefused):
        rate(1, 1, sample_size=MIN_SAMPLE_FOR_RATE - 1)
    assert rate(10, 10, sample_size=MIN_SAMPLE_FOR_RATE) == "100%"


def test_rate_is_floored_so_nothing_short_of_all_prints_as_all():
    assert rate(26, 27, sample_size=27) == "96.2%"
    assert rate(2999, 3000, sample_size=3000) == "99.9%"
    assert rate(3000, 3000, sample_size=3000) == "100%"


def test_count_phrase_prints_no_symbol_below_the_floor():
    small = count_phrase(5, 5, "changes found", sample_size=5, sample_unit="changes")
    assert "%" not in small
    assert "5 of 5 changes found" in small
    assert f"n = 5 changes; no rate, n < {MIN_SAMPLE_FOR_RATE}" in small

    large = count_phrase(20, 20, "offsets", sample_size=20, sample_unit="offsets")
    assert "20 of 20 offsets" in large
    assert "100%, n = 20 offsets" in large


def test_the_caveat_is_always_printed_and_its_numbers_match_the_corpus(
    card, output, data_dir: Path
):
    """Prose in the caveat is a claim too, so it is checked against the counts."""
    flat_output, flat_caveat = _flat(output), _flat(CAVEAT)
    assert flat_caveat in flat_output

    largest = max(metric.sample_size for metric in card.metrics)
    changes = next(m for m in card.metrics if m.key == "diff_completeness").sample_size
    assert f"largest independent sample here is {largest} spans" in flat_caveat
    assert f"change detection rests on {changes}" in flat_caveat
    assert "withholds a rate below a sample of ten" in flat_caveat
    assert "No model runs here and no network call is made" in flat_caveat

    # The caveat now carries the three offset numbers. Check each against the
    # corpus, because a caveat quoting a stale number is worse than none.
    sites = Corpus.load(data_dir).citation_sites()
    offsets = next(m for m in card.metrics if m.key == "citation_verification")
    assert f"re-reads {len(sites)} recorded offsets" in flat_caveat
    assert f"all {len(sites)} verify" in flat_caveat
    assert f"readings of {offsets.sample_size} subjects" in flat_caveat
    assert (
        f"{len({site.quoted_text for site in sites})} distinct quoted strings "
        "at the most generous count" in flat_caveat
    )
    assert "it prints no rate at all" in flat_caveat


# ---------------------------------------------------------------------------
# The sample is derived, not declared
# ---------------------------------------------------------------------------


def _metric_fields(hits: int = 1, total: int = 1) -> dict:
    """Everything a Metric needs except the sample. Prose kept short on purpose."""
    return {
        "key": "probe",
        "title": "Probe",
        "measures": "nothing",
        "method": "nothing",
        "threshold": "nothing",
        "hits": hits,
        "total": total,
        "unit": "things",
    }


def _metric(sample: Sample, hits: int = 1, total: int = 1) -> Metric:
    return Metric(**_metric_fields(hits, total), sample=sample)


def test_a_sample_counts_distinct_evidence_not_probes():
    """Twenty readings of six subjects are six samples, not twenty."""
    sample = Sample(
        unit="subjects",
        probe_unit="readings",
        identities=("a", "a", "a", "b", "c"),
    )
    assert sample.probes == 5
    assert sample.size == 3
    assert "3" not in sample.phrase  # the phrase names units, the size is separate
    assert sample.phrase == "subjects over 5 readings"


def test_a_sample_with_nothing_repeated_does_not_pad_its_phrase():
    sample = Sample(unit="changes", probe_unit="changes", identities=("a", "b"))
    assert sample.size == sample.probes == 2
    assert sample.phrase == "changes"


def test_a_sample_built_from_no_evidence_is_refused():
    """0 of 0 reads as a pass, so an empty corpus must never reach the page."""
    with pytest.raises(ValueError) as caught:
        Sample(unit="offsets", probe_unit="offsets", identities=())
    assert "no evidence" in str(caught.value)
    assert "has not passed; it has not run" in str(caught.value)


def test_a_metric_cannot_be_handed_its_own_sample_size():
    """The number that licenses a rate may not arrive from a caller.

    This is the whole fix. A call site that could pass 20 could pass it again
    next time, and the rule would hold everywhere except where breaking it
    bought a better number. So Metric is constructed directly here rather than
    through the helper: the constructor is the thing under test.
    """
    one = Sample(unit="offsets", probe_unit="offsets", identities=("a",))

    with pytest.raises(TypeError):
        Metric(**_metric_fields(), sample=one, sample_size=20)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Metric(**_metric_fields(), sample=one, sample_unit="offsets")  # type: ignore[call-arg]

    # And the derived ones cannot be written over after the fact either.
    metric = Metric(**_metric_fields(), sample=one)
    with pytest.raises(AttributeError):
        metric.sample_size = 20  # type: ignore[misc]


def test_a_metric_derives_its_sample_size_from_the_evidence():
    metric = _metric(
        Sample(unit="subjects", probe_unit="offsets", identities=("a", "a", "b"))
    )
    assert metric.sample_size == 2
    assert metric.sample_unit == "subjects over 3 offsets"


# ---------------------------------------------------------------------------
# The offsets scorecard, which used to print a rate it had not earned
# ---------------------------------------------------------------------------


def test_nine_copies_of_one_sentence_are_one_sample(data_dir: Path):
    """The corpus says what each offset is about, and nine say the same thing."""
    corpus = Corpus.load(data_dir)
    sites = corpus.citation_sites()
    assert len(sites) == 20

    subjects = [site.subject for site in sites]
    assert len(set(subjects)) == 6
    assert subjects.count(corpus.boilerplate_subject) == 9

    # The more generous reading -- every distinct string a separate sample --
    # only reaches ten, which ties the floor rather than clearing it.
    assert len({site.quoted_text for site in sites}) == 10


def test_the_offsets_metric_reports_a_count_and_refuses_a_rate(card, output: str):
    """Twenty offsets over six subjects cannot carry a percentage.

    The old line read '20 of 20 manifest offsets verify [100%, n = 20 offsets]'.
    Nine of those twenty quote one sentence, so twenty was never the sample.
    """
    metric = next(m for m in card.metrics if m.key == "citation_verification")
    assert (metric.hits, metric.total) == (20, 20)
    assert metric.sample_size == 6
    assert metric.sample.probes == 20

    flat = _flat(output)
    assert "20 of 20 manifest offsets verify" in flat
    assert "100%, n = 20 offsets" not in flat
    assert (
        "20 of 20 manifest offsets verify "
        "[n = 6 subjects over 20 recorded offsets; "
        f"no rate, n < {MIN_SAMPLE_FOR_RATE}]"
    ) in flat


def test_the_raw_count_and_the_distinct_count_are_printed_together(output: str):
    """Do not hide the number. Twenty offsets verifying is a real result."""
    flat = _flat(output)
    assert "20 recorded offsets" in flat
    assert "10 distinct quoted strings" in flat
    assert "6 subjects" in flat


def test_no_metric_reaches_the_floor_so_no_percentage_is_printed(card, output: str):
    """Nothing this corpus measures earns a rate, and the page prints none.

    Written as an implication rather than a ban on percentages. Grow the corpus
    until some metric holds ten independent samples and the first assertion
    fails first -- which is a prompt to revisit the second, not a rule against
    ever printing a rate.
    """
    over = [m.key for m in card.metrics if m.sample_size >= MIN_SAMPLE_FOR_RATE]
    assert over == [], over
    assert _PERCENT.findall(_flat(output)) == []


def test_a_rate_still_prints_when_the_evidence_really_is_independent():
    """The fix deflates a sample. It does not switch rates off.

    Ten distinct subjects clear the floor and print a rate, so a harness that
    grows a real corpus is not stuck reporting counts forever.
    """
    sample = Sample(
        unit="subjects",
        probe_unit="offsets",
        identities=tuple(f"s{n}" for n in range(10)) + ("s0", "s0"),
    )
    assert sample.size == MIN_SAMPLE_FOR_RATE
    metric = _metric(sample, hits=12, total=12)
    phrase = count_phrase(
        metric.hits,
        metric.total,
        metric.unit,
        sample_size=metric.sample_size,
        sample_unit=metric.sample_unit,
    )
    assert phrase == "12 of 12 things  [100%, n = 10 subjects over 12 offsets]"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_clean_run_exits_zero(data_dir: Path, capsys):
    assert run.main(["--data", str(data_dir)]) == 0
    printed = capsys.readouterr().out
    assert "RELEASE BLOCKER" not in printed
    assert "VERDICT: 5 of 5 metrics pass." in printed


def test_a_drifted_offset_is_a_release_blocker_in_those_words(corpus_copy, capsys):
    """Move one manifest offset by a single character. Everything must stop."""

    def shift_chg1(manifest):
        for change in manifest["changes"]:
            if change["id"] == "CHG-1":
                change["before"]["start"] += 1

    _edit_manifest(corpus_copy, shift_chg1)

    assert run.main(["--data", str(corpus_copy)]) == 1
    printed = _flat(capsys.readouterr().out)
    assert "1. CITATION VERIFICATION FAIL" in printed
    assert (
        "RELEASE BLOCKER: Citation verification did not clear its threshold."
        in printed
    )
    assert "Do not ship this." in printed
    assert "RELEASE BLOCKER: Citation verification." in printed
    assert "Exit 1. This is a gate, not a report." in printed
    assert "VERDICT: 4 of 5 metrics pass." in printed


def test_a_corruption_probe_that_no_longer_corrupts_fails_rather_than_passes(
    corpus_copy, capsys
):
    """The probe alters one numeral inside a real quote. Take the numeral away.

    A probe that quietly becomes a no-op is worse than no probe: it reports a
    pass for a check nobody ran. Same-length substitution, so every other offset
    in the corpus stays where it was and only this metric moves.
    """
    swap = FABRICATION_FROM.replace("50%", "5O%")
    assert len(swap) == len(FABRICATION_FROM)

    def rewrite_chg1(manifest):
        for change in manifest["changes"]:
            if change["id"] == "CHG-1":
                change["after"]["exact_text"] = change["after"]["exact_text"].replace(
                    FABRICATION_FROM, swap
                )

    _edit_manifest(corpus_copy, rewrite_chg1)
    source = corpus_copy / "v2_revised_proposed_rule.txt"
    text = source.read_bytes().decode("utf-8").replace(FABRICATION_FROM, swap)
    source.write_bytes(text.encode("utf-8"))

    assert run.main(["--data", str(corpus_copy)]) == 1
    printed = capsys.readouterr().out
    assert "2. DELIBERATE-CORRUPTION REJECTION" in printed
    assert "probe 1 did not fabricate anything" in printed
    assert "RELEASE BLOCKER: Deliberate-corruption rejection." in printed
    # The offsets never moved, so the first metric is still green. The gate
    # closed on the probe alone.
    assert "1. CITATION VERIFICATION" in printed
    assert "RELEASE BLOCKER: Citation verification." not in printed


def test_a_failed_metric_still_prints_its_counts_and_its_reasons(corpus_copy, capsys):
    def break_two_offsets(manifest):
        for change in manifest["changes"]:
            if change["id"] in {"CHG-1", "CHG-3"}:
                change["after"]["end"] -= 1

    _edit_manifest(corpus_copy, break_two_offsets)

    assert run.main(["--data", str(corpus_copy)]) == 1
    printed = _flat(capsys.readouterr().out)
    assert "quoted text does not match the source at the cited offsets" in printed
    # The counts move and stay counts. Two offsets stopped verifying, which is
    # 18 of 20 and would once have printed as 90.0%. The sample did not change
    # -- the same six subjects -- so there is still no rate to print, in the bad
    # direction as well as the good one.
    assert (
        "18 of 20 manifest offsets verify [n = 6 subjects over 20 recorded "
        f"offsets; no rate, n < {MIN_SAMPLE_FOR_RATE}]" in printed
    )
    assert "90.0%" not in printed
    assert "%" not in printed


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_scoring_opens_no_socket(monkeypatch, data_dir: Path):
    """Break the socket module, then score. Nothing should notice."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the eval harness opened a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    card = run.score(Corpus.load(data_dir))
    assert card.exit_code == 0
    assert run.render(card)


def test_the_harness_imports_no_model_client_and_no_database_engine(repo_root: Path):
    """Checked in a fresh process, because this test's own imports are noisy."""
    probe = (
        "import sys, app.evals.run;"
        "print('\\n'.join(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
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
        # app.state.db builds a SQLAlchemy engine at import time. The eval path
        # must not reach it, or `make eval` acquires a database.
        "app.state.db",
    }
    assert not (loaded & forbidden), sorted(loaded & forbidden)


def test_make_eval_runs_on_a_clean_checkout_with_no_key_and_no_database(
    repo_root: Path, data_dir: Path, tmp_path: Path
):
    """The reviewer's path: no API key, no configured database, empty cwd."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "STRATA_DATABASE_URL",
        }
    }
    env["PYTHONPATH"] = str(repo_root)

    result = subprocess.run(
        [sys.executable, "-m", "app.evals.run", "--data", str(data_dir)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "VERDICT: 5 of 5 metrics pass." in result.stdout
    # Nothing was written beside the process: no strata.db, no cache, no export.
    assert list(tmp_path.iterdir()) == []


def test_the_makefile_target_runs_this_module(repo_root: Path):
    """`make eval` is advertised. Keep the advertisement true."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert "app.evals.run" in makefile
