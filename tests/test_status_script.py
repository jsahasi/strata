"""The instrument that reports on the repository, held to the repository's rule.

docs/.ai/state.json is generated, and ADR-42 makes it authoritative over every
prose document here -- it is the one file that cannot be stale, so anything
downstream believes it. That standing is exactly why the thing writing it must
not guess.

THE DEFECT. scripts/status.py regexed pytest output for "N passed" and fell back
to 0 when nothing matched. A collection error prints no summary at all: one
half-written module importing something not yet created aborts the run before a
single test executes. The fallback turned "I could not measure" into "0 tests
pass", and wrote it to the authoritative file. That single number cannot be told
apart from an empty suite or a suite where everything failed -- three different
facts flattened into one, and the alarming one.

Absence is denial applies to our own instruments as much as to a citation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from status import parse_pytest_summary  # noqa: E402


# Captured from real runs of this repository's suite rather than invented, so a
# change to pytest's output shape breaks these rather than passing quietly.
CLEAN = "........................................          [100%]\n458 passed in 35.58s\n"

WITH_FAILURES = (
    "FAILED tests/test_workflow_editor.py::test_the_editor_assets_are_served\n"
    "37 failed, 502 passed in 50.67s\n"
)

COLLECTION_ERROR = (
    "ImportError while importing test module "
    "'/Users/x/strata/tests/test_rollback.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "E   ModuleNotFoundError: No module named 'app.state.rollback'\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
    "1 error in 0.43s\n"
)

NOTHING_USEFUL = "bash: pytest: command not found\n"

# The other shape pytest uses, which prints a separator line of underscores that
# a loose pattern will happily read as part of the filename.
COLLECT_ERROR_BANNER = (
    "==================== ERRORS ====================\n"
    "_____ ERROR collecting tests/test_workflow.py ____________________\n"
    "ModuleNotFoundError: No module named 'app.state.workflow'\n"
    "!!!!!!! Interrupted: 1 error during collection !!!!!!!\n"
    "1 error in 0.51s\n"
)


def test_a_clean_run_reports_its_count():
    got = parse_pytest_summary(CLEAN)
    assert got["passed"] == 458
    assert got["failed"] is None
    assert got["measured"] is True
    assert got["problem"] == ""


def test_failures_are_counted_and_still_count_as_measured():
    """A red suite is a measurement. It is the unmeasurable case that differs."""
    got = parse_pytest_summary(WITH_FAILURES)
    assert got["passed"] == 502
    assert got["failed"] == 37
    assert got["measured"] is True


def test_a_collection_error_reports_no_number_at_all():
    """The defect. Zero would be a lie; None is the truth.

    Asserting `is None` rather than falsiness matters: 0 is falsy too, and the
    whole bug was that 0 looked like an answer.
    """
    got = parse_pytest_summary(COLLECTION_ERROR)
    assert got["passed"] is None, "a run that never started must not report a count"
    assert got["failed"] is None
    assert got["measured"] is False
    assert got["errors"] == 1


def test_the_collection_error_names_the_module_that_broke():
    """A reviewer needs to know which file to look at, not merely that one broke."""
    got = parse_pytest_summary(COLLECTION_ERROR)
    assert "test_rollback.py" in got["problem"]
    assert "collection failed" in got["problem"]


def test_unrecognisable_output_is_also_unmeasured_rather_than_zero():
    """The catch-all. Anything we cannot read is a refusal, not a count of nought."""
    got = parse_pytest_summary(NOTHING_USEFUL)
    assert got["passed"] is None
    assert got["measured"] is False
    assert got["problem"], "an unmeasured run must say why"


def test_zero_is_never_substituted_for_an_unknown():
    """The rule, stated once against every unmeasurable shape we know of."""
    for raw in (COLLECTION_ERROR, NOTHING_USEFUL, "", "collected 0 items"):
        got = parse_pytest_summary(raw)
        assert got["passed"] != 0, f"reported 0 rather than refusing, for: {raw[:40]!r}"


def test_the_module_name_stops_at_whitespace():
    """A separator line of underscores is not part of the filename."""
    got = parse_pytest_summary(COLLECT_ERROR_BANNER)
    assert got["measured"] is False
    assert "test_workflow.py" in got["problem"]
    assert "___" not in got["problem"], f"read the separator as a path: {got['problem']!r}"
    assert got["problem"].count("test_workflow.py") == 1, "named the same file twice"
