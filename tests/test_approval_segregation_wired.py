"""can_approve must be ASKED, not merely defined.

    WIRED 2026-08-05 into app/web/views/actions.py::decide, which is the
    approval path -- a person holding action.approve deciding one proposed
    action on a change somebody else interpreted. The two earlier attempts put
    it on review.py::resolve_escalation and were both reverted: gate 2 demands
    action.approve, escalation.resolve sits on ROLE_ANALYST, and the analyst
    does not hold action.approve, so the gate made the analyst's own screen
    refuse the analyst. DEMO_SELF_APPROVAL does not rescue that -- it waives
    gate 4 alone and policy.py says so in its own docstring.

    This file spent a day as a strict xfail carrying that argument, and the
    marker did its job twice: it failed the suite on both wrong wirings and
    forced the argument to be had again, then failed on the right one and forced
    its own removal. The guard below stays. Only the concession is gone.

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
