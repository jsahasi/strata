"""The one live call is written down, and the record cannot outlive the model.

WHY THIS EXISTS. Every test in this repository drives a deterministic fake
through an injected transport, so the class that talks to the endpoint --
AnthropicTransport, in app/chat/agent.py and app/interpretation/propose.py --
was documented design that had never run. scripts/probe_live_transport.py runs
it once and docs/.ai/live-transport-probe.html records what came back.

THE FAILURE THIS GUARDS. A transcript is only worth reading if it describes the
model the product actually calls. The moment somebody edits MODEL_ID, the
transcript becomes a true statement about a model nobody is using -- which reads
exactly like a true statement about the model everybody is using. Nothing on the
page says how old it is, so a reader cannot tell. That is the same class of
defect as a stale ADR: right on the day it was typed, wrong on the day the
constant moved, and trusted either way.

So the model id is read out of the application rather than typed here, and the
document is held to it. If that test fails you changed the model: run the probe
again against the new one, or say plainly in the document that the recorded call
was made against a model the product no longer uses.

WHAT THE REST OF THIS FILE GUARDS, and why it grew. A reviewer read the first
version of the transcript and found three more claims on it that nothing checked,
two of which were wrong. The request block had been reformatted by hand while the
page said it was printed. The reason given for joining the text blocks was a fact
about the SDK that the SDK contradicts. And the module the probe ran still said,
in a docstring ten lines below its corrected heading, that it had never run. Each
now has a test that reads the answer out of the system -- the shipped transport,
the installed SDK, the modules the page itself names -- rather than out of prose.

The pattern worth keeping: a transcript is a set of claims, and a claim nobody
can check is decoration. These tests cannot make the page true. They can stop the
checkable parts of it from quietly becoming false.
"""

import ast
import json
import pathlib
import re
import sys

from app.chat.agent import MODEL_ID
from app.interpretation.propose import MODEL_ID as PROPOSER_MODEL

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "scripts"))

import probe_live_transport  # noqa: E402 -- the path insert above has to run first

PROBE_DOC = ROOT / "docs" / ".ai" / "live-transport-probe.html"

PROBE_SCRIPT = ROOT / "scripts" / "probe_live_transport.py"

#: The one element the document promises to carry, so the guard reads the value
#: the probe RECORDED rather than any model id that happens to appear in prose.
#: An attribute rather than a heading or a table position: prose gets rewritten,
#: and a guard that breaks when a sentence is reworded gets deleted.
_RECORDED = re.compile(
    r'data-probe="model-id"[^>]*>\s*([A-Za-z0-9._-]+)\s*<', re.IGNORECASE
)

#: Every Anthropic model id anywhere on the page. Deliberately wider than the
#: marked element: a document that records one model and mentions another in
#: passing is a document a reader can misread, and the second id is the one they
#: will remember. Narrow enough not to fire on ordinary words -- it needs the
#: "claude-" prefix and at least one more segment.
_ANY_MODEL_ID = re.compile(r"\bclaude-[a-z0-9]+(?:-[a-z0-9]+)*\b", re.IGNORECASE)

#: The modules the transcript says did and did not run. The guard below reads
#: BOTH lists off the page rather than naming the files here, because the thing
#: that rots is the file list: a third transport added next month is a file this
#: test would never have heard of, and a hand-kept list would stay green while
#: its docstring lied.
_EXERCISED_MODULE = re.compile(
    r'data-probe="exercised-module"[^>]*>\s*([^<\s]+)\s*<', re.IGNORECASE
)
_UNEXERCISED_MODULE = re.compile(
    r'data-probe="unexercised-module"[^>]*>\s*([^<\s]+)\s*<', re.IGNORECASE
)

#: An absolute claim that a path has never executed. Deliberately narrow: it
#: catches "has never run", "unexercised", "never been sent" and nothing softer.
#: "untested" is NOT here and must not be -- one live call is not a test, so a
#: docstring that calls the exercised path untested is still telling the truth.
#: What this catches is the sentence that survives a probe by accident: the
#: paragraph ten lines below a corrected heading that still says it never ran.
#: Claims about a BRANCH deliberately slip through -- "the refusal branch has
#: been taken by a fake and never by the endpoint" is true of the exercised
#: module and has to stay sayable. One call proved the path, not every path
#: through it, and a guard that forbade saying so would buy a green suite with a
#: less honest docstring.
_NEVER_RAN = re.compile(
    r"never (?:run|runs|been run|sent|been sent|been exercised|executed)"
    r"|unexercised",
    re.IGNORECASE,
)

#: The cell whose stated reason is a fact about the installed SDK rather than a
#: fact about the call. A reason that is wrong is worse than no reason: it reads
#: as a checked detail and gets repeated.
_THINKING_FIELDS_CLAIM = re.compile(
    r'data-probe="thinking-block-has-no-text"', re.IGNORECASE
)


def _docstrings(module_path: pathlib.Path) -> list[str]:
    """Every docstring in a module, found by walking the tree rather than grep.

    Comments and string literals inside code are deliberately excluded. A guard
    that reads the whole file fires on a comment quoting an old docstring, or on
    a constant that happens to contain the words -- and a guard that fires on
    correct code is a guard somebody deletes.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            text = ast.get_docstring(node)
            if text:
                found.append(text)
    return found


def _probe_text() -> str:
    """The document, or a refusal that says which file is missing and why.

    A bare read raises FileNotFoundError, which names a path and nothing else.
    The reason a reader needs is what the absent file was carrying.
    """
    assert PROBE_DOC.exists(), (
        f"{PROBE_DOC.relative_to(ROOT)} is missing, so there is no record of "
        "the live call and nothing here can be checked."
    )
    return PROBE_DOC.read_text(encoding="utf-8")


def test_the_probe_script_is_in_the_repository():
    """The transcript names a script. A reader must be able to run it again."""
    assert PROBE_SCRIPT.exists(), (
        f"{PROBE_SCRIPT.relative_to(ROOT)} is missing: the live-call transcript "
        "cites a script that nobody can re-run, so nothing in it is checkable."
    )


def test_the_live_call_transcript_exists():
    """No document, no evidence. The claim degrades to an unproven one."""
    assert PROBE_DOC.exists(), (
        f"{PROBE_DOC.relative_to(ROOT)} is missing. docs/tdd.html says the live "
        "transport ran once and links this file for the proof; without it the "
        "claim is prose about a call nobody can see."
    )


def test_the_transcript_records_the_model_the_application_calls():
    """The document is held to the constant, not to somebody's memory of it.

    Both model modules are read, and both must agree with the page. Reading only
    one would let the pair drift apart while this test stayed green -- which is
    the failure tests/test_model_status.py already guards from the other side.
    """
    text = _probe_text()

    match = _RECORDED.search(text)
    assert match, (
        "the transcript carries no element marked data-probe=\"model-id\", so "
        "there is nothing for this guard to check the application against. Add "
        'e.g. <code data-probe="model-id">claude-opus-5</code> to the page.'
    )
    recorded = match.group(1)

    assert recorded == MODEL_ID == PROPOSER_MODEL, (
        f"the live-call transcript records {recorded!r}, app/chat/agent.py pins "
        f"{MODEL_ID!r} and app/interpretation/propose.py pins "
        f"{PROPOSER_MODEL!r}. Run scripts/probe_live_transport.py against the "
        "model the product now calls, or say on the page that the recorded "
        "call was made against a model it no longer calls."
    )


def test_no_other_model_id_appears_on_the_page():
    """One transcript, one model. A second id anywhere is a reader's error.

    This is the half a marked element cannot do. The mark stays correct while a
    paragraph beside it still describes last month's model, and the paragraph is
    what gets read.
    """
    text = _probe_text()
    strays = sorted(
        {found for found in _ANY_MODEL_ID.findall(text) if found != MODEL_ID}
    )
    assert not strays, (
        f"the transcript names model ids the application does not call: "
        f"{strays}. The page records one call against {MODEL_ID!r}; every other "
        "id on it is something a reader can carry away as fact."
    )


def test_the_exercised_module_no_longer_says_it_has_never_run():
    """The probe corrects the module it ran, all the way down, not just the top.

    THE DEFECT THIS GUARDS. A module whose top docstring is corrected to say the
    path ran once, while a class docstring ten lines below still calls it "a path
    that has never run". Both sentences are in the same file, both read as the
    author's considered word, and the second one is the one a reader lands on
    when they open the class. The heading was fixed and the body was not.
    """
    text = _probe_text()
    named = _EXERCISED_MODULE.findall(text)
    assert named, (
        'the transcript marks no element data-probe="exercised-module", so this '
        "guard cannot tell which module the probe actually ran. Mark it, e.g. "
        '<code data-probe="exercised-module">app/interpretation/propose.py</code>.'
    )

    for relative in named:
        module = ROOT / relative
        assert module.exists(), (
            f"the transcript says it exercised {relative}, which is not a file "
            "in this repository."
        )
        stale = [
            doc for doc in _docstrings(module) if _NEVER_RAN.search(doc)
        ]
        assert not stale, (
            f"{relative} ran against the real API on the date in the "
            f"transcript, and {len(stale)} docstring(s) in it still say it never runs "
            "or is unexercised. Whoever reads the class does not read the "
            "module docstring first. Offending text: "
            f"{stale[0][:400]!r}"
        )


def test_the_unexercised_module_still_says_it_has_never_run():
    """The other half. A caveat that quietly disappears is worse than a wrong one.

    The forward path is the test above -- a corrected module that still carries
    the old sentence. This is the backward path: a module the probe did NOT run,
    whose honest "this has never run" gets deleted by somebody tidying up after
    reading the transcript. Then two transports look equally proven and only one
    of them is.
    """
    text = _probe_text()
    named = _UNEXERCISED_MODULE.findall(text)
    assert named, (
        'the transcript marks no element data-probe="unexercised-module". The '
        "page says one of the two transports still has never run; mark which, "
        'e.g. <code data-probe="unexercised-module">app/chat/agent.py</code>.'
    )

    for relative in named:
        module = ROOT / relative
        assert module.exists(), (
            f"the transcript says {relative} was not exercised, and that is not "
            "a file in this repository."
        )
        assert any(_NEVER_RAN.search(doc) for doc in _docstrings(module)), (
            f"the transcript says {relative} has still never run against the "
            "real API, and no docstring in it says so any more. Either it ran "
            "and the transcript is stale, or the caveat was deleted and the "
            "code now reads as proven."
        )


def test_the_recorded_request_is_the_one_the_transport_builds_today():
    """The block on the page is the script's output, not somebody's retyping.

    THE DEFECT THIS GUARDS, and it is the reason the page cannot simply be
    trusted. A request block that was reformatted by hand -- nested keys folded
    onto one line to read better -- is indistinguishable from one that was
    printed, and it is the one artefact on the page a reader would quote back at
    the code. Reformatting is the cheap version of the same failure that changing
    a value would be, and nothing on the page tells the two apart.

    So the guard rebuilds the request by running the SHIPPED transport against a
    recorder and holds the page to the exact bytes. It also catches every later
    drift for free: change MODEL_ID, MAX_OUTPUT_TOKENS, the schema or the probe's
    prompt and this fires, because the recorded request stops being the request.
    """
    expected = json.dumps(
        probe_live_transport.request_the_transport_would_send(),
        indent=2,
        default=str,
    )
    text = _probe_text()
    assert expected in text, (
        "the request block in the transcript is not what "
        "app/interpretation/propose.py's AnthropicTransport builds today. "
        "Regenerate it with `.venv/bin/python scripts/probe_live_transport.py` "
        "and paste the printed block verbatim. Expected to find:\n"
        f"{expected}"
    )


def test_the_transcript_reason_about_thinking_blocks_matches_the_sdk():
    """The stated reason is a claim about the SDK, so the SDK is asked.

    THE DEFECT THIS GUARDS. The transcript explains why the parser joins the
    text blocks instead of indexing content[0]. The conclusion is right and the
    reason it gave was not: it said indexing would have returned the reasoning,
    when this model never returns raw reasoning and the SDK's ThinkingBlock
    carries no text field at all, so that index raises AttributeError. A wrong
    reason beside a right conclusion is the kind of detail a reader repeats,
    because it reads as the one part somebody checked.
    """
    from anthropic.types import TextBlock, ThinkingBlock  # noqa: PLC0415

    text = _probe_text()
    assert _THINKING_FIELDS_CLAIM.search(text), (
        'the transcript carries no element marked data-probe='
        '"thinking-block-has-no-text", so its explanation of why the parser '
        "joins text blocks is prose no test can check against the SDK."
    )
    assert "text" not in ThinkingBlock.model_fields, (
        "the transcript says a thinking block carries no text field, and the "
        f"installed SDK now gives ThinkingBlock {sorted(ThinkingBlock.model_fields)}. "
        "The page's stated reason is out of date; say what the SDK does now."
    )
    assert "text" in TextBlock.model_fields, (
        "the transcript rests on text blocks carrying a text field, and the "
        f"installed SDK gives TextBlock {sorted(TextBlock.model_fields)}."
    )
