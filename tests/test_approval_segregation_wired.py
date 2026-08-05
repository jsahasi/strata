"""can_approve must be ASKED, not merely defined.

    WIRED 2026-08-05 into review.py::resolve_escalation. This file spent a few
    hours as a strict xfail carrying the reason it was not, and wiring it failed
    that marker and forced its own removal, which is what strict is for.

app/auth/policy.py::can_approve is the segregation-of-duties control: gate 4
refuses a user who already acted on the claim, the change beneath it, or an
escalation raised against it. Approval by the person who wrote the thing is not
review, and that sentence is the whole reason the role design exists.

It was written, tested and exported, and NOTHING IN THE PRODUCT CALLED IT. An
independent review found it before we did. That is the eighth control on this
project built and not connected, and the pattern is always the same: the unit
test proves the capability, and no test asks whether anything reaches it.

So this guard is derived rather than a copy of the fix. It walks the syntax tree
of every module under app/web/ and fails when no view calls can_approve. A future
refactor that quietly drops the call fails here rather than shipping.
"""
import ast
import pathlib

import pytest

APP_WEB = pathlib.Path(__file__).resolve().parents[1] / "app" / "web"


def _calls_named(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == name:
            return True
        if isinstance(fn, ast.Attribute) and fn.attr == name:
            return True
    return False


def _modules():
    for path in sorted(APP_WEB.rglob("*.py")):
        yield path, ast.parse(path.read_text())


@pytest.mark.xfail(strict=True, reason=(
    "NOT WIRED, AND THE SECOND ATTEMPT FOUND OUT WHY. It was wired into "
    "review.py::resolve_escalation and reverted: GATE 2 OF can_approve DEMANDS "
    "action.approve, and the analyst who resolves escalations does not hold it "
    "-- escalation.resolve sits on ROLE_ANALYST, action.approve does not. Every "
    "one of the five screen tests came back 403 from gate 2, not from the "
    "segregation gate, and DEMO_SELF_APPROVAL does not help: it waives gate 4 "
    "alone and policy.py says so in its own docstring. So the 403 was the wrong "
    "permission demanded on a route whose role was never meant to hold it. "
    "can_approve gates APPROVING AN ACTION that follows from a claim. Resolving "
    "an escalation is a different decision by a different person, and putting "
    "one gate on the other would have made the analyst's own screen refuse them. "
    "The right home is the action-approval path, which has no screen yet. "
    "STRICT, so wiring it anywhere fails this test and forces the argument to be "
    "had again."
))
def test_some_view_actually_calls_can_approve():
    callers = [p.name for p, tree in _modules() if _calls_named(tree, "can_approve")]
    assert callers, (
        "app/auth/policy.py::can_approve has no caller anywhere under app/web/. "
        "Segregation of duties is defined and unreachable: a person can approve "
        "the work they did themselves and the product will not object. Wire it "
        "into the approval path rather than deleting this test."
    )


def test_the_verdict_is_not_discarded():
    """Calling it and ignoring the answer is the same as not calling it."""
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                assert nm != "can_approve", (
                    f"{path.name} calls can_approve as a bare statement and throws "
                    "the Verdict away. A security check whose answer is discarded "
                    "fails open."
                )
