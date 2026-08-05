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
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.interpretation.propose import (
    DROP_EMPTY_QUOTE,
    DROP_MALFORMED,
    DROP_OUT_OF_RANGE,
    DROP_OUTSIDE_THE_CHANGE,
    DROP_STORED_INCOMPLETE,
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
    materiality_for_company,
    transport_from_environment,
)
from app.state.audit import ACTION_MATERIALITY_SET, ACTOR_MODEL, event_count, verify_chain
from app.state.claims import change_for_company
from app.state.db import init_db, session_scope
from app.state.models import (
    MATERIALITY_MATERIAL,
    MATERIALITY_NOT_MATERIAL,
    AuditEvent,
    Change,
    DocumentVersion,
    Proceeding,
)
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
    REASON_VERSION_UNREADABLE,
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
        materiality_for_company(None, "", "CHG-V1-V2-000", transport=None)


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


# ===========================================================================
# THE VERDICT THAT IS STORED, AND THE CITATION THAT IS NOT
#
# Everything above this line judges and shows. Everything below judges ONCE and
# then reads what it wrote -- and the whole risk of that is the trap ADR-003
# exists to refuse, so the tests are written around the trap rather than around
# the feature.
#
# WHAT IS STORED is the model's OUTPUT: the verdict, the sentence behind it, the
# citation it named, which model said it and when. WHAT IS NOT STORED is the
# fact that the citation verified. That fact is recomputed on every read against
# the bytes in the database at that moment, exactly as app/state/claims.py
# recomputes it for a stored claim. So the test that matters most here is
# test_a_stored_verdict_whose_source_moved_is_withheld: move the bytes, call no
# model at all, and watch the stored verdict refuse to show itself.
#
# The corpus is built by hand rather than seeded. The seed's own texts are long
# and their offsets move whenever the corpus is edited; these two are four
# sentences and the change spans them exactly, so a test that fails here failed
# for a reason about materiality.
# ===========================================================================

COMPANY = "MEP"
RIVAL = "RIVAL"
PROCEEDING = "PRC-STORED"
STORED_CHANGE = "CHG-STORED-1"
DOCKET = "2026-90001"


def _version(version_id: str, source: str, company: str = COMPANY) -> DocumentVersion:
    return DocumentVersion(
        id=version_id,
        company_id=company,
        docket=DOCKET,
        label=f"Filing {version_id}",
        status="FINAL",
        source_text=source,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


@pytest.fixture
def corpus():
    """Two versions and one change between them. No seed, no model, no key."""
    init_db()
    with session_scope() as session:
        session.add(
            Proceeding(
                id=PROCEEDING,
                company_id=COMPANY,
                docket=DOCKET,
                commission="KY PSC",
                subject="Large load tariff",
            )
        )
        session.add(_version("V1", V1_TEXT))
        session.add(_version("V2", V2_TEXT))
        session.add(
            Change(
                id=STORED_CHANGE,
                company_id=COMPANY,
                proceeding_id=PROCEEDING,
                from_version_id="V1",
                to_version_id="V2",
                change_type="modified",
                before_start=0,
                before_end=BEFORE_END,
                after_start=0,
                after_end=AFTER_END,
                section="6.1",
                alignment_confidence=0.98,
                materiality=None,
                status="FINAL",
            )
        )


def _read(reply: str | None = None, **kwargs) -> tuple[MaterialityRun, object]:
    """One render of the materiality block, with its own session.

    Returns the run and the transport, so a test can ask what the model was
    asked as well as what came back. `reply` of None installs no transport at
    all, which is the state a reviewer with no key is in.
    """
    transport = None if reply is None else FakeTransport(reply)
    with session_scope() as session:
        run = materiality_for_company(
            session, COMPANY, STORED_CHANGE, transport=transport, **kwargs
        )
    return run, transport


def _stored() -> Change:
    with session_scope() as session:
        change = change_for_company(session, COMPANY, STORED_CHANGE)
        session.expunge(change)
        return change


def _move_the_bytes() -> None:
    """Edit the cited text in place, leaving its length and offsets alone.

    The same move tests/test_change_view.py makes against a stored claim, for
    the same reason: the citation still addresses real characters, and only the
    words there have changed. A verdict that survived this would be a cached
    verdict.
    """
    with session_scope() as session:
        for version in session.query(DocumentVersion).filter(
            DocumentVersion.company_id == COMPANY
        ):
            if version.id == "V2":
                version.source_text = version.source_text.replace(
                    "within 60 days", "within 30 days"
                )


# ------------------------------------------------------------- what is written --


def test_a_verdict_that_passes_the_gate_is_written_beside_the_column(corpus):
    """Cost one and cost two of ADR-78, closed. The judgement stops vanishing.

    The verdict word goes in the existing column, which is 32 characters and can
    hold it. Everything the verdict rests on goes in the columns beside it,
    because a bare word that cannot show its source is exactly the assertion
    this product refuses.
    """
    run, _ = _read(one_judgement())
    assert run.judgement is not None

    change = _stored()
    assert change.materiality == MATERIALITY_MATERIAL
    assert change.materiality_why == WHY
    assert change.materiality_citation_version_id == "V2"
    assert change.materiality_citation_start == 0
    assert change.materiality_citation_end == AFTER_END
    assert change.materiality_citation_quote == UNIQUE_QUOTE
    assert change.materiality_model_id == MODEL_ID
    assert change.materiality_judged_at is not None
    assert change.materiality_judged_at.tzinfo is not None


def test_not_material_is_written_as_a_verdict_and_not_as_a_blank(corpus):
    """An unjudged change and a change judged harmless are opposite facts.

    Leaving the column NULL for a false verdict would make the two identical in
    the database as well as on screen, and the project list reads this column.
    """
    _read(one_judgement(material=False, why="The deadline did not move."))
    assert _stored().materiality == MATERIALITY_NOT_MATERIAL


def test_the_verdict_is_audited_under_the_one_action_code(corpus):
    """A living, auditable state. The row says what was decided, by what, and when.

    actor_kind is model, so nothing here can be read later as a person's
    judgement -- and app/auth/policy.py treats an act on a change as authorship
    of the claims under it, which is why the code has to be the one spelling.
    """
    with session_scope() as session:
        before = event_count(session, COMPANY)

    _read(one_judgement())

    with session_scope() as session:
        assert event_count(session, COMPANY) == before + 1
        row = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert row.action == ACTION_MATERIALITY_SET
        assert row.actor_kind == ACTOR_MODEL
        assert row.actor_user_id is None
        assert MODEL_ID in row.actor
        assert row.subject_type == "change"
        assert row.subject_id == STORED_CHANGE
        assert WHY in row.reason
        assert row.citation == f"V2:0:{AFTER_END}"
        assert verify_chain(session, COMPANY) is True

    # The moment in the log and the moment on the row are the same moment. Two
    # clocks would let the record disagree with itself about when it knew.
    assert _stored().materiality_judged_at == row.occurred_at


def test_a_judgement_the_gate_withholds_is_never_written(corpus):
    """The model said material and quoted words that are not there.

    Nothing is stored, and nothing is audited. Storing the verdict and refusing
    to show it would leave a row that reads as a finding to anything that
    queries the column instead of the screen -- the project list does exactly
    that.
    """
    with session_scope() as session:
        before = event_count(session, COMPANY)

    run, _ = _read(one_judgement(citation={"quoted_text": "within 30 days."}))
    assert run.withheld is not None

    change = _stored()
    assert change.materiality is None
    assert change.materiality_judged_at is None
    with session_scope() as session:
        assert event_count(session, COMPANY) == before


def test_a_dropped_answer_is_never_written(corpus):
    run, _ = _read(one_judgement(citation={"version_id": "V7"}))
    assert run.dropped is not None
    assert _stored().materiality_judged_at is None


def test_with_no_key_nothing_is_judged_and_nothing_is_written(corpus):
    """The state a reviewer is in. It must not leave a mark either way."""
    with session_scope() as session:
        before = event_count(session, COMPANY)

    run, _ = _read(None)

    assert run.fallback == FALLBACK_NO_API_KEY
    assert _stored().materiality is None
    with session_scope() as session:
        assert event_count(session, COMPANY) == before


# ------------------------------------------------------------- what is read --


def test_the_second_read_uses_the_stored_verdict_and_calls_no_model(corpus):
    """The cost this whole change exists to remove.

    Every render used to be a model call: money, seconds, and two loads able to
    disagree about one change. The second read here answers the same verdict
    with the transport untouched.
    """
    first, _ = _read(one_judgement())
    second, transport = _read(one_judgement())

    assert transport.calls == []
    assert second.judgement is not None
    assert second.judgement.material is first.judgement.material
    assert second.judgement.why == first.judgement.why
    assert second.judgement.model_id == MODEL_ID
    assert second.judgement.judged_at == first.judgement.judged_at


def test_the_stored_verdict_is_read_even_with_the_model_path_off(corpus):
    """A verdict already earned does not disappear because the key did.

    This is the one place the persistence pays for itself twice: the demo has no
    key, and without a store the change screen can only ever say "unjudged".
    """
    _read(one_judgement())
    run, _ = _read(None)

    assert run.fallback is None
    assert run.judgement.why == WHY


def test_the_shown_quote_is_the_sources_bytes_and_not_the_stored_quote(corpus):
    """Reading back the model's own quote would prove nothing about the source."""
    _read(one_judgement())
    run, _ = _read(None)

    assert run.judgement.actual_text == V2_TEXT[:AFTER_END]


# ------------------------------- the trap: this must not become a cached verdict --


def test_a_stored_verdict_whose_source_moved_is_withheld(corpus):
    """THE TEST THIS FEATURE IS WRITTEN AROUND. ADR-003, on a stored verdict.

    The verdict was earned, written and audited. Then the cited bytes changed
    under it. On the very next read -- with no model call, no job, nothing rerun
    -- the gate refuses it and the reason takes its place. A product that
    answered from the column here would be asserting a fact about bytes that
    have moved, which is the single thing ADR-003 exists to refuse.
    """
    _read(one_judgement())
    _move_the_bytes()

    run, transport = _read(None)

    assert run.judgement is None
    assert run.withheld is not None
    assert run.withheld.reason == REASON_QUOTE_MISMATCH
    # The stored quote is shown against what the source really says, which is
    # evidence. The verdict and the sentence behind it are not shown at all.
    assert run.withheld.citation_quote == UNIQUE_QUOTE
    assert run.withheld.source_excerpt == V2_TEXT[:AFTER_END].replace(
        "within 60 days", "within 30 days"
    )
    assert WHY not in repr(run.withheld)
    # And the row is untouched: the refusal is a read, not a correction.
    assert _stored().materiality == MATERIALITY_MATERIAL


def test_a_withheld_stored_verdict_cannot_carry_the_verdict(corpus):
    """Structural, not conventional. The object has no field to leak.

    Same shape as a withheld claim and for the same reason: a template cannot
    render what the object does not have, so a mistake in the screen can only
    fail to render, never assert.

    THE FIRST ASSERTION IS THE ONE THAT MAKES THE OTHERS MEAN ANYTHING, and it
    was missing. Without it this test PASSED with the gate deleted, while every
    test around it went red -- because `run.withheld` is None on that path, and
    every check below reads as satisfied against None: hasattr(None, "material")
    is False, and object.__setattr__(None, ...) raises. A test that passes
    hardest when the feature is gone is worse than no test, because it is
    counted in the green number. TypeError is out of the raises() tuple for the
    same reason: it was never the frozen-slots failure, only None's.
    """
    _read(one_judgement())
    _move_the_bytes()
    run, _ = _read(None)

    assert run.withheld is not None
    assert not hasattr(run.withheld, "material")
    assert not hasattr(run.withheld, "why")
    with pytest.raises(AttributeError):
        object.__setattr__(run.withheld, "material", True)


def test_moved_bytes_and_a_key_earn_a_fresh_verdict_that_replaces_the_stale_one(
    corpus,
):
    """A stale citation is the one thing that buys another model call.

    Cheap in the ordinary case and correct in the interesting one: the verdict
    is re-earned against the text as it now reads, and the row is overwritten so
    the next render is free again.
    """
    _read(one_judgement())
    _move_the_bytes()

    moved = V2_TEXT.replace("within 60 days", "within 30 days")
    fresh = json.dumps(
        {
            "material": False,
            "why": "The deadline moved again, this time in the utility's favour.",
            "citation": {
                "version_id": "V2",
                "char_start": 0,
                "char_end": AFTER_END,
                "quoted_text": moved[:AFTER_END],
            },
        }
    )
    run, transport = _read(fresh)

    assert len(transport.calls) == 1
    assert run.judgement.material is False
    change = _stored()
    assert change.materiality == MATERIALITY_NOT_MATERIAL
    assert change.materiality_citation_quote == moved[:AFTER_END]

    # And the next render is free again.
    _, quiet = _read(one_judgement())
    assert quiet.calls == []


def test_a_stale_verdict_stays_refused_when_the_fresh_call_cannot_answer(corpus):
    """The transport failed. The screen must not fall back to the stored verdict.

    Two refusals are in play and the specific one wins: "this change's stored
    verdict no longer verifies" says more to a reader than "the model did not
    answer just now", and showing the stale verdict because the retry failed is
    the cached verdict arriving through the back door.
    """
    _read(one_judgement())
    _move_the_bytes()

    with session_scope() as session:
        run = materiality_for_company(
            session, COMPANY, STORED_CHANGE, transport=ExplodingTransport()
        )

    assert run.judgement is None
    assert run.withheld.reason == REASON_QUOTE_MISMATCH


def test_a_stored_verdict_naming_a_version_this_change_no_longer_spans_is_withheld(
    corpus,
):
    """Absence is denial. A citation nobody can re-read is not evidence."""
    _read(one_judgement())
    with session_scope() as session:
        change_for_company(
            session, COMPANY, STORED_CHANGE
        ).materiality_citation_version_id = "V9"

    run, _ = _read(None)

    assert run.judgement is None
    assert run.withheld.reason == REASON_VERSION_UNREADABLE


def test_a_stored_verdict_with_no_citation_beside_it_is_dropped(corpus):
    """A bare word in a 32-character column is not a judgement this may show.

    Nothing in the product writes that shape -- the columns are written together
    or not at all -- so a row in this state arrived from a loader or by hand.
    It is named and refused rather than read as a verdict, and it is not
    silently overwritten either: somebody put it there.
    """
    with session_scope() as session:
        change = change_for_company(session, COMPANY, STORED_CHANGE)
        change.materiality = MATERIALITY_MATERIAL
        change.materiality_judged_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

    run, transport = _read(one_judgement())

    assert run.judgement is None
    assert run.withheld is None
    assert run.dropped.reason == DROP_STORED_INCOMPLETE
    assert transport.calls == []
    assert _stored().materiality_why is None


def test_a_stored_word_this_build_does_not_know_is_dropped(corpus):
    """"quite important" is not a verdict, and it is not read as one either."""
    with session_scope() as session:
        change = change_for_company(session, COMPANY, STORED_CHANGE)
        change.materiality = "quite important"
        change.materiality_why = WHY
        change.materiality_citation_version_id = "V2"
        change.materiality_citation_start = 0
        change.materiality_citation_end = AFTER_END
        change.materiality_citation_quote = UNIQUE_QUOTE
        change.materiality_model_id = MODEL_ID
        change.materiality_judged_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

    run, _ = _read(one_judgement())

    assert run.dropped.reason == DROP_STORED_INCOMPLETE


# ------------------------------------------------------------------ tenancy --


def test_another_companys_change_is_refused_rather_than_judged(corpus):
    with session_scope() as session:
        with pytest.raises(ValueError):
            materiality_for_company(
                session, RIVAL, STORED_CHANGE, transport=FakeTransport(one_judgement())
            )

    assert _stored().materiality is None


def test_the_write_lands_in_the_companys_own_chain(corpus):
    _read(one_judgement())
    with session_scope() as session:
        assert event_count(session, RIVAL) == 0


# ---------------------------------------------------------------- the schema --


def test_the_verdict_columns_reach_a_database_that_predates_them(tmp_path: Path):
    """The deploy, not the fresh install. Seven new columns and no ALTER by hand.

    app/state/migrate.py derives the difference between the models and the
    database rather than listing it, so nothing in this change edits that file.
    This test is what turns that claim into a fact for these seven columns: it
    builds today's schema, takes them back out, puts a row in, and migrates.

    THE ROW IS THE POINT. The migration is additive only -- it never drops,
    renames or backfills -- so a change judged before these columns existed must
    come through with NULL in all seven, which says "the schema of the day did
    not record this". Anything else would write a fact into a historical row.

    A NOT NULL column with no default would have been REFUSED here rather than
    added, and "refused" stops a deploy. That is why every one of the seven is
    nullable, and this asserts the empty refusal list rather than assuming it.
    """
    from sqlalchemy import create_engine, inspect, text

    from app.state.migrate import migrate
    from app.state.models import Base

    added = [
        "materiality_why",
        "materiality_citation_version_id",
        "materiality_citation_start",
        "materiality_citation_end",
        "materiality_citation_quote",
        "materiality_model_id",
        "materiality_judged_at",
    ]

    engine = create_engine(f"sqlite:///{tmp_path / 'yesterday.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for column in added:
            connection.execute(text(f"ALTER TABLE changes DROP COLUMN {column}"))
        connection.execute(
            text(
                "INSERT INTO changes (id, company_id, proceeding_id, "
                "from_version_id, to_version_id, change_type, "
                "alignment_confidence, status) VALUES ('CHG-OLD', 'MEP', "
                "'PRC-OLD', 'v1', 'v2', 'modified', 0.9, 'FINAL')"
            )
        )

    # The failure a deploy would have met, asserted before it is closed. Without
    # this half the test would pass against a migration that did nothing.
    with pytest.raises(Exception) as raised:
        with engine.begin() as connection:
            connection.execute(text("SELECT materiality_judged_at FROM changes"))
    assert "no such column" in str(raised.value)

    report = migrate(engine)

    assert report["refused"] == []
    assert [f"changes.{column}" for column in added] == [
        entry for entry in report["columns"] if entry.startswith("changes.")
    ]

    present = {column["name"] for column in inspect(engine).get_columns("changes")}
    assert set(added) <= present

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT materiality, materiality_why, materiality_judged_at "
                "FROM changes WHERE id = 'CHG-OLD'"
            )
        ).one()
    assert row == (None, None, None)


# ------------------------------------------------ the reasoning, kept in the file --


def test_the_docstring_argues_why_a_stored_verdict_is_not_a_cached_one(repo_root: Path):
    """The optimisation this file exists to stop is one line away.

    A later reader sees a verdict in a column, sees the gate run on every read,
    and removes the gate -- it looks like redundant work, and every test that
    does not move the bytes keeps passing. So the argument lives in the
    docstring, at length, and this test refuses to let it be deleted with the
    code it protects.
    """
    import app.interpretation.propose as module

    text = (module.__doc__ or "").lower()
    assert "adr-003" in text
    assert "re-verified on every read" in text
    assert "cached" in text


# ===========================================================================
# EVERY READER OF THE COLUMN, NOT JUST THE ONE THAT WROTE IT
#
# THE BUG THESE TESTS WERE WRITTEN FOR, because it is the one that got through.
#
# Before the verdict was stored, `Change.materiality` was NULL on every row in
# the product. That made every reader of the column trivially safe: there was
# nothing there to leak. The change that started WRITING the column gated
# exactly one reader -- the change screen -- and left the other two reading the
# raw column with no citation, no verify_citation and no re-read of the source:
#
#   app/web/views/projects.py  the project list, one row per change
#   app/chat/tools.py          change_detail, the assistant's own answer
#
# So a verdict earned on the change screen, whose cited bytes then moved, was
# WITHHELD on that screen and printed as fact on the other two. That is a claim
# asserted from the record alone about text that is no longer there, which is
# precisely what ADR-003 exists to refuse, and 2,066 tests stayed green over the
# hole because none of them judged a change and then read a different screen.
#
# THE FIX IS THE CLASS, NOT THE TWO CALL SITES. shown_materiality_for_company()
# is the one way to read this column for display: it runs the same gate, it
# calls no model and it writes nothing. The two surfaces above go through it,
# and test_no_surface_reads_the_materiality_column_raw below walks the syntax
# tree so the THIRD reader -- the one nobody has written yet -- cannot repeat
# the mistake by touching the attribute directly.
# ===========================================================================

READER_PROJECT = "PRJ-STORED"


@pytest.fixture
def attached(corpus):
    """The same corpus, with the change attached to a project and a user to ask.

    The project list and the chat tool both need more scaffolding than the
    change screen does, and both are the point of this section, so it is built
    once here rather than inside each test.
    """
    from app.state.models import Project, ProjectChange, User

    with session_scope() as session:
        session.add(
            Project(
                id=READER_PROJECT,
                company_id=COMPANY,
                name="Large load tariff",
                docket_ref=DOCKET,
                jurisdiction="KY",
                status="active",
                owner="J. Okonkwo",
            )
        )
        session.add(
            ProjectChange(
                company_id=COMPANY,
                project_id=READER_PROJECT,
                change_id=STORED_CHANGE,
                attached_by="J. Okonkwo",
            )
        )
        session.add(
            User(
                id="USR-READER",
                company_id=COMPANY,
                email="j.okonkwo@example.com",
                display_name="J. Okonkwo",
                status="active",
                # Never signed in with, and never checked here. change_detail
                # takes an actor for the scope check and the label; the three
                # credential columns are NOT NULL, so they carry a value that
                # cannot verify rather than a plausible one.
                password_hash="unused",
                password_salt="unused",
                kdf_params="{}",
            )
        )


def _shown():
    """What the gated reader says about the one stored change."""
    from app.interpretation.propose import shown_materiality_for_company

    with session_scope() as session:
        return shown_materiality_for_company(session, COMPANY, [STORED_CHANGE])[
            STORED_CHANGE
        ]


def _project_row():
    """The row the project list would print, through the real function."""
    from app.web.views.projects import _change_rows

    with session_scope() as session:
        rows = _change_rows(session, COMPANY, READER_PROJECT)
    assert len(rows) == 1
    return rows[0]


def _chat_change():
    """The change payload the assistant would answer with, through the real tool."""
    from app.chat.tools import change_detail
    from app.state.models import User

    with session_scope() as session:
        actor = session.query(User).filter(User.id == "USR-READER").one()
        return change_detail(
            session, company_id=COMPANY, actor=actor, change_id=STORED_CHANGE
        )


# ------------------------------------------------- the gate, without a model --


def test_a_reader_that_never_judges_still_shows_a_verdict_that_verifies(attached):
    """The common case. A reader gets the word, and gets it from the source."""
    _read(one_judgement())

    shown = _shown()
    assert shown.judged is True
    assert shown.verdict == MATERIALITY_MATERIAL
    assert shown.reason == ""


def test_a_reader_gate_withholds_a_verdict_whose_source_moved(attached):
    """THE TEST THIS SECTION EXISTS FOR, at the level of the shared gate.

    Judge once, move the cited characters, then read as a screen that cannot
    judge. No transport is installed anywhere in this test, so nothing can
    recover by asking again -- the reader has only the row and the bytes, which
    is exactly what the project list has.
    """
    _read(one_judgement())
    _move_the_bytes()

    shown = _shown()
    assert shown.judged is True
    assert shown.verdict is None
    assert shown.reason == REASON_QUOTE_MISMATCH


def test_a_reader_gate_never_returns_a_bare_absence(attached):
    """Absence is denial. Nothing judged and a refused verdict are not the same.

    A reader handed None for both would print one blank cell for two opposite
    facts, which is the nullable-boolean mistake app/state/models.py refuses in
    the column itself.
    """
    unjudged = _shown()
    assert unjudged.judged is False
    assert unjudged.verdict is None
    assert unjudged.reason != ""

    _read(one_judgement())
    _move_the_bytes()
    refused = _shown()

    assert refused.judged is True
    assert refused.reason != unjudged.reason


def test_a_reader_gate_calls_no_model_and_writes_nothing(attached):
    """It is a READ. Thirty rows must not be thirty model calls or thirty writes.

    The function takes no transport at all -- there is no argument to pass one
    through -- so this asserts the audit chain and the row instead, which is
    what a model call would have moved.
    """
    _read(one_judgement())
    _move_the_bytes()

    with session_scope() as session:
        before = event_count(session, COMPANY)
    stale_judged_at = _stored().materiality_judged_at

    assert _shown().verdict is None

    with session_scope() as session:
        assert event_count(session, COMPANY) == before
    after = _stored()
    assert after.materiality == MATERIALITY_MATERIAL
    assert after.materiality_judged_at == stale_judged_at


def test_a_reader_gate_refuses_a_change_this_company_cannot_read(attached):
    """Absence is denial, again. A missing id is not an unjudged change.

    Dropping the id from the returned map would let a caller print "not
    assessed" for a change it was never allowed to see, which reads as a fact
    about the change rather than as the scope refusal it is.
    """
    from app.interpretation.propose import shown_materiality_for_company

    with session_scope() as session:
        with pytest.raises(ValueError):
            shown_materiality_for_company(session, COMPANY, ["CHG-NOT-OURS"])


def test_a_reader_gate_refuses_an_unscoped_company(attached):
    from app.interpretation.propose import shown_materiality_for_company

    for scope in (None, "", "   ", "%", "_"):
        with session_scope() as session:
            with pytest.raises(ValueError):
                shown_materiality_for_company(session, scope, [STORED_CHANGE])


def test_a_reader_gate_narrows_sources_exactly_as_the_change_screen_does(attached):
    """Parity, so the two surfaces cannot disagree about the same row.

    The judging path narrows the sources to the two versions the change spans,
    so a stored citation pointing anywhere else comes back unreadable rather
    than quietly verifying against a filing nobody was looking at. A reader that
    handed over every version the company owns would be MORE permissive than the
    screen that earned the verdict, and the two would print different words.
    """
    from app.interpretation.propose import shown_materiality_for_company

    _read(one_judgement())

    other = "V-ELSEWHERE"
    with session_scope() as session:
        session.add(_version(other, V2_TEXT))
        change = change_for_company(session, COMPANY, STORED_CHANGE)
        change.materiality_citation_version_id = other

    with session_scope() as session:
        shown = shown_materiality_for_company(session, COMPANY, [STORED_CHANGE])[
            STORED_CHANGE
        ]
    assert shown.verdict is None
    assert shown.reason != ""

    # Not vacuous: the change screen refuses the same row for the same reason.
    run, _ = _read()
    assert run.judgement is None


def test_a_reader_gate_refuses_a_row_that_did_not_come_from_this_product(attached):
    """A word in the column and no citation beside it. Refused, not printed.

    This is the row a loader, a hand-written UPDATE or an older build leaves
    behind. The judging path already names it; the reader has to name it too,
    because the project list is where such a row would actually be seen.
    """
    _read(one_judgement())
    with session_scope() as session:
        change = change_for_company(session, COMPANY, STORED_CHANGE)
        change.materiality_citation_quote = None

    shown = _shown()
    assert shown.judged is True
    assert shown.verdict is None
    assert shown.reason != ""


# --------------------------------------------- the two surfaces that leaked --


def test_the_project_list_withholds_a_verdict_whose_citation_no_longer_verifies(
    attached,
):
    """The leak, on the screen that shows the verdict most often.

    One row per change, no click required. It printed "material" over words that
    are no longer in the document, while the change screen one link away refused
    the very same verdict.
    """
    from app.web.views.projects import MATERIALITY_UNASSESSED, MATERIALITY_WITHHELD

    _read(one_judgement())
    assert _project_row().materiality == MATERIALITY_MATERIAL

    _move_the_bytes()
    row = _project_row()

    assert row.materiality == MATERIALITY_WITHHELD
    assert MATERIALITY_MATERIAL not in row.materiality
    assert row.materiality != MATERIALITY_UNASSESSED
    assert REASON_QUOTE_MISMATCH in row.materiality_reason


def test_the_project_list_paints_a_harmless_verdict_without_the_alarm(attached):
    """The dead branch that came alive, and shipped wearing the wrong colour.

    While the column was permanently NULL the template's else-branch never ran.
    It renders ANY verdict as badge--material, which app/web/views/changes.py
    documents as the ALARM treatment. So the first change ever judged HARMLESS
    would have worn the alarm colour on the project list.
    """
    _read(one_judgement(material=False, why="The deadline did not move."))

    row = _project_row()
    assert row.materiality == MATERIALITY_NOT_MATERIAL
    assert row.materiality_badge != "badge--material"


def test_the_chat_tool_withholds_a_verdict_whose_citation_no_longer_verifies(attached):
    """The same leak, in the answer the assistant composes.

    Worse here than on a screen, because the value is handed to a model that
    will restate it in prose. A stale "material" becomes a sentence a regulator
    reads, with no column beside it to check.
    """
    _read(one_judgement())
    assert _chat_change()["change"]["materiality"] == MATERIALITY_MATERIAL

    _move_the_bytes()
    result = _chat_change()

    assert result["change"]["materiality"] is None
    assert REASON_QUOTE_MISMATCH in result["note"]

    # The verdict is gone from the VALUES, checked one by one rather than by
    # searching the serialised payload: "material" is a substring of the key
    # "materiality", so a dumps() scan reports a leak on every healthy result
    # and would have to be weakened until it caught nothing.
    assert MATERIALITY_MATERIAL not in [
        value for value in result["change"].values() if isinstance(value, str)
    ]


def test_the_chat_tool_tells_the_two_absences_apart(attached):
    """An unjudged change and a refused verdict both send None. The note must not.

    Handing the model a bare null for both would let it write "this change is
    not material" for a change whose verdict was withheld, which is the fluent
    wrong sentence this whole product is built to stop.
    """
    unjudged = _chat_change()["note"]

    _read(one_judgement())
    _move_the_bytes()
    refused = _chat_change()["note"]

    assert unjudged != refused
    assert REASON_QUOTE_MISMATCH not in unjudged
    assert REASON_QUOTE_MISMATCH in refused


# ------------------------------------------ and the reader nobody has written --


def test_no_surface_reads_the_materiality_column_raw(repo_root: Path):
    """The structural guard, in the shape tests/test_tenancy_derived.py uses.

    Two behavioural tests above pin the two surfaces that leaked. Neither says
    anything about the third one. This walks the syntax tree of every module
    under app/ and fails on any attribute read of `.materiality` outside the
    gate itself and the model that declares the column -- so a new screen that
    reaches for the column directly fails here, on the day it is written,
    rather than after it has printed a stale verdict to a regulator.

    Attribute reads only. The seven columns beside it are written by
    _record_verdict and read by _stored_verdict, both inside the gate.

    ONE EXEMPTION, AND IT IS THE GATE ITSELF. Every other exemption that
    suggested itself turned out to be unnecessary, which is why none is here:
    app/state/models.py declares the column as an annotated assignment rather
    than an attribute read, and app/pipeline.py passes materiality=None as a
    keyword at construction. Both were checked rather than assumed. An allowlist
    carrying entries it does not need is a weaker guard, because the day one of
    those files DOES read the column, nothing says so.
    """
    allowed = {Path("app/interpretation/propose.py")}

    offenders = []
    for path in sorted((repo_root / "app").rglob("*.py")):
        relative = path.relative_to(repo_root)
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "materiality":
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == [], (
        "these read Change.materiality without the gate; go through "
        "shown_materiality_for_company: " + ", ".join(offenders)
    )
