"""Eval harness. Prints scores rather than adjectives.

    make eval
    .venv/bin/python -m app.evals.run [--data DIR]

What this is. A scorecard over the deterministic spine of Strata -- the
citation verifier, the diff, the occurrence check, the draft/final routing
table -- computed against `data/manifest.json`, whose offsets were produced by
a separate script and verified against the bytes. It calls no model, opens no
socket and connects to no database, so a reviewer can run it on a clean
checkout with no API key and read every number back to a file in `data/`.

Why the counts look small and stay small. The corpus labels five changes. A
recall percentage over five items is false precision, and this repository
exists to catch claims that assert more than their evidence supports. So the
harness reports raw counts with their denominators, prints a rate only when the
independent sample reaches ten, and closes with a caveat naming the sample size
and what it cannot support. `app/evals/report.py` owns that rule so no future
metric can quietly drop it.

Exit code. Zero when every blocking metric clears its threshold, one when any
fails, so `make eval` is usable as a gate in CI rather than as something a
human reads and forgets.
"""

import argparse
import textwrap
from dataclasses import dataclass
from pathlib import Path

from app.evals.corpus import Corpus
from app.evals.metrics import ALL_METRICS, ALL_OBSERVATIONS, Metric, Observation
from app.evals.report import CAVEAT, count_phrase

WIDTH = 80
LABEL_WIDTH = 11

# Printed output stays ASCII on purpose. A reviewer's terminal is not ours to
# choose, and a scorecard that raises UnicodeEncodeError on a Windows console
# has failed at the one job it had.
BLOCKER = "RELEASE BLOCKER"


@dataclass(frozen=True)
class Scorecard:
    data_dir: Path
    versions: tuple[tuple[str, str, str, str], ...]
    metrics: tuple[Metric, ...]
    observations: tuple[Observation, ...]

    @property
    def failures(self) -> tuple[Metric, ...]:
        return tuple(metric for metric in self.metrics if not metric.passed)

    @property
    def blocking_failures(self) -> tuple[Metric, ...]:
        return tuple(metric for metric in self.failures if metric.blocking)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocking_failures else 0


def score(corpus: Corpus) -> Scorecard:
    """Run every metric against the corpus. No I/O beyond what Corpus read."""
    return Scorecard(
        data_dir=corpus.data_dir,
        versions=tuple(
            (
                version_id,
                corpus.label(version_id),
                corpus.status(version_id),
                corpus.digest(version_id),
            )
            for version_id in corpus.version_ids
        ),
        metrics=tuple(metric(corpus) for metric in ALL_METRICS),
        observations=tuple(observation(corpus) for observation in ALL_OBSERVATIONS),
    )


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _field(label: str, body: str) -> str:
    prefix = f"  {label:<{LABEL_WIDTH}}"
    return textwrap.fill(
        body, width=WIDTH, initial_indent=prefix, subsequent_indent=" " * len(prefix)
    )


def _bullet(marker: str, body: str) -> str:
    prefix = f"  {marker} "
    return textwrap.fill(
        body, width=WIDTH, initial_indent=prefix, subsequent_indent=" " * len(prefix)
    )


def _headline(ordinal: int, metric: Metric) -> str:
    verdict = "PASS" if metric.passed else "FAIL"
    left = f"{ordinal}. {metric.title.upper()}"
    return f"{left}{verdict:>{max(1, WIDTH - len(left))}}"


def render(card: Scorecard) -> str:
    lines: list[str] = [
        _rule("="),
        "STRATA EVAL SCORECARD",
        _rule("="),
        "No model call, no network call, no database connection. Every expected",
        "answer comes from data/manifest.json, computed by a separate script and",
        "verified against the bytes; nothing in app/ produced it.",
        "",
        f"corpus: {card.data_dir}",
        "",
        "Scored against these bytes. A verdict belongs to a version, not to a",
        "document (best-practices.html section 28):",
    ]
    for version_id, label, status, digest in card.versions:
        lines.append(f"  {version_id}  {label:<32} {status:<6} sha256 {digest}")

    for ordinal, metric in enumerate(card.metrics, start=1):
        lines += ["", _rule(), _headline(ordinal, metric), _rule()]
        lines.append(_field("Measures", metric.measures))
        lines.append(_field("How", metric.method))
        lines.append(_field("Threshold", metric.threshold))
        lines.append(
            _field(
                "Result",
                count_phrase(
                    metric.hits,
                    metric.total,
                    metric.unit,
                    sample_size=metric.sample_size,
                    sample_unit=metric.sample_unit,
                ),
            )
        )
        for failure in metric.failures:
            lines.append(_bullet("!", failure))
        for note in metric.notes:
            lines.append(_bullet("-", note))
        if not metric.passed and metric.blocking:
            lines.append("")
            lines.append(
                _bullet(
                    " ",
                    f"{BLOCKER}: {metric.title} did not clear its threshold. "
                    f"Do not ship this. {metric.threshold}",
                )
            )

    for observation in card.observations:
        lines += ["", _rule(), observation.title, _rule()]
        for line in observation.lines:
            lines.append(_bullet("-", line))

    lines += ["", _rule(), CAVEAT, ""]

    passed = len(card.metrics) - len(card.failures)
    lines.append(_rule("="))
    lines.append(f"VERDICT: {passed} of {len(card.metrics)} metrics pass.")
    if card.blocking_failures:
        names = ", ".join(metric.title for metric in card.blocking_failures)
        lines.append(f"{BLOCKER}: {names}.")
        lines.append("Exit 1. This is a gate, not a report.")
    else:
        lines.append("No release blocker. Exit 0.")
        lines.append(
            "Read the caveat above before quoting any of it. n = 5 for change "
            "detection."
        )
    lines.append(_rule("="))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals.run",
        description="Score Strata's deterministic spine against data/manifest.json.",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="corpus directory holding manifest.json (default: ./data)",
    )
    args = parser.parse_args(argv)

    card = score(Corpus.load(args.data))
    print(render(card))
    return card.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
