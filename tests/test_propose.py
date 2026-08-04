"""The model may propose. Only the verifier may let a proposal be shown.

Every test here runs with no API key and no network. The transport is injected,
so the deterministic fake below stands in for the model and the real client is
never constructed. That is the property the whole suite depends on, and the
first two tests measure it rather than trusting it.
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.interpretation.propose import (
    DROP_ACTION_OUT_OF_VOCABULARY,
    DROP_EMPTY_QUOTE,
    DROP_MALFORMED,
    DROP_OUT_OF_RANGE,
    DROP_UNASKED_VERSION,
    FALLBACK_NO_API_KEY,
    FALLBACK_TRANSPORT_FAILED,
    FALLBACK_UNREADABLE_RESPONSE,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    AnthropicTransport,
    ProposalRun,
    propose_changes,
    propose_changes_for_company,
    transport_from_environment,
)
from app.verification.verifier import (
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
)

# A repeated sentence, because that is the trap the corpus is built around: the
# same words in two sections mean two things, and text equality alone is not
# enough to tell the analyst which one a claim rests on.
SENTENCE = "The utility shall file a large load tariff within 60 days."
V2_TEXT = (
    f"6.1 {SENTENCE} "
    "6.2 The utility shall report to the Commission each month. "
    f"9.4 {SENTENCE}"
)
V1_TEXT = "6.1 The utility shall file a large load tariff within 90 days."

FIRST = V2_TEXT.index(SENTENCE)
SECOND = V2_TEXT.rindex(SENTENCE)
MONTHLY = "The utility shall report to the Commission each month."


@dataclass(frozen=True)
class FakeVersion:
    """Enough of a DocumentVersion for the proposer: id, status, source text."""

    id: str
    status: str
    source_text: str


V1 = FakeVersion("V1", "DRAFT", V1_TEXT)
V2 = FakeVersion("V2", "FINAL", V2_TEXT)


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


def one_proposal(**overrides) -> str:
    """A well-formed proposal citing the second occurrence, JSON-encoded."""
    import json

    proposal = {
        "statement": "The filing deadline is 60 days.",
        "action": "comply",
        "version_id": "V2",
        "char_start": SECOND,
        "char_end": SECOND + len(SENTENCE),
        "quoted_text": SENTENCE,
        "occurrence": 1,
    }
    proposal.update(overrides)
    return json.dumps([proposal])


def run(reply: str, **kwargs) -> ProposalRun:
    return propose_changes(FakeTransport(reply), V1, V2, **kwargs)


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
        raise AssertionError("the proposer path opened a socket")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    result = run(one_proposal())
    assert len(result.proposed) == 1


# --------------------------------------------------- the announced fallback --


def test_no_api_key_means_no_transport():
    assert transport_from_environment({}) is None
    assert transport_from_environment({"ANTHROPIC_API_KEY": ""}) is None


def test_with_no_transport_the_run_says_the_model_path_is_off():
    """best-practices 26. A fallback that does not announce itself is a lie."""
    result = propose_changes(None, V1, V2)

    assert result.fallback == FALLBACK_NO_API_KEY
    assert result.announcement
    assert "model" in result.announcement.lower()
    assert "ANTHROPIC_API_KEY" in result.announcement
    # And it proposes nothing at all. Seeded claims dressed as model output is
    # the exact degradation this constant exists to prevent.
    assert result.proposed == ()
    assert result.withheld == ()
    assert result.dropped == ()


def test_a_successful_run_announces_nothing():
    result = run(one_proposal())
    assert result.fallback is None
    assert result.announcement is None


# ------------------------------------------------------------ the verifier --


def test_a_proposal_whose_citation_verifies_may_be_shown():
    result = run(one_proposal())

    assert len(result.proposed) == 1
    claim = result.proposed[0]
    assert claim.statement == "The filing deadline is 60 days."
    assert claim.citation_version_id == "V2"
    assert claim.cited_occurrence == 1
    # actual_text comes from the source, not from the model's quote. Echoing
    # the quote back would prove nothing.
    assert claim.actual_text == V2_TEXT[SECOND : SECOND + len(SENTENCE)]


def test_a_quote_that_does_not_match_the_source_is_withheld():
    result = run(one_proposal(quoted_text="The utility shall file within 30 days."))

    assert result.proposed == ()
    assert len(result.withheld) == 1
    assert result.withheld[0].reason == REASON_QUOTE_MISMATCH


def test_a_withheld_proposal_cannot_carry_what_it_would_have_said():
    """No statement field, and slots so none can be attached at runtime.

    What it does carry is the quote against the real bytes at those offsets, so
    the analyst sees the mismatch itself. That is evidence, not an assertion.
    """
    result = run(one_proposal(quoted_text="something else entirely"))
    withheld = result.withheld[0]

    assert not hasattr(withheld, "statement")
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(withheld, "statement", "leaked")
    assert "filing deadline" not in repr(withheld)
    assert withheld.citation_quote == "something else entirely"
    assert withheld.source_excerpt == V2_TEXT[SECOND : SECOND + len(SENTENCE)]


def test_a_repeated_quote_with_no_occurrence_is_withheld():
    """The corpus trap, survived on the model path as well as the seeded one."""
    result = run(one_proposal(occurrence=None))

    assert result.proposed == ()
    assert result.withheld[0].reason == REASON_AMBIGUOUS_OCCURRENCE


def test_the_wrong_occurrence_is_withheld_even_though_the_words_match():
    result = run(one_proposal(char_start=FIRST, char_end=FIRST + len(SENTENCE)))
    # Cites section 6.1's offsets while claiming to rely on occurrence 1.
    assert result.proposed == ()
    assert result.withheld[0].reason == REASON_AMBIGUOUS_OCCURRENCE


def test_a_unique_quote_needs_no_occurrence():
    start = V2_TEXT.index(MONTHLY)
    result = run(
        one_proposal(
            statement="Reporting is monthly.",
            char_start=start,
            char_end=start + len(MONTHLY),
            quoted_text=MONTHLY,
            occurrence=None,
        )
    )
    assert len(result.proposed) == 1


# ------------------------------------------------------------- the dropped --


def test_offsets_past_the_end_of_the_source_are_dropped():
    result = run(one_proposal(char_end=len(V2_TEXT) + 500))

    assert result.proposed == ()
    assert result.withheld == ()
    assert [d.reason for d in result.dropped] == [DROP_OUT_OF_RANGE]


def test_a_negative_offset_is_dropped():
    result = run(one_proposal(char_start=-1))
    assert [d.reason for d in result.dropped] == [DROP_OUT_OF_RANGE]


def test_an_empty_quote_is_dropped():
    result = run(one_proposal(quoted_text="   "))
    assert [d.reason for d in result.dropped] == [DROP_EMPTY_QUOTE]


def test_a_version_the_caller_did_not_ask_about_is_dropped():
    result = run(one_proposal(version_id="V7"))
    assert [d.reason for d in result.dropped] == [DROP_UNASKED_VERSION]


def test_a_proposal_missing_its_citation_is_dropped():
    import json

    reply = json.dumps([{"statement": "Something changed.", "action": "comply"}])
    result = run(reply)
    assert [d.reason for d in result.dropped] == [DROP_MALFORMED]


def test_offsets_that_are_not_integers_are_dropped():
    result = run(one_proposal(char_start="12"))
    assert [d.reason for d in result.dropped] == [DROP_MALFORMED]


def test_a_malformed_proposal_is_never_repaired_into_a_plausible_one():
    """The quote sits two characters away. Nothing moves it there.

    Repairing offsets to the nearest match is the single most tempting thing to
    write in this module, and it would make the verifier a formality: every
    citation would be adjusted until it passed.
    """
    result = run(one_proposal(char_start=SECOND - 2, char_end=SECOND - 2 + len(SENTENCE)))

    assert result.proposed == ()
    assert len(result.withheld) == 1
    # The offsets it reports are the ones the model gave, not corrected ones.
    assert result.withheld[0].citation_start == SECOND - 2


def test_one_bad_proposal_does_not_take_the_good_ones_with_it():
    import json

    good = json.loads(one_proposal())[0]
    bad = dict(good, char_end=len(V2_TEXT) + 1)
    result = run(json.dumps([good, bad]))

    assert len(result.proposed) == 1
    assert len(result.dropped) == 1


# ----------------------------------------------------------- ADR-005, draft --


def test_the_model_is_never_asked_whether_a_version_is_draft_or_final():
    """The status is read from the record and dispatched on. ADR-005.

    A prompt that asks the model to classify the document has moved the most
    expensive decision in the domain into the least auditable place.
    """
    transport = FakeTransport("[]")
    result = propose_changes(transport, V1, V2)
    system, user = transport.calls[0]
    prompt = f"{system}\n{user}"

    # FINAL, so the only action on offer is comply. The draft words must not
    # even appear -- their presence would mean the model was handed the choice.
    assert result.action_vocabulary == ("comply",)
    assert "comply" in prompt
    assert "monitor" not in prompt
    assert "comment" not in prompt
    # Stated as a fact read from the record, for both versions.
    assert "STATUS: FINAL" in prompt
    assert "STATUS: DRAFT" in prompt
    # And nothing in the prompt is a question. This is the crude check that
    # catches the tempting edit -- "is this a draft or a final order" appended
    # to a prompt that already knows.
    assert "?" not in prompt


def test_a_draft_gets_the_draft_vocabulary():
    draft = FakeVersion("V2", "DRAFT", V2_TEXT)
    transport = FakeTransport("[]")
    result = propose_changes(transport, V1, draft)
    prompt = "".join(transport.calls[0])

    assert result.action_vocabulary == ("monitor", "comment")
    assert "monitor" in prompt and "comment" in prompt
    assert "comply" not in prompt


def test_an_action_outside_the_status_vocabulary_is_dropped():
    result = run(one_proposal(action="comment"))  # comment on a FINAL order
    assert result.proposed == ()
    assert [d.reason for d in result.dropped] == [DROP_ACTION_OUT_OF_VOCABULARY]


def test_an_unknown_status_is_refused_before_the_model_is_called():
    transport = FakeTransport(one_proposal())
    unknown = FakeVersion("V2", "PROPOSED", V2_TEXT)

    with pytest.raises(ValueError):
        propose_changes(transport, V1, unknown)
    with pytest.raises(ValueError):
        propose_changes(transport, FakeVersion("V1", "", V1_TEXT), V2)

    assert transport.calls == []  # no call was spent on a question we refuse


# ------------------------------------------------------- proposing nothing --


def test_the_model_may_propose_nothing():
    """A proposer that always proposes is a proposer that invents."""
    result = run("[]")

    assert result.proposed == ()
    assert result.withheld == ()
    assert result.dropped == ()
    assert result.fallback is None  # nothing went wrong; there was nothing to say


def test_an_unreadable_response_proposes_nothing_and_says_so():
    result = run("I could not find anything of note in these documents.")

    assert result.proposed == ()
    assert result.fallback == FALLBACK_UNREADABLE_RESPONSE
    assert result.announcement


def test_a_transport_failure_is_announced_not_raised():
    result = propose_changes(ExplodingTransport(), V1, V2)

    assert result.fallback == FALLBACK_TRANSPORT_FAILED
    assert "connection reset" in result.announcement
    assert result.proposed == ()


def test_a_fenced_json_block_is_still_read():
    result = run(f"```json\n{one_proposal()}\n```")
    assert len(result.proposed) == 1


# ------------------------------------------------------------------ tenancy --


def test_the_scoped_entry_point_refuses_an_unscoped_call():
    with pytest.raises(ValueError):
        propose_changes_for_company(
            None, "", from_version_id="V1", to_version_id="V2", transport=None
        )


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
    client = FakeClient(FakeResponse([FakeBlock("text", "[]")]))
    AnthropicTransport(client).complete(system="S", user="U")
    sent = client.messages.kwargs

    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["messages"] == [{"role": "user", "content": "U"}]
    assert sent["system"] == "S"
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert not {"budget_tokens", "temperature", "top_p", "top_k"} & set(sent)


def test_the_transport_reads_text_blocks_and_ignores_the_rest():
    response = FakeResponse(
        [FakeBlock("thinking", "not the answer"), FakeBlock("text", "[]")]
    )
    assert AnthropicTransport(FakeClient(response)).complete(system="S", user="U") == "[]"


def test_a_refusal_is_not_read_as_an_empty_answer():
    """A declined request returns a normal 200 with nothing in it.

    Read without the check, that is indistinguishable from the model correctly
    finding nothing to say -- and the second is a result while the first is a
    failure that must announce itself.
    """
    refused = FakeResponse([], stop_reason="refusal")
    result = propose_changes(AnthropicTransport(FakeClient(refused)), V1, V2)

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

    client = FakeClient(FakeResponse([FakeBlock("text", "[]")]))
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
    import ast

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
