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

THE RULE THIS FILE ENFORCES, stated exactly, because the first draft of this
sentence was false and a reviewer proved it. A function under app/ is IN SCOPE
when it takes an injectable clock -- a `now`, `moment` or `as_of` parameter
defaulting to None, which is this codebase's spelling for "use the real clock if
you must" -- AND its answer can turn on an expiry. Every in-scope function must
be called from tests WITH that argument. A clocked function whose answer cannot
turn on an expiry is NOT in scope and this file says nothing about it.

WHY THE SECOND HALF OF THAT SENTENCE IS THERE. Only an expiry can rot. A function
that stamps a row with the current time gives the same answer on Wednesday as it
gave on Tuesday; a function that asks whether something has run out does not. The
first version of this guard skipped the distinction and flagged 110 call sites,
almost all harmless, and a guard reporting ninety per cent noise is one somebody
widens until it passes -- which is precisely how the defect it exists for would
have been "fixed".

WHY THE NARROWING IS A CALL GRAPH AND NOT A WORD LIST, which is the whole repair
of 2026-08-05 and the reason this docstring is longer than it was. The narrowing
used to be four substrings -- expires, expiry, is_live, lapsed -- matched against
the candidate function's OWN body. That is a hand-maintained list wearing the
costume of a derived rule, and it was already stale on the day it shipped.
`awaiting_acceptance` in app/state/routing.py contains none of the four words. It
hands its clock one frame down to `resolve_escalation_owner`, and THAT is where
an invitation is tested for having run out. So the guard could not see
test_an_item_waiting_on_an_invitation_leaves_the_shared_queue_for_its_own in
tests/test_invites.py, which wrote an invitation at a fixed T0 and asked
`awaiting_acceptance` about it on the real clock. INVITE_DEFAULT_TTL_DAYS is 7.
That call was armed to fail on 2026-08-11, on somebody else's clean clone, with
nothing in the output to explain it -- the identical mechanism the file exists to
remove, surviving inside the fix for it.

So expiry-dependence is now computed transitively. A function is expiry-dependent
if its own body names an expiry, OR if it hands its clock to a function that is.
`_expiry_dependent` below walks that to a fixpoint. A function that grows an
expiry three frames down is covered without anybody adding it here, which is the
claim the old prose made and could not keep. In numbers: 48 clocked public
functions under app/, of which the word list saw 14 and the walk sees 29.

WHAT THIS STILL CANNOT SEE, listed rather than left to be found, because the last
version of this docstring claimed more than it did and that is the failure being
repaired. (1) Calls are matched by BARE NAME -- two functions sharing a name are
one node, which widens and never narrows, and an import alias is invisible.
(2) A clock passed POSITIONALLY from a test reads as unpinned, so the guard would
send you to pin something already pinned; the house style is keywords and every
call site here follows it. (3) Only tests/*.py is scanned, not subdirectories.
(4) A function that reaches an expiry WITHOUT being handed a clock -- reading the
real clock itself, deeper down -- is invisible to this and cannot be fixed by
pinning anyway; that is a defect in app/, and this guard is not the place it gets
caught.

WHAT THE PINNING PASS TAUGHT, which is not what it set out to teach. Pinning is
not the goal. AGREEMENT BETWEEN THE WRITE AND THE READ is the goal. Some calls
write a row that is read back over HTTP, and an HTTP route has no clock a test
can set. Pinning those writes would stamp a fixed date on a row read by a moving
clock -- not a cousin of the invitation defect, the identical mechanism. A pinned
clock on one side of a pair is worse than no pinning at all, so those stay on the
real clock, where both ends move together, and ALLOWED says so in words.
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
TESTS = pathlib.Path(__file__).resolve().parent

#: The three names this codebase uses for an injectable clock.
CLOCK_NAMES = ("now", "moment", "as_of")

#: The words that mean a function's own body asks whether something has run out.
#: These are the SEEDS of the expiry-dependence walk, not the whole of it. A
#: function that names none of them is still in scope if it hands its clock to
#: one that does -- see `_expiry_dependent`, and see the docstring for the day
#: that distinction was paid for.
EXPIRY_WORDS = ("expires", "expiry", "is_live", "lapsed")

#: Call sites allowed to omit the clock: {(file, enclosing, called): (count, why)}.
#:
#: KEYED BY CALL SITE, NOT BY FILE. The oldest shape allowed a whole file, which
#: is a hole the size of the file: one call in tests/test_sharing.py genuinely
#: must read the real clock, and allowing the file for it would have waved
#: through the four beside it that must not.
#:
#: AND COUNTED, WHICH IS THE 2026-08-05 REPAIR. Keying by (file, enclosing,
#: called) alone is the same hole one level down, and a reviewer walked through
#: it: he added a second, unrelated, unpinned create_share_link inside
#: tests/test_sharing.py::_mint and every test in this file still passed. One
#: helper is not one decision. So each entry declares HOW MANY unpinned calls it
#: was written for, and `test_an_allowance_covers_the_number_of_calls_it_was_
#: written_for` fails when the real number moves in either direction. Adding a
#: call to an allowed helper now costs you a number you have to change by hand,
#: with this comment in front of you.
#:
#: THE ONE REASON THAT QUALIFIES, and every entry below is an instance of it:
#: THE OTHER END OF THIS PAIR CANNOT BE PINNED, so pinning this end would make
#: the two disagree. Two shapes of it appear below. Most are a row written here
#: and read back over HTTP, and a route has no clock a test can set. Three are
#: the other way round -- a read here against a corpus a seed wrote on the real
#: clock, where the WRITE is the unpinnable half.
#:
#: Either way the argument is the same. Pinning one end of that pair does not
#: remove the defect, it plants it: a row stamped at a fixed date and read by a
#: moving clock passes today, passes for a week, and then fails on somebody
#: else's clean clone with nothing wrong in the product. That is the exact
#: mechanism of the invitation that fired on 2026-08-05.
#:
#: What does NOT qualify, and was pinned instead: a call that refuses before it
#: reads the clock, a call whose assertion is not about time, a call in a test
#: with no fixed moment of its own. All of those are cheap to pin, and pinning
#: them is what makes an omitted clock argument mean "somebody forgot".
ALLOWED = {
    (
        "test_sharing.py",
        "_mint",
        "create_share_link",
    ): (
        1,
        "the links this helper mints are opened over HTTP by client.get(url), "
        "and the share route reads the real clock. Both ends move together or "
        "neither does; see the docstring on _mint.",
    ),
    (
        "test_source_links.py",
        "_mint",
        "create_share_link",
    ): (
        1,
        "same pair as test_sharing._mint -- minted here, opened through "
        "share_client.get(url), which no test can pin.",
    ),
    (
        "test_tenancy_derived.py",
        "_populate",
        "create_share_link",
    ): (
        2,
        "corpus for the tenancy sweep, read back by the function sweep and by "
        "an HTTP sweep that signs in over the real stack. Two calls, one per "
        "tenant, so the sweep has a link on each side of the line to cross. "
        "See _populate.",
    ),
    (
        "test_tenancy_derived.py",
        "_populate",
        "provision_login",
    ): (
        1,
        "same corpus, same two real-clock readers. An invitation pinned to a "
        "fixed date would read live today and expired next week, and the admin "
        "screen the sweep fetches would quietly lose the row.",
    ),
    (
        "test_tenancy_derived.py",
        "_populate",
        "start_run",
    ): (
        1,
        "the run this starts is read back by the HTTP sweep, which drives the "
        "workflow screens on the real clock. A run opened at a fixed date and "
        "read by a moving one goes overdue on a day nobody chose.",
    ),
    (
        "test_change_mapping.py",
        "test_the_wedge_completes_once_a_mapping_exists",
        "resolve_change_owner",
    ): (
        2,
        "the unpinnable half here is the WRITE, not the read. The `seeded` "
        "fixture calls load() and ensure_accounts(), neither of which takes a "
        "moment, so the corpus is stamped whenever the suite runs. A read "
        "pinned to a fixed date against a corpus written today is the "
        "invitation defect with the ends swapped. Two calls, before and after "
        "the mapping, and the pair is the assertion.",
    ),
    (
        "test_change_mapping.py",
        "test_the_seed_leaves_a_workspace_where_the_wedge_completes",
        "resolve_change_owner",
    ): (
        1,
        "same real-clock corpus, plus seed_change_mappings(), which also takes "
        "no moment. What this asserts is that some change in the demo reaches "
        "an owner, which is a fact about the mapping rather than about time.",
    ),
    (
        "test_sharing.py",
        "test_the_setting_is_asked_at_one_place_and_turning_it_off_closes_the_door",
        "open_share",
    ): (
        1,
        "the link being opened was minted by _mint on the real clock, and this "
        "is the read half of that pair. Pinning the read while the write moves "
        "is the invitation defect with the ends swapped.",
    ),
    (
        "test_sharing.py",
        "test_the_sharer_may_revoke_their_own_link",
        "open_share",
    ): (
        1,
        "reads a link _mint wrote on the real clock; the assertion is about who "
        "revoked it, not about when. Pin the read and the pair disagrees.",
    ),
    (
        "test_sharing.py",
        "test_an_admin_may_revoke_somebody_else_s_link",
        "open_share",
    ): (
        1,
        "same real-clock mint, same pair. The question under test is whether an "
        "admin may revoke a link they did not make, which no clock changes.",
    ),
    (
        "test_sharing.py",
        "test_a_colleague_who_is_neither_may_not",
        "open_share",
    ): (
        1,
        "same real-clock mint, same pair. The refusal is about the person, and "
        "it is reached before the expiry is ever consulted.",
    ),
    (
        "test_sharing.py",
        "test_the_registry_reads_are_scoped_to_the_company_that_asks",
        "open_share",
    ): (
        1,
        "the link came from _mint on the real clock; this asserts the tenant "
        "line holds. Pinning one end of a real-clock pair plants the defect.",
    ),
    (
        "test_sharing.py",
        "test_an_empty_token_resolves_to_nothing",
        "open_share",
    ): (
        2,
        "two calls, an empty token and a None one. Neither reaches an expiry: "
        "open_share refuses an unusable token before it looks any link up, so "
        "there is no link whose life could run out between two runs. It takes "
        "a stamp first, but nothing is ever compared against it.",
    ),
}


def _clock_fed_names(node: ast.AST, clock_params: set[str]) -> set[str]:
    """Every local name in this function that carries the clock.

    A function almost never hands its `now` parameter straight on. It writes
    `moment = _require_aware(now or _utcnow(), "now")` and passes `moment`, so a
    rule that only looked for the parameter name would miss the forwarding on
    the very first line of nearly every function in this codebase.

    So: start with the clock parameters, then repeatedly add any name assigned
    from an expression that mentions a name already carrying the clock. Run to a
    fixpoint rather than a fixed number of passes, because `a = now; b = a;
    c = b` is three passes and nobody should have to count them.

    Deliberately generous. A name that merely brushed the clock is treated as
    carrying it. Being generous here can only put MORE functions in scope, and
    the two costs are not symmetric: a false positive costs one keyword
    argument in one test, a false negative costs a suite that goes red on a
    Tuesday for somebody who changed nothing.
    """
    carried = set(clock_params)
    while True:
        grew = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                targets, value = sub.targets, sub.value
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and sub.value:
                targets, value = [sub.target], sub.value
            else:
                continue
            used = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            if not (used & carried):
                continue
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and name.id not in carried:
                        carried.add(name.id)
                        grew = True
        if not grew:
            return carried


def _clock_forwarding_calls(node: ast.AST, clock_params: set[str]) -> set[str]:
    """The functions this one hands its clock to, by name.

    Two shapes count. A keyword whose NAME is a clock name -- `f(now=moment)` --
    because that is this codebase's house style and it is unambiguous. And any
    argument, positional or keyword, whose VALUE is a name carrying the clock,
    because `invitation_is_live(invitation, moment)` forwards just as surely and
    reads nothing like the first shape.
    """
    carried = _clock_fed_names(node, clock_params)
    forwarded: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        callee = getattr(sub.func, "id", None) or getattr(sub.func, "attr", None)
        if not callee:
            continue
        if any(kw.arg in CLOCK_NAMES for kw in sub.keywords if kw.arg):
            forwarded.add(callee)
            continue
        values = list(sub.args) + [kw.value for kw in sub.keywords]
        if any(isinstance(v, ast.Name) and v.id in carried for v in values):
            forwarded.add(callee)
    return forwarded


def _read_app_functions() -> dict[str, dict]:
    """Parse app/ once and describe every function in it, keyed by name.

    Read from the source rather than by importing, so a module with an import
    cost or a side effect cannot change what this guard sees.

    KEYED BY BARE NAME, AND THAT IS A KNOWN, DELIBERATE IMPRECISION. Calls in
    this codebase are written as bare names or attributes, and resolving them
    properly would mean following imports across forty modules. Two functions
    with one name -- `resolve_assignee` exists in app/state/routing.py and in
    app/web/views/admin.py -- are merged into one node here. The merge can only
    ever widen the scope, never narrow it, and the paragraph above says why that
    is the safe direction. It is written down rather than discovered because the
    last three guards in this repository failed by being quietly narrower than
    their own docstrings claimed.
    """
    functions: dict[str, dict] = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a file that will not parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = [a.arg for a in args.args + args.kwonlyargs]
            defaults = list(args.defaults) + list(args.kw_defaults)
            has_none_default = any(
                isinstance(d, ast.Constant) and d.value is None for d in defaults
            )
            clock_params = {n for n in names if n in CLOCK_NAMES}
            entry = functions.setdefault(
                node.name,
                {"clocked": False, "seed": False, "forwards": set(), "path": ""},
            )
            if clock_params and has_none_default:
                entry["clocked"] = True
                entry["path"] = str(path.relative_to(APP.parent))
            # The seed set: this function's own body asks whether something has
            # run out. `ast.dump` rather than the raw text so a word inside a
            # comment or an unrelated string does not count -- a comment saying
            # "this has nothing to do with expiry" would otherwise pull the
            # function into scope, and a guard that reacts to comments is one
            # nobody trusts.
            if any(w in ast.dump(node) for w in EXPIRY_WORDS):
                entry["seed"] = True
            # Only the parameters actually spelt like a clock seed the taint. An
            # earlier draft seeded it with EVERY parameter when the function had
            # no clock of its own, on the theory that a helper might receive the
            # moment under some other name. That makes any argument of any
            # function a clock and any call an edge. Measured, it took the scope
            # from 29 functions to 32 -- a smaller blast than it deserves, and
            # the number is written down because the first draft of this comment
            # guessed "the whole of app/, 48" and was wrong.
            #
            # It is rejected on the principle rather than the count. Those three
            # extra functions are in scope because a variable that never held a
            # moment was treated as one, so the reason they are there cannot be
            # explained to the person told to pin their call. A guard whose
            # inclusions cannot be justified one by one is a guard that gets
            # widened until it says nothing, which is how the defect this file
            # exists for would have been "fixed".
            entry["forwards"] |= _clock_forwarding_calls(node, clock_params)
    return functions


def _expiry_dependent(functions: dict[str, dict]) -> set[str]:
    """The functions whose answer can turn on an expiry, followed through calls.

    THE REPAIR OF 2026-08-05, and the reason this is a graph walk rather than a
    substring test. `awaiting_acceptance` names no expiry. It hands its clock to
    `resolve_escalation_owner`, which asks whether an invitation is still live.
    The old narrowing looked only at the candidate's own body, so
    `awaiting_acceptance` was out of scope, and tests/test_invites.py:708 -- an
    invitation written at a fixed T0, read on the real clock, seven-day life --
    sat there armed to fail on 2026-08-11 for somebody who had changed nothing.
    That is the exact defect this whole file exists to remove, surviving inside
    the fix for it, hidden by a word list.

    Transitive closure, run to a fixpoint. If f hands its clock to g and g can
    turn on an expiry, then f can too, however many frames deep g is.
    """
    dependent = {name for name, f in functions.items() if f["seed"]}
    while True:
        grown = {
            name
            for name, f in functions.items()
            if name not in dependent and (f["forwards"] & dependent)
        }
        if not grown:
            return dependent
        dependent |= grown


def _functions_taking_a_clock() -> dict[str, str]:
    """Every public function under app/ that takes a clock AND can rot.

    Both halves are read out of the source. The first is the parameter; the
    second is `_expiry_dependent`, which follows the clock through calls rather
    than grepping one function's body for four words. Private helpers are left
    out of the ANSWER -- no test calls them -- but they are in the graph, which
    is how a chain like awaiting_acceptance -> _open_escalations -> a liveness
    test stays visible.
    """
    functions = _read_app_functions()
    dependent = _expiry_dependent(functions)
    return {
        name: f["path"]
        for name, f in functions.items()
        if f["clocked"] and name in dependent and not name.startswith("_")
    }


def _unpinned_call_sites(clocked: dict[str, str]) -> list[tuple[str, str, str, int]]:
    """Every call in tests/ to a clocked function that omits the moment.

    Returns (file, enclosing function, function called, line) -- ALLOWED is
    keyed on the first three, and the line is carried only so the failure
    message can point at something.

    THE ENCLOSING FUNCTION IS FOUND BY DESCENT, NOT BY LINE ARITHMETIC. An
    earlier draft matched a call to a def by comparing line numbers, which gets
    a nested helper wrong and, worse, gets it wrong silently -- the call is
    attributed to the outer test, the ALLOWED key never matches, and the reader
    is told to pin something that is already pinned. Walking down from each
    module-level def and carrying the innermost name is exact.
    """
    misses: list[tuple[str, str, str, int]] = []
    for path in sorted(TESTS.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue

        def visit(node: ast.AST, enclosing: str) -> None:
            for child in ast.iter_child_nodes(node):
                inner = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name
                if isinstance(child, ast.Call):
                    name = getattr(child.func, "id", None) or getattr(
                        child.func, "attr", None
                    )
                    if name in clocked:
                        passed = {k.arg for k in child.keywords if k.arg}
                        if not (passed & set(CLOCK_NAMES)):
                            misses.append((path.name, enclosing, name, child.lineno))
                visit(child, inner)

        visit(tree, "<module>")
    return misses


def _calls_missing_the_clock(clocked: dict[str, str]) -> list[str]:
    """The unpinned call sites nobody has decided about, ready to print."""
    return [
        f"{file}:{line} {called}() in {where} -- {clocked[called]}"
        for file, where, called, line in _unpinned_call_sites(clocked)
        if (file, where, called) not in ALLOWED
    ]


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


def test_every_allowance_still_names_a_real_unpinned_call():
    """No entry in ALLOWED may describe a call site that is not there any more.

    THE FAILURE THIS EXISTS FOR, which the list it replaced already had. An
    allowance is a hole in the guard. Rename the test that earned it, pin the
    call, delete the helper -- and the hole stays open, pointing at nothing,
    and the next call that lands on that name inherits a decision nobody made
    about it. That is how the routers list, the tenant-filter list and the
    partial-template list in this repository each went stale: the entry outlived
    the reason.

    So an allowance has to keep earning itself. If the named call is now pinned,
    or gone, the entry goes with it -- and this test says so by name rather than
    leaving somebody to notice.
    """
    clocked = _functions_taking_a_clock()
    live = {(file, where, called) for file, where, called, _ in _unpinned_call_sites(clocked)}

    stale = sorted(key for key in ALLOWED if key not in live)
    assert not stale, (
        "these entries in ALLOWED no longer name an unpinned call:\n  "
        + "\n  ".join(f"{file}:{where} {called}()" for file, where, called in stale)
        + "\n\nEither the call was pinned, or it was renamed, or it is gone. "
        "Delete the entry. An allowance that points at nothing is a hole with "
        "no reason attached, and the next call to land on that name inherits it."
    )


def test_an_allowance_covers_the_number_of_calls_it_was_written_for():
    """A helper is not a blanket. Each entry declares how many calls it covers.

    THE HOLE THIS CLOSES, found by a reviewer and reproduced before it was
    fixed. Keying an allowance by (file, enclosing function, callee) sounds
    exact and is not: it waves through UNLIMITED calls to that callee inside
    that helper. He dropped a second, unrelated, unpinned create_share_link into
    tests/test_sharing.py::_mint and every test in this file still passed. That
    is the same criticism this file already levels at allowing a whole file,
    one level down, and the file had not noticed it about itself.

    So the count is part of the decision. Adding a call to an allowed helper now
    turns this red, and the person adding it has to look at the reason beside
    the number and say whether it covers their call too. Removing one turns it
    red as well: an allowance for two calls where only one survives is an
    allowance half of which nobody is thinking about any more.
    """
    clocked = _functions_taking_a_clock()
    live: dict[tuple[str, str, str], int] = {}
    for file, where, called, _line in _unpinned_call_sites(clocked):
        live[(file, where, called)] = live.get((file, where, called), 0) + 1

    wrong = [
        f"{file}:{where} {called}() -- allowed for {declared}, found {live.get(key, 0)}"
        for key, (declared, _reason) in ALLOWED.items()
        for file, where, called in [key]
        if live.get(key, 0) != declared
    ]
    assert not wrong, (
        "these allowances no longer cover the number of calls they were written "
        "for:\n  " + "\n  ".join(wrong)
        + "\n\nIf a call was added, decide about it: either pin it, or raise the "
        "number and widen the reason to say why the new one qualifies too. If a "
        "call was pinned or deleted, lower the number. An allowance whose count "
        "drifts is an allowance covering calls nobody read."
    )


def test_every_allowance_carries_a_reason():
    """A key with an empty reason is a name somebody forgot, wearing a decision.

    Cheap, and it closes the obvious way round the rule above: adding the key
    and leaving the value blank, or writing "see above", passes every other
    check in this file.
    """
    for key, (count, reason) in ALLOWED.items():
        assert isinstance(count, int) and count >= 1, (
            f"{key} declares {count!r} calls, which is not a count. An allowance "
            "covers a whole number of calls somebody read."
        )
        assert isinstance(reason, str) and len(reason.split()) >= 8, (
            f"{key} is allowed to read the real clock without saying why. "
            "Write the reason out: what reads this row back, and why that "
            "reader cannot be pinned."
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


def test_the_scope_follows_the_clock_into_the_frame_below():
    """The second incident, guarded the same way as the first.

    accounts_for_company names an expiry in its own body, so the word-list
    narrowing this file used to run could see it. `awaiting_acceptance` names
    none: it hands its clock to resolve_escalation_owner, and the invitation is
    tested for having run out down there. The old guard therefore could not see
    tests/test_invites.py:708, which wrote an invitation at a fixed T0 and read
    it on the real clock with a seven-day life -- armed to fail on 2026-08-11 on
    an unchanged clean clone.

    So the closure gets its own non-vacuity test, naming the function that
    exposed it. If somebody replaces the graph walk with something cheaper, this
    is what tells them what they have just given up, by name, rather than
    leaving it to be rediscovered in another six days.

    resolve_escalation_owner is asserted alongside it: awaiting_acceptance can
    only be in scope because that one is, and an assertion on the conclusion
    without the premise reads as luck.
    """
    clocked = _functions_taking_a_clock()
    for name in ("resolve_escalation_owner", "awaiting_acceptance"):
        assert name in clocked, (
            f"{name} takes a clock and its answer turns on whether an invitation "
            "has run out -- one frame down, in resolve_escalation_owner. The "
            "narrowing has stopped following the clock through calls, and every "
            "test that reads an expiry indirectly is unguarded again."
        )


def test_the_scope_is_narrower_than_every_clocked_function():
    """The other half of not being vacuous: the narrowing must still narrow.

    A closure that swallows app/ whole is not a stricter guard, it is a broken
    one -- it flags a hundred harmless call sites, and the next person widens it
    until it flags nothing, which is exactly how the defect this file exists for
    would have been "fixed". The realistic way to get there is not a bad taint
    rule but a broken parser: make the expiry seed match everything and the walk
    reaches every clocked function in one hop, silently, while every other test
    in this file still passes.

    So: some clocked function under app/ must be OUT of scope. `logout` is the
    honest example and is named here on purpose. It takes `now`, and it stamps
    revoked_at with it -- but it compares that moment against nothing. It
    revokes whatever session it finds, and it gives the same answer on Wednesday
    as on Tuesday. A reviewer counted logout among the functions this guard
    "drops" as though that were the defect. It is the rule working.
    """
    functions = _read_app_functions()
    all_clocked = {
        name
        for name, f in functions.items()
        if f["clocked"] and not name.startswith("_")
    }
    in_scope = set(_functions_taking_a_clock())
    assert in_scope < all_clocked, (
        "every clocked function under app/ is in scope, which means the "
        "expiry-dependence walk has stopped discriminating. A guard that flags "
        "everything gets widened until it flags nothing."
    )
    assert "logout" not in in_scope, (
        "logout is in scope. Either it grew an expiry comparison -- in which "
        "case this test is the thing to change, and every logout call in tests/ "
        "now needs pinning -- or the walk has started following clocks that "
        "reach no expiry."
    )
