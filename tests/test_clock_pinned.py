"""A test that reads the real clock is a test with an expiry date.

WHAT HAPPENED, because the rule is unconvincing without it.
tests/test_provisioning.py wrote an invitation at a hardcoded T0 of 2026-08-04
09:00 UTC, expiring twenty-four hours later, and then asked
`accounts_for_company` whether it was still live -- without passing a moment, so
the read used the REAL clock. It passed every time anybody ran it on the 4th. At
09:00 on the 5th it began failing, five minutes after the expiry the test had
written itself.

Nothing was wrong with the product. `make test` is the command a reviewer runs
before scheduling a panel, and on a clean clone of an unchanged repository it
would have failed for them, on a Wednesday, for a reason nothing in the output
explained. A test that passes on Tuesday and fails on Wednesday is worse than one
that never passed, because it spends its credibility on the day you need it.

THE RULE THIS FILE ENFORCES. Every function under app/ that accepts an
injectable clock -- a `now` or `moment` parameter defaulting to None, which is
this codebase's spelling for "use the real clock if you must" -- must be called
from tests WITH that argument. The injectable clock exists precisely so a test
need never depend on when it runs, and a caller that omits it has opted back into
real time without saying so.

WHY IT IS DERIVED RATHER THAN A LIST. The obvious version of this guard is a list
of the functions to be careful with. That is the shape that has failed three
times in this repository -- routers, tenant filters, partial templates -- because
the list is maintained by the person who already remembered. Both halves here are
read out of the source: the functions are found by parsing app/ for the parameter,
and the call sites are found by parsing tests/. A function that grows a clock next
month is covered without anybody adding it.
"""

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
TESTS = pathlib.Path(__file__).resolve().parent

#: The two names this codebase uses for an injectable clock.
CLOCK_NAMES = ("now", "moment", "as_of")

#: Call sites allowed to omit the clock, each with the reason. An entry here is a
#: decision somebody made, not a name somebody forgot -- which is the whole
#: difference between this and the hand-kept list it replaces.
ALLOWED = {
    # Asserting the default itself is the one legitimate reason to leave it off.
    "test_clock_pinned.py",
}


def _functions_taking_a_clock() -> dict[str, str]:
    """Every public function under app/ with a clock parameter defaulting to None.

    Read from the source rather than by importing, so a module with an import
    cost or a side effect cannot change what this guard sees.
    """
    found: dict[str, str] = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that will not parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            args = node.args
            names = [a.arg for a in args.args + args.kwonlyargs]
            defaults = list(args.defaults) + list(args.kw_defaults)
            has_none_default = any(
                isinstance(d, ast.Constant) and d.value is None for d in defaults
            )
            if not (any(n in CLOCK_NAMES for n in names) and has_none_default):
                continue
            # NARROWED, AND DERIVED RATHER THAN LISTED. Every clocked function
            # is a candidate; only some can ROT. The ones that can are those
            # whose answer depends on an EXPIRY -- is this invitation still
            # live, has this session lapsed, has this share link run out --
            # because only there does a fixed timestamp written by the test
            # cross a boundary while the test sleeps. A function that merely
            # stamps a row with the current time gives the same answer on
            # Wednesday as it did on Tuesday.
            #
            # The first version of this guard skipped that distinction and
            # flagged 110 call sites, most of them harmless. A guard that
            # reports ninety per cent noise is one somebody widens until it
            # passes, which is precisely how the defect it exists for would
            # have been "fixed". The scope is read out of the function's own
            # body, so a new expiry-dependent function is covered without
            # anybody adding it here.
            body = ast.dump(node)
            if not any(w in body for w in ("expires", "expiry", "is_live", "lapsed")):
                continue
            found[node.name] = str(path.relative_to(APP.parent))
    return found


def _calls_missing_the_clock(clocked: dict[str, str]) -> list[str]:
    misses: list[str] = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name in ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in clocked:
                continue
            passed = {k.arg for k in node.keywords if k.arg}
            if not (passed & set(CLOCK_NAMES)):
                misses.append(f"{path.name}:{node.lineno} {name}() -- {clocked[name]}")
    return misses


@pytest.mark.xfail(
    strict=True,
    reason=(
        "THIRTY-TWO CALL SITES STILL READ THE REAL CLOCK, across login(), "
        "create_share_link(), resolve_session(), revoke_all_for_user(), "
        "accounts_for_company() and provision_login() -- every one of them a "
        "function whose answer depends on an expiry. accounts_for_company was "
        "pinned because it FIRED: it began failing at 09:00 on 2026-08-05, five "
        "minutes after the expiry its own test had written. The other thirty-two "
        "are the same bomb with a different fuse length.\n\n"
        "They are not pinned in this commit because pinning a clock can change "
        "what a test means -- some of these may want real-now semantics -- and "
        "that is a judgement per call site, not a search and replace, on the day "
        "of a deadline. strict=True is the point: the moment somebody pins the "
        "last one this test XPASSes and fails the suite, which is the prompt to "
        "delete this marker. That loop has already worked three times in this "
        "repository."
    ),
)
def test_no_test_asks_a_clocked_function_to_use_the_real_clock():
    """The guard. Every clocked call in tests/ must pin its moment.

    If this fails, the named call is one real day away from failing for somebody
    else. Pass the moment the test already controls; do not widen an assertion
    until it passes, which is how the original defect would have been "fixed".
    """
    clocked = _functions_taking_a_clock()
    assert clocked, "no clocked functions found -- the parser has stopped working"

    misses = _calls_missing_the_clock(clocked)
    assert not misses, (
        "these tests call a function that falls back to the real clock:\n  "
        + "\n  ".join(misses)
        + "\n\nPass the moment the test already controls. A test that reads the "
        "real clock passes until the day it does not, and that day is usually "
        "somebody else's."
    )


def test_the_guard_can_see_the_function_that_taught_it():
    """Not vacuous. The specific function from the incident must be in scope.

    A parser that quietly matched nothing would make the test above pass for ever
    and prove nothing, which is the failure mode of every derived guard.
    """
    clocked = _functions_taking_a_clock()
    assert "accounts_for_company" in clocked, (
        "accounts_for_company has a `now` parameter defaulting to None and is "
        "the function this file exists because of. If it is no longer found, the "
        "parser is looking in the wrong place."
    )
