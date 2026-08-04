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
from app.evals.metrics import ALL_METRICS, FABRICATION_FROM
from app.evals.report import (
    CAVEAT,
    MIN_SAMPLE_FOR_RATE,
    RateRefused,
    count_phrase,
    rate,
)

# The counts the current code earns against the committed corpus. Written out
# rather than computed, so a regression appears as a diff in this file.
EXPECTED = {
    "citation_verification": (20, 20, 20, "offsets"),
    "corruption_rejection": (2, 2, 2, "probes"),
    "diff_completeness": (5, 5, 5, "changes"),
    "occurrence_disambiguation": (27, 27, 9, "spans"),
    "draft_final_routing": (7, 7, 1, "change: CHG-4"),
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


def test_the_caveat_is_always_printed_and_its_numbers_match_the_corpus(card, output):
    """Prose in the caveat is a claim too, so it is checked against the counts."""
    flat_output, flat_caveat = _flat(output), _flat(CAVEAT)
    assert flat_caveat in flat_output

    largest = max(metric.sample_size for metric in card.metrics)
    changes = next(m for m in card.metrics if m.key == "diff_completeness").sample_size
    assert f"sample here is {largest} recorded offsets" in flat_caveat
    assert f"change detection rests on {changes}" in flat_caveat
    assert "withholds a rate below a sample of ten" in flat_caveat
    assert "No model runs here and no network call is made" in flat_caveat


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
    # The counts move, the rate stays tied to its sample, and 18 of 20 is floored
    # to 90.0% rather than rounded anywhere near the threshold it just missed.
    assert "18 of 20 manifest offsets verify [90.0%, n = 20 offsets]" in printed


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
