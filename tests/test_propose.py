"""The model may judge one change. Only the verifier may let the judgement show.

Every test here runs with no API key and no network. The transport is injected,
so the deterministic fake below stands in for the model and the real client is
never constructed. That is the property the whole suite depends on, and the
first two tests measure it rather than trusting it.

WHAT THIS FILE GUARDS THAT ITS PREDECESSOR DID NOT. The proposer used to send
the model the full text of both versions and ask it to report what changed --
the job ADR-004 reserves for the deterministic diff -- and its output carried no
materiality field at all. The drift was found by reading the ADR against the
code, not by a failing test, because no test asked what was in the prompt. Two
tests here ask now: one asserts neither document's full text reaches the model,
and one asserts the prompt names the diff as the thing that found the change.
"""

import ast
import json
import subprocess
import sys
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from app.interpretation.propose import (
    DROP_EMPTY_QUOTE,
    DROP_MALFORMED,
    DROP_OUT_OF_RANGE,
    DROP_OUTSIDE_THE_CHANGE,
    DROP_UNASKED_VERSION,
    FALLBACK_NO_API_KEY,
    FALLBACK_TRANSPORT_FAILED,
    FALLBACK_UNREADABLE_RESPONSE,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    STATUS_LINE_DRAFT,
    STATUS_LINE_FINAL,
    AnthropicTransport,
    ChangeUnderReview,
    MaterialityJudgement,
    MaterialityRun,
    Span,
    change_under_review,
    judge_materiality,
    judge_materiality_for_company,
    transport_from_environment,
)
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
)

# A repeated sentence, because that is the trap the corpus is built around: the
# same words in two sections mean two things, and text equality alone is not
# enough to tell the analyst which one a judgement rests on. Here the section
# number in front of it is what makes one span unique and the sentence inside it
# ambiguous -- both quotes sit inside the same change, so one fixture drives the
# judgement that shows and the judgement that is withheld.
SENTENCE = "The utility shall file a large load tariff within 60 days."
MONTHLY = "6.2 The utility shall report to the Commission each month."

V1_TEXT = (
    "6.1 The utility shall file a large load tariff within 90 days. " + MONTHLY
)
V2_TEXT = f"6.1 {SENTENCE} {MONTHLY} 9.4 {SENTENCE}"

# The change: section 6.1's deadline moved. Both spans carry their section
# number, which is what the diff hands over.
BEFORE_END = V1_TEXT.index(MONTHLY) - 1
AFTER_END = 4 + len(SENTENCE)

UNIQUE_QUOTE = V2_TEXT[:AFTER_END]  # "6.1 The utility shall file ... 60 days."
REPEATED_QUOTE = SENTENCE  # sits in 6.1 and again in 9.4

SOURCES = {"V1": V1_TEXT, "V2": V2_TEXT}

CHANGE = ChangeUnderReview(
    change_id="CHG-V1-V2-000",
    change_type="modified",
    section="6.1",
    status="FINAL",
    before=Span("V1", 0, BEFORE_END, V1_TEXT[:BEFORE_END]),
    after=Span("V2", 0, AFTER_END, V2_TEXT[:AFTER_END]),
)

WHY = "The filing deadline moved from 90 days to 60 days."


class FakeTransport:
    """Returns canned text and records what it was asked. No network, ever."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


class ExplodingTransport:
    def complete(self, *, system: str, user: str) -> str:
        raise RuntimeError("connection reset by peer")


def one_judgement(citation: dict | None = None, **overrides) -> str:
    """A well-formed judgement citing the unique span, JSON-encoded."""
    payload = {
        "material": True,
        "why": WHY,
        "citation": {
            "version_id": "V2",
            "char_start": 0,
            "char_end": AFTER_END,
            "quoted_text": UNIQUE_QUOTE,
        },
    }
    payload.update(overrides)
    if citation is not None:
        payload["citation"] = {**payload["citation"], **citation}
    return json.dumps(payload)


def run(reply: str, change: ChangeUnderReview = CHANGE) -> MaterialityRun:
    return judge_materiality(FakeTransport(reply), change, SOURCES)


# ---------------------------------------------------------------- offline ---


def test_importing_the_proposer_does_not_import_the_sdk(repo_root: Path):
    """Checked in a fresh process. Import-time cost is paid by every caller.

    The eval harness asserts that no model client is loaded on its path. A
    top-level `import anthropic` here would put the SDK one import away from
    everything, and the harness would be measuring a promise nobody keeps.
    """
    probe = (
        "import sys, app.interpretation.propose;"
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
    assert "anthropic" not in loaded
    assert "app.state.db" not in loaded


def test_the_whole_path_runs_with_the_network_refused(monkeypatch):
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the judgement path opened a socket")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    assert run(one_judgement()).judgement is not None


# --------------------------------------------------- the announced fallback --


def test_no_api_key_means_no_transport():
    assert transport_from_environment({}) is None
    assert transport_from_environment({"ANTHROPIC_API_KEY": ""}) is None


def test_with_no_transport_the_run_says_the_model_path_is_off():
    """best-practices 26. A fallback that does not announce itself is a lie."""
    result = judge_materiality(None, CHANGE, SOURCES)

    assert result.fallback == FALLBACK_NO_API_KEY
    assert result.announcement
    assert "ANTHROPIC_API_KEY" in result.announcement
    # And it judges nothing at all. An unjudged change reading "not material"
    # is the exact degradation this constant exists to prevent.
    assert result.judgement is None
    assert result.withheld is None
    assert result.dropped is None


def test_a_successful_run_announces_nothing():
    result = run(one_judgement())
    assert result.fallback is None
    assert result.announcement is None


# ------------------------------------------------------------ the verifier --


def test_a_judgement_whose_citation_verifies_may_be_shown():
    result = run(one_judgement())

    judgement = result.judgement
    assert judgement.material is True
    assert judgement.why == WHY
    assert judgement.citation_version_id == "V2"
    # actual_text comes from the source, not from the model's quote. Echoing
    # the quote back would prove nothing.
    assert judgement.actual_text == V2_TEXT[:AFTER_END]


def test_a_quote_that_does_not_match_the_source_withholds_the_judgement():
    """The whole reason this module is worth wiring.

    The model said material. The words it quoted are not at the offsets it
    gave. The judgement does not survive that, and the reason takes its place.
    """
    result = run(one_judgement(citation={"quoted_text": "within 30 days."}))

    assert result.judgement is None
    assert result.withheld.reason == REASON_QUOTE_MISMATCH


def test_a_withheld_judgement_cannot_carry_the_judgement():
    """No material field, no why, and slots so neither can be attached later.

    What it does carry is the model's quote against the real bytes at those
    offsets, so the analyst sees the mismatch itself. That is evidence, not an
    assertion.
    """
    result = run(one_judgement(citation={"quoted_text": "something else"}))
    withheld = result.withheld

    assert not hasattr(withheld, "material")
    assert not hasattr(withheld, "why")
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(withheld, "material", True)
    assert WHY not in repr(withheld)
    assert withheld.citation_quote == "something else"
    assert withheld.source_excerpt == V2_TEXT[:AFTER_END]


def test_not_material_passes_the_same_gate_as_material():
    """A false judgement is a judgement. It is not a safe default to fall to."""
    bad = one_judgement(material=False, citation={"quoted_text": "not there"})
    assert run(bad).judgement is None
    assert run(bad).withheld.reason == REASON_QUOTE_MISMATCH

    good = one_judgement(material=False, why="The deadline did not move.")
    assert run(good).judgement.material is False


def test_a_repeated_quote_is_withheld_because_the_citation_cannot_say_which():
    """The corpus trap, on the model's own output.

    The judgement's citation has no occurrence field, so a quote appearing
    twice in the version cannot state which of the two it rests on and the gate
    withholds it. Deriving the occurrence from the offsets the model gave would
    make the check circular -- it would agree with itself every time.
    """
    start = V2_TEXT.index(REPEATED_QUOTE)
    result = run(
        one_judgement(
            citation={
                "char_start": start,
                "char_end": start + len(REPEATED_QUOTE),
                "quoted_text": REPEATED_QUOTE,
            }
        )
    )

    assert result.judgement is None
    assert result.withheld.reason == REASON_AMBIGUOUS_OCCURRENCE


def test_a_citation_is_never_repaired_into_a_plausible_one():
    """The span starts two characters late. Nothing moves it back.

    Repairing offsets to the nearest match is the single most tempting thing to
    write in this module, and it would make the verifier a formality: every
    citation would be adjusted until it passed. The offsets stay inside the
    change, so this is the verifier refusing rather than the shown-text check.
    """
    result = run(one_judgement(citation={"char_start": 2}))

    assert result.judgement is None
    # The offsets it reports are the ones the model gave, not corrected ones.
    assert result.withheld.citation_start == 2


# ------------------------------------------------------------- the dropped --


def test_offsets_past_the_end_of_the_source_are_dropped():
    result = run(one_judgement(citation={"char_end": len(V2_TEXT) + 500}))

    assert result.judgement is None
    assert result.withheld is None
    assert result.dropped.reason == DROP_OUT_OF_RANGE


def test_a_negative_offset_is_dropped():
    assert run(one_judgement(citation={"char_start": -1})).dropped.reason == (
        DROP_OUT_OF_RANGE
    )


def test_an_empty_quote_is_dropped():
    assert run(one_judgement(citation={"quoted_text": "   "})).dropped.reason == (
        DROP_EMPTY_QUOTE
    )


def test_a_version_this_change_does_not_touch_is_dropped():
    assert run(one_judgement(citation={"version_id": "V7"})).dropped.reason == (
        DROP_UNASKED_VERSION
    )


def test_a_citation_outside_the_change_is_dropped():
    """ADR-004 again, at the other end of the call.

    The model was shown one change. A citation into text it was never handed is
    a claim about a document it cannot see, however well the quote verifies --
    and this one verifies perfectly, which is what makes the drop necessary
    rather than incidental.
    """
    start = V2_TEXT.index(MONTHLY)
    result = run(
        one_judgement(
            citation={
                "char_start": start,
                "char_end": start + len(MONTHLY),
                "quoted_text": MONTHLY,
            }
        )
    )

    assert result.judgement is None
    assert result.dropped.reason == DROP_OUTSIDE_THE_CHANGE


def test_citing_a_side_this_change_does_not_have_is_dropped():
    added = ChangeUnderReview(
        change_id="CHG-V1-V2-001",
        change_type="added",
        section="9.4",
        status="FINAL",
        before=None,
        after=Span("V2", 0, AFTER_END, V2_TEXT[:AFTER_END]),
    )
    result = judge_materiality(
        FakeTransport(one_judgement(citation={"version_id": "V1", "char_start": 0,
                                              "char_end": BEFORE_END,
                                              "quoted_text": V1_TEXT[:BEFORE_END]})),
        added,
        SOURCES,
    )
    assert result.dropped.reason == DROP_OUTSIDE_THE_CHANGE


def test_a_judgement_with_no_citation_is_dropped():
    result = run(json.dumps({"material": True, "why": WHY}))
    assert result.dropped.reason == DROP_MALFORMED


def test_a_verdict_that_is_not_true_or_false_is_dropped():
    """"probably" is not an answer this column can hold, and it is not a bool."""
    assert run(one_judgement(material="probably")).dropped.reason == DROP_MALFORMED
    assert run(one_judgement(material=1)).dropped.reason == DROP_MALFORMED


def test_a_judgement_with_no_reason_is_dropped():
    assert run(one_judgement(why="   ")).dropped.reason == DROP_MALFORMED


def test_offsets_that_are_not_integers_are_dropped():
    assert run(one_judgement(citation={"char_start": "0"})).dropped.reason == (
        DROP_MALFORMED
    )


# ------------------------------------------- ADR-004, the drift this repairs --


def test_the_model_is_never_sent_a_whole_document():
    """The repair, pinned.

    The proposer this replaced put both versions' full text in the prompt and
    asked what changed. That is the deterministic diff's job, and a model given
    it will quietly miss changes in a long document with nothing to say so.
    """
    transport = FakeTransport(one_judgement())
    judge_materiality(transport, CHANGE, SOURCES)
    system, user = transport.calls[0]
    prompt = f"{system}\n{user}"

    assert V1_TEXT not in prompt
    assert V2_TEXT not in prompt
    # Not vacuous: the change itself is there, both sides of it.
    assert CHANGE.before.text in prompt
    assert CHANGE.after.text in prompt


def test_the_model_is_told_the_change_was_already_found():
    """It is asked whether this matters, never what moved."""
    transport = FakeTransport(one_judgement())
    judge_materiality(transport, CHANGE, SOURCES)
    prompt = "".join(transport.calls[0]).lower()

    assert "diff" in prompt
    assert "what changed" not in prompt


def test_the_offsets_the_model_must_count_from_are_given_to_it():
    transport = FakeTransport(one_judgement())
    judge_materiality(transport, CHANGE, SOURCES)
    prompt = "".join(transport.calls[0])

    assert f"{CHANGE.after.char_start}-{CHANGE.after.char_end}" in prompt
    assert f"{CHANGE.before.char_start}-{CHANGE.before.char_end}" in prompt


# ----------------------------------------------------------- ADR-005, draft --


def test_the_model_is_never_asked_whether_a_version_is_draft_or_final():
    """The status is read from the record and stated as fact. ADR-005.

    A prompt that asks the model to classify the document has moved the most
    expensive decision in the domain into the least auditable place.
    """
    transport = FakeTransport(one_judgement())
    judge_materiality(transport, CHANGE, SOURCES)
    prompt = "".join(transport.calls[0])

    assert "STATUS: FINAL" in prompt
    assert STATUS_LINE_FINAL in prompt
    # And nothing in the prompt is a question. This is the crude check that
    # catches the tempting edit -- "is this a draft or a final order" appended
    # to a prompt that already knows.
    assert "?" not in prompt


def test_a_draft_change_is_judged_as_a_draft():
    draft = ChangeUnderReview(
        change_id=CHANGE.change_id,
        change_type=CHANGE.change_type,
        section=CHANGE.section,
        status="DRAFT",
        before=CHANGE.before,
        after=CHANGE.after,
    )
    transport = FakeTransport(one_judgement())
    judge_materiality(transport, draft, SOURCES)
    prompt = "".join(transport.calls[0])

    assert "STATUS: DRAFT" in prompt
    assert STATUS_LINE_DRAFT in prompt
    assert STATUS_LINE_FINAL not in prompt


def test_an_unknown_status_is_refused_before_the_model_is_called():
    transport = FakeTransport(one_judgement())
    unknown = ChangeUnderReview(
        change_id=CHANGE.change_id,
        change_type=CHANGE.change_type,
        section=CHANGE.section,
        status="PROPOSED",
        before=CHANGE.before,
        after=CHANGE.after,
    )

    with pytest.raises(ValueError):
        judge_materiality(transport, unknown, SOURCES)

    assert transport.calls == []  # no call was spent on a question we refuse


def test_a_change_with_no_source_to_re_read_is_refused():
    """Absence is denial. A judgement nobody can check is not worth making."""
    with pytest.raises(ValueError):
        judge_materiality(FakeTransport(one_judgement()), CHANGE, {"V1": V1_TEXT})


def test_a_change_with_neither_side_is_refused():
    empty = ChangeUnderReview(
        change_id="CHG-nothing",
        change_type="modified",
        section=None,
        status="FINAL",
        before=None,
        after=None,
    )
    with pytest.raises(ValueError):
        judge_materiality(FakeTransport(one_judgement()), empty, SOURCES)


# --------------------------------------------------------- reading the answer --


def test_an_unreadable_response_judges_nothing_and_says_so():
    result = run("I could not tell whether this matters.")

    assert result.judgement is None
    assert result.fallback == FALLBACK_UNREADABLE_RESPONSE
    assert result.announcement


def test_a_list_of_judgements_is_unreadable_rather_than_the_first_of_them():
    """One change was sent, so one answer is the only readable shape."""
    result = run(f"[{one_judgement()}]")
    assert result.fallback == FALLBACK_UNREADABLE_RESPONSE


def test_a_transport_failure_is_announced_not_raised():
    result = judge_materiality(ExplodingTransport(), CHANGE, SOURCES)

    assert result.fallback == FALLBACK_TRANSPORT_FAILED
    assert "connection reset" in result.announcement
    assert result.judgement is None


def test_a_fenced_json_block_is_still_read():
    assert run(f"```json\n{one_judgement()}\n```").judgement is not None


# ---------------------------------------------- materiality is not confidence --


def test_materiality_carries_no_score_and_no_threshold(repo_root: Path):
    """ADR-006 owns the confidence floor. This is a different judgement.

    A number here would become a second threshold with nobody deciding where it
    sits, and the two would be read as one thing on screen. The verdict is a
    boolean and the gate is the citation, so there is nothing to tune.
    """
    names = {field.name for field in fields(MaterialityJudgement)}
    assert not {name for name in names if "confidence" in name or "score" in name}

    tree = ast.parse(
        (repo_root / "app" / "interpretation" / "propose.py").read_text("utf-8")
    )
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    offenders = {
        name
        for name in assigned
        if "CONFIDENCE" in name.upper() or "THRESHOLD" in name.upper()
    }
    assert offenders == set(), sorted(offenders)


# ------------------------------------------------------------------ tenancy --


def test_the_scoped_entry_point_refuses_an_unscoped_call():
    with pytest.raises(ValueError):
        judge_materiality_for_company(None, "", "CHG-V1-V2-000", transport=None)


def test_the_change_is_built_from_the_row_and_the_stored_text():
    """change_under_review reads the texts; it never asks the caller for them."""

    @dataclass
    class Row:
        id: str = "CHG-V1-V2-000"
        change_type: str = "modified"
        section: str | None = "6.1"
        status: str = "FINAL"
        from_version_id: str = "V1"
        to_version_id: str = "V2"
        before_start: int | None = 0
        before_end: int | None = BEFORE_END
        after_start: int | None = 0
        after_end: int | None = AFTER_END

    built = change_under_review(Row(), SOURCES)

    assert built == CHANGE


def test_a_pure_addition_has_no_before_side():
    @dataclass
    class Row:
        id: str = "CHG-V1-V2-001"
        change_type: str = "added"
        section: str | None = "9.4"
        status: str = "FINAL"
        from_version_id: str = "V1"
        to_version_id: str = "V2"
        before_start: int | None = None
        before_end: int | None = None
        after_start: int | None = 0
        after_end: int | None = AFTER_END

    built = change_under_review(Row(), SOURCES)

    assert built.before is None
    assert built.after.text == V2_TEXT[:AFTER_END]


# ------------------------------------------------------- the client itself --


@dataclass
class FakeBlock:
    type: str
    text: str


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs: dict = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    """Shaped like the SDK client, and about as far as an offline test can go.

    It proves this module sends what its docstring says it sends. It cannot
    prove the API accepts it -- see the honest limit in the module docstring.
    """

    def __init__(self, response):
        self.messages = FakeMessages(response)


@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "end_turn"


def test_the_transport_sends_the_parameters_this_model_family_takes():
    client = FakeClient(FakeResponse([FakeBlock("text", "{}")]))
    AnthropicTransport(client).complete(system="S", user="U")
    sent = client.messages.kwargs

    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["messages"] == [{"role": "user", "content": "U"}]
    assert sent["system"] == "S"
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert not {"budget_tokens", "temperature", "top_p", "top_k"} & set(sent)


def test_the_schema_asks_for_a_verdict_a_reason_and_a_citation():
    client = FakeClient(FakeResponse([FakeBlock("text", "{}")]))
    AnthropicTransport(client).complete(system="S", user="U")
    schema = client.messages.kwargs["output_config"]["format"]["schema"]

    assert set(schema["required"]) == {"material", "why", "citation"}
    assert schema["properties"]["material"]["type"] == "boolean"
    citation = schema["properties"]["citation"]
    assert set(citation["required"]) == {
        "version_id",
        "char_start",
        "char_end",
        "quoted_text",
    }


def test_the_transport_reads_text_blocks_and_ignores_the_rest():
    response = FakeResponse(
        [FakeBlock("thinking", "not the answer"), FakeBlock("text", "{}")]
    )
    assert (
        AnthropicTransport(FakeClient(response)).complete(system="S", user="U") == "{}"
    )


def test_a_refusal_is_not_read_as_an_empty_answer():
    """A declined request returns a normal 200 with nothing in it.

    Read without the check, that is indistinguishable from an answer, and a
    refusal is a failure that must announce itself.
    """
    refused = FakeResponse([], stop_reason="refusal")
    result = judge_materiality(
        AnthropicTransport(FakeClient(refused)), CHANGE, SOURCES
    )

    assert result.fallback == FALLBACK_TRANSPORT_FAILED
    assert "declined" in result.announcement


def test_the_request_shape_matches_the_installed_sdk():
    """The furthest an offline test can go toward proving this call is right.

    It checks every argument name against the client's own signature, and both
    nested shapes against the typed parameters the SDK declares. It does not
    prove the endpoint accepts them -- only the endpoint can do that -- but it
    does catch the two failures that would otherwise wait for a live key: a
    keyword the installed SDK has never heard of, and a nested key invented
    from memory.
    """
    import inspect

    from anthropic.resources.messages import Messages
    from anthropic.types import (
        JSONOutputFormatParam,
        OutputConfigParam,
        ThinkingConfigAdaptiveParam,
    )

    client = FakeClient(FakeResponse([FakeBlock("text", "{}")]))
    AnthropicTransport(client).complete(system="S", user="U")
    sent = client.messages.kwargs

    accepted = set(inspect.signature(Messages.create).parameters)
    assert set(sent) <= accepted, sorted(set(sent) - accepted)

    assert sent["thinking"]["type"] == "adaptive"
    assert set(sent["thinking"]) <= set(ThinkingConfigAdaptiveParam.__annotations__)
    assert set(sent["output_config"]) <= set(OutputConfigParam.__annotations__)
    assert set(sent["output_config"]["format"]) <= set(
        JSONOutputFormatParam.__annotations__
    )


def test_a_key_in_the_environment_builds_a_live_transport():
    # Imported here, not at module scope: every other test in this file must
    # keep passing on a machine that never installed the SDK.
    import anthropic  # noqa: F401

    transport = transport_from_environment({"ANTHROPIC_API_KEY": "sk-ant-not-a-key"})
    assert isinstance(transport, AnthropicTransport)


def test_the_model_id_is_the_one_the_repo_claims():
    assert MODEL_ID == "claude-opus-5"


def test_the_request_carries_no_parameter_this_model_family_rejects(repo_root: Path):
    """budget_tokens, temperature, top_p and top_k are all 400s here.

    Read from the syntax tree, not from the text, for two reasons. The module
    explains in prose why each one is absent, and a substring check would fire
    on the explanation. And a keyword argument is what the API sees -- a name
    inside a comment cannot reach it.
    """
    source = (repo_root / "app" / "interpretation" / "propose.py").read_text("utf-8")
    tree = ast.parse(source)
    rejected = {"budget_tokens", "temperature", "top_p", "top_k"}

    found = set()
    constants = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in rejected:
            found.add(node.arg)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            constants.add(node.value)
            if node.value in rejected:
                found.add(node.value)

    assert found == set(), sorted(found)
    assert "adaptive" in constants  # thinking is on, and the model sets its depth


def test_the_sdk_is_declared_where_a_reviewer_installs_from(repo_root: Path):
    requirements = (repo_root / "requirements.txt").read_text("utf-8")
    assert "anthropic==" in requirements


def test_the_docstring_states_whether_this_ever_ran_for_real(repo_root: Path):
    """An untested integration claimed as tested is the failure we argue against."""
    import app.interpretation.propose as module

    assert "never" in (module.__doc__ or "").lower()
