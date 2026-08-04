"""One turn of chat: screen, then the model with tools, then the reply and pills.

THE ORDER IS THE DESIGN. persona.screen() runs before anything else and cannot
be skipped. It is a regular expression, so it costs nothing, it cannot be talked
out of its answer, and the refusal it produces is auditable as a *deterministic*
decision rather than as a model's mood on the day. Everything expensive --
the model, the tools, the database reads behind them -- sits behind it.

THE MODEL NEVER SUPPLIES IDENTITY, AND THIS FILE DOES NOT ENFORCE THAT ITSELF.
Every tool call goes through one dispatcher --
``dispatch(session, company_id=..., actor=..., tool=..., arguments=...)`` -- and
company_id and actor are injected here from the signed-in session while the
model supplies only the tool name and the other arguments. app/chat/tools.py
owns what happens to those arguments: it refuses a call that names an identity
argument, records the attempt in the audit chain under its own action code, and
halts the turn.

This module deliberately does NOT strip identity arguments before dispatching
them. Stripping would look safer and be worse: the call would succeed quietly
and the injection attempt would vanish from the chain, leaving the one control
that can see an attack unable to report it. Passing the model's arguments
through untouched is what keeps the refusal, the audit row and the halt in one
place instead of two that can disagree.

OFFLINE BY DEFAULT. The transport is injected and the SDK is imported inside the
factory, so importing this module reaches nothing and `make test` passes with no
key and no network. With no key configured the surface SAYS the assistant is
unavailable. It does not answer from a canned script that reads like a real
answer -- best-practices.html section 26, and the one degradation that would
make the whole submission dishonest. tests/test_chat_agent.py asserts the
announcement rather than trusting this paragraph.

WHAT IS NOT PROVEN HERE. This path has never run against the real API in this
repository. What the tests prove is the loop: that the screen comes first, that
identity is injected and never accepted, that the step cap holds, that a
withheld count is surfaced whether or not the model mentions it, and that every
turn ends with somewhere to go. What they cannot prove is anything only the
endpoint can answer -- that claude-opus-5 accepts this combination of
parameters, that the tool_use blocks parse first time, or how any of it behaves
under a rate limit. AnthropicTransport is unexercised code sitting behind
tested code, and it is written that way round on purpose.

WHAT THIS MODULE DOES NOT OWN. Tone, rules and the refusal wording are
app/chat/persona.py's, used from there rather than restated -- there is one
place where the voice is defined. Running a tool, vetting its arguments and
auditing a refusal are app/chat/tools.py's, reached through the injected
dispatcher. What is left here is the loop itself: the order, the cap, the
counting, the coverage note and the pills. The chat_messages row is the
endpoint's to write; what this returns is the turn, including the message_id a
thumb will attach to.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from app.chat import persona, pills as pill_module
from app.chat.pills import Consulted
from app.state.audit import ACTOR_USER, record_event
from app.state.identity import permissions_for_user
from app.state.models import PERMISSION_CODES

# The tenant guard every read in this codebase goes through, imported rather
# than copied. Two copies of a scope check drift, and the copy nobody audited is
# the one that leaks.
from app.state.queries import _require_scope

# --------------------------------------------------------------------------- #
# The model call                                                              #
# --------------------------------------------------------------------------- #

#: Pinned, with no date suffix, matching the alias the client docs publish and
#: the id app/interpretation/propose.py already sends. Which model answered is
#: part of the record; a deployment that swapped it from the environment would
#: make every earlier transcript unreproducible.
MODEL_ID = "claude-opus-5"

#: Thinking and reply share this budget. A clerk turn is three sentences and a
#: handful of tool calls, so the ceiling is low on purpose -- it is well under
#: the point where a non-streaming request risks an HTTP timeout, which is why
#: this stays a plain create() rather than a stream.
MAX_OUTPUT_TOKENS = 8000

#: Thinking depth. Not the default `high`: this is lookup and summary over a
#: handful of read-only tools with a closed vocabulary, not open-ended agentic
#: work, and the expensive judgements in this product -- does the citation
#: verify, may this person approve -- are made in Python before the model ever
#: sees a claim. Raise it if the tool-choice quality disappoints; do not raise
#: it hoping for better prose, which is the persona's job.
EFFORT = "medium"

#: How many rounds of tool calls one turn may take. Four is enough for the
#: deepest real path -- find the project, open it, read the change, find the
#: owner -- and short enough that a confused turn ends in seconds. When the cap
#: is reached the reply SAYS the answer is incomplete rather than presenting the
#: partial one as final.
MAX_TOOL_STEPS = 4

# Named degradations. Each is a state the product must be able to say out loud,
# and each has an announcement that goes into the reply the person reads.
FALLBACK_NO_API_KEY = "CHAT_OFF_NO_API_KEY"
FALLBACK_NO_TOOLSET = "CHAT_OFF_NO_TOOLSET"
FALLBACK_TRANSPORT_FAILED = "CHAT_DEGRADED_TRANSPORT_FAILED"
FALLBACK_UNREADABLE_RESPONSE = "CHAT_DEGRADED_UNREADABLE_RESPONSE"
FALLBACK_MODEL_DECLINED = "CHAT_DEGRADED_MODEL_DECLINED"
FALLBACK_STEP_CAP = "CHAT_DEGRADED_STEP_CAP"
FALLBACK_TOOL_FAILED = "CHAT_DEGRADED_TOOL_FAILED"
FALLBACK_TOOL_HALTED = "CHAT_HALTED_BY_TOOL_LAYER"
FALLBACK_UNCOUNTED_WITHHELD = "CHAT_DEGRADED_UNCOUNTED_WITHHELD"

ANNOUNCEMENT_NO_API_KEY = (
    "The assistant is unavailable: this deployment has no ANTHROPIC_API_KEY, so "
    "no question was sent and no source was consulted. Every screen still works "
    "and holds the same record."
)
ANNOUNCEMENT_NO_TOOLSET = (
    "The assistant is unavailable: it has no tools to read the workspace with, "
    "so it would be answering from nothing. Use the screens."
)
ANNOUNCEMENT_TRANSPORT_FAILED = (
    "The assistant could not reach the model, so nothing was consulted and "
    "nothing below is an answer. The error was: {error}"
)
ANNOUNCEMENT_UNREADABLE_RESPONSE = (
    "The model answered in a shape this turn could not read, so there is no "
    "reply to show. Nothing was guessed from it."
)
ANNOUNCEMENT_MODEL_DECLINED = (
    "The model declined to answer that, so nothing was consulted. Ask a "
    "narrower question about this workspace, or use the screens."
)
ANNOUNCEMENT_STEP_CAP = (
    "I stopped after {steps} rounds of look-ups, so this answer is incomplete "
    "rather than finished. Ask again for one part of it, or open the screen."
)
ANNOUNCEMENT_TOOL_HALTED = (
    "I stopped this turn. {reason} Nothing further was read, and nothing here "
    "is an answer."
)
ANNOUNCEMENT_UNCOUNTED_WITHHELD = (
    "At least one source did not report how much it withheld, so treat any "
    "count above as a floor and not a total. That is a defect, not an empty "
    "queue."
)
WITHHELD_NOTE = (
    "{count} claim{plural} withheld because a citation did not verify. The "
    "reason is in the review queue, not in this answer."
)

#: Not a degradation, so it carries no fallback code: an empty box is a person
#: pressing send, and the answer is the suggestions rather than an apology.
EMPTY_MESSAGE_REPLY = "You have not asked anything yet. Take one of these, or type a question."

#: The two arguments the tool layer injects. A tool that declares either in its
#: own schema is refused at registration: it would be inviting the model to
#: supply what only the session may.
INJECTED = ("company_id", "actor")

# What the person typed is deliberately absent from the audit row. See
# _audit_refusal.
SUBJECT_TYPE = "chat_message"


# --------------------------------------------------------------------------- #
# The shapes that cross the transport boundary                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for. ``arguments`` are untrusted input."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelTurn:
    """What came back from one model round.

    ``content`` is the provider's own content list, kept opaque and echoed back
    into the next request unchanged. That matters beyond convenience: on this
    model family the thinking blocks must be replayed exactly as received, and
    the way to guarantee that is never to rebuild them.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    content: Any = None


class Transport(Protocol):
    """One round with a model: prompt and history in, text and tool calls out.

    Deliberately narrow. Everything above it -- the screen, the injection, the
    counting, the pills -- is pure and runs in CI, and everything the network
    touches is on the far side of this one method. That is what makes the fake
    in tests/test_chat_agent.py a fair stand-in rather than a mock of something
    more interesting.
    """

    def converse(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn: ...


class Dispatcher(Protocol):
    """Runs one tool the model chose, with identity the model never supplies.

    The signature is app/chat/tools.py's dispatch(), which is the real one. It
    is a seam rather than a direct call for the same reason the transport is:
    the loop has to be exercisable with no database and no corpus.

    The dispatcher owns argument vetting, permission refusal and the audit row
    for an attempt. This module hands it what the model produced, untouched.
    """

    def __call__(
        self,
        session: Any,
        *,
        company_id: str,
        actor: Any,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may CHOOSE. It does not say how the tool runs.

    Declaration lives here and execution lives in app/chat/tools.py, because
    that module's registry carries no description or JSON schema and a model
    that cannot read what a tool is for chooses badly. default_toolset() derives
    every field below from that registry rather than restating it, so a tool
    added there appears here without an edit.

    ``permission`` is the code a person must hold for this tool to be OFFERED.
    It is a courtesy and not the control: the gate is inside the tool, where it
    refuses and writes its own row. This only stops the model spending a round
    on a call that will be refused.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    permission: str = ""

    def __post_init__(self) -> None:
        declared = set((self.input_schema or {}).get("properties", {}))
        offending = sorted(declared.intersection(INJECTED))
        if offending:
            raise ValueError(
                f"tool {self.name!r} declares {offending} in its own schema. "
                "The tool layer injects identity from the signed-in session; a "
                "tool that advertises it is asking the model to supply it, and "
                "a model can be talked into supplying somebody else's."
            )
        if self.permission and self.permission not in PERMISSION_CODES:
            raise ValueError(
                f"tool {self.name!r} is gated on {self.permission!r}, which is "
                f"not a permission this product defines. The codes are "
                f"{', '.join(PERMISSION_CODES)}."
            )

    def as_wire(self) -> dict[str, Any]:
        """The definition handed to the model. Identity is not in it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True)
class Turn:
    """One answer, and everything the screen and the transcript need.

    ``fallback`` names the FIRST degradation this turn hit and never leaves the
    server. It is not part of the wire contract -- as_dict() returns exactly the
    five agreed keys -- but every degradation it names is also announced in
    ``reply``, where the person can read it. A silent fallback is the failure
    this field exists to make impossible to ship.
    """

    reply: str
    pills: list[dict[str, str]]
    used: list[dict[str, Any]]
    withheld: int
    message_id: str
    fallback: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "pills": self.pills,
            "used": self.used,
            "withheld": self.withheld,
            "message_id": self.message_id,
        }


class ToolsetUnavailable(RuntimeError):
    """Raised when the tool registry cannot be loaded. Never answered around."""


# --------------------------------------------------------------------------- #
# The provider, imported only when there is a key to use it with              #
# --------------------------------------------------------------------------- #

ENV_API_KEY = "ANTHROPIC_API_KEY"


class AnthropicTransport:
    """The real thing. Unexercised: no key has ever been held in this repo.

    Written against the installed SDK (0.120), not from memory. Two parameters
    are current and would have been wrong a year ago: thinking is
    ``{"type": "adaptive"}`` -- budget_tokens is rejected outright on this model
    -- and effort lives inside ``output_config`` rather than at the top level.
    Sampling parameters are not sent at all, because this model refuses them.
    """

    def __init__(self, client: Any):
        self._client = client

    def converse(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        response = self._client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
        )
        # stop_reason is read before content on purpose: this model's classifiers
        # can decline a request and return a 200 with an empty content list, and
        # code that indexes content[0] first would raise instead of reporting it.
        stop_reason = getattr(response, "stop_reason", "") or ""
        blocks = list(getattr(response, "content", None) or ())
        text = "".join(
            block.text for block in blocks if getattr(block, "type", "") == "text"
        )
        calls = tuple(
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
            for block in blocks
            if getattr(block, "type", "") == "tool_use"
        )
        return ModelTurn(
            text=text, tool_calls=calls, stop_reason=stop_reason, content=blocks
        )


def transport_from_environment() -> Transport | None:
    """A transport, or None when there is no key. None is a state, not an error.

    The SDK is imported here rather than at module scope so that importing this
    module costs nothing and reaches nothing. A caller that gets None must
    announce it; run_turn does.
    """
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        return None
    import anthropic  # imported late, and only when a key exists

    return AnthropicTransport(anthropic.Anthropic(api_key=key))


#: Python type to JSON Schema type, for turning the tool layer's argument table
#: into something a model can read. bool is checked before int deliberately,
#: because bool IS an int in Python and a limit of `true` would otherwise be
#: declared as an integer -- the same trap app/chat/tools.py names in its own
#: type table.
_JSON_TYPES: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (str, "string"),
)


def _json_type(kinds: tuple[type, ...]) -> str:
    for kind, name in _JSON_TYPES:
        if kind in kinds:
            return name
    return "string"


def _first_line(text: str | None) -> str:
    """A tool's own docstring summary, used as its description.

    Derived rather than restated. A description written here would drift from
    what the tool does the first time somebody changes one and not the other,
    and the model chooses tools by reading exactly this string.
    """
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def default_toolset() -> dict[str, ToolSpec]:
    """What the model may choose, derived from app/chat/tools.py's registry.

    Every field comes from that module: the names and argument allowlists from
    REGISTRY, the types from its argument table, the descriptions from the
    tools' own docstrings. Nothing is restated, so a tool added there is offered
    here without an edit, with one exception noted in _TOOL_PERMISSIONS.

    Raises rather than returning an empty registry. An empty one would produce a
    Clarke that answers every question with "nothing on the record", which is the
    worst failure available: it reads exactly like a working product reporting
    an empty workspace.
    """
    try:
        from app.chat import tools as tool_module  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - a sibling module may be mid-edit
        raise ToolsetUnavailable(f"app/chat/tools.py is not importable: {exc}") from exc

    registry = getattr(tool_module, "REGISTRY", None)
    types = getattr(tool_module, "_ARGUMENT_TYPES", {})
    if not isinstance(registry, Mapping) or not registry:
        raise ToolsetUnavailable(
            "app/chat/tools.py must publish REGISTRY as a non-empty mapping of "
            "name to Tool."
        )

    # The one thing the registry does not carry. tools.py knows create_project
    # is gated -- it calls policy.require itself -- but publishes only a
    # `writes` flag, so the code is imported from there rather than spelled
    # again here. A gate added to another tool and not added to this map costs
    # one wasted round and an audited refusal, never a leak. The handoff is for
    # tools.Tool to carry its own permission.
    gates = {"create_project": getattr(tool_module, "PERMISSION_PROJECT_CREATE", "")}

    toolset: dict[str, ToolSpec] = {}
    for name, tool in registry.items():
        properties = {
            argument: {"type": _json_type(types.get(argument, (str,)))}
            for argument in sorted(getattr(tool, "arguments", ()))
        }
        toolset[name] = ToolSpec(
            name=name,
            description=_first_line(getattr(getattr(tool, "run", None), "__doc__", "")),
            input_schema={
                "type": "object",
                "properties": properties,
                "required": [],
            },
            permission=gates.get(name, ""),
        )
    return toolset


def default_dispatcher() -> Dispatcher:
    """app/chat/tools.py's dispatch(), which is where identity is enforced."""
    try:
        from app.chat import tools as tool_module  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - a sibling module may be mid-edit
        raise ToolsetUnavailable(f"app/chat/tools.py is not importable: {exc}") from exc

    dispatch = getattr(tool_module, "dispatch", None)
    if not callable(dispatch):
        raise ToolsetUnavailable("app/chat/tools.py must publish dispatch().")
    return dispatch


# --------------------------------------------------------------------------- #
# The turn                                                                    #
# --------------------------------------------------------------------------- #


def _new_message_id() -> str:
    """A chosen string, because a thumbs-down comes back carrying it."""
    return f"msg_{uuid.uuid4().hex[:16]}"


def _audit_refusal(session, *, company_id: str, actor, screened, message_id: str) -> None:
    """Record that the deterministic screen turned this turn away.

    WHAT IS NOT WRITTEN: the message. The reason and the action code are the
    facts a reviewer needs -- somebody tried to override the instructions, and a
    regular expression stopped it before a token was spent. The prose they typed
    is theirs, and this table is append-only and cannot be redacted afterwards,
    so a password pasted into a chat box would sit in the chain for ever. The
    verdict is the record; the text is not.

    The action code comes from the screen rather than from a constant here.
    app/chat/persona.py owns the two chat refusal codes today, and restating
    them in this file would create the second spelling that
    app/state/audit.py's own comments describe as the failure the ACTION_
    constants were consolidated to stop.
    """
    record_event(
        session,
        company_id=company_id,
        actor=actor.email,
        action=screened.audit_action,
        subject_type=SUBJECT_TYPE,
        subject_id=message_id,
        reason=screened.reason,
        actor_user_id=actor.id,
        actor_kind=ACTOR_USER,
    )


def _counts(name: str, result: Any) -> tuple[int, int | None]:
    """(found, withheld) from a tool result, or a refusal to guess either.

    app/chat/tools.py writes both keys on EVERY result, refusals included, from
    one constructor -- so a well-behaved dispatcher always satisfies this. The
    read is strict anyway, because this loop must work with any dispatcher and
    the two failure modes are the ones a transcript must never hide: a found
    count nobody measured, printed as though somebody had, and a missing
    withheld count read as nothing withheld.

    A missing `found` raises: the result is unreadable and the model is told so.
    A missing `withheld` returns None: the count IS readable, and the turn
    escalates around it rather than throwing the answer away.
    """
    if not isinstance(result, Mapping):
        raise ValueError(
            f"tool {name!r} returned {type(result).__name__}, not a mapping "
            "carrying an integer 'found' count."
        )
    found = result.get("found")
    if not isinstance(found, int) or isinstance(found, bool):
        raise ValueError(
            f"tool {name!r} returned no integer 'found' count. The turn reports "
            "what each tool consulted and will not invent the number."
        )
    withheld = result.get("withheld")
    if not isinstance(withheld, int) or isinstance(withheld, bool):
        return found, None
    return found, withheld


def _tool_result_block(call_id: str, payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": payload if isinstance(payload, str) else json.dumps(payload, default=str),
        "is_error": is_error,
    }


def run_turn(
    session,
    *,
    company_id: str,
    actor,
    company_name: str,
    message: str,
    surface: str,
    project_id: str | None = None,
    toolset: Mapping[str, ToolSpec] | None = None,
    dispatch: Dispatcher | None = None,
    transport: Transport | None = None,
    max_steps: int = MAX_TOOL_STEPS,
) -> Turn:
    """Answer one message. Returns the whole turn, and writes no chat row.

    company_name is a parameter rather than a lookup because there is no
    companies table: app/web/deps.py resolves the display name from the loaded
    corpus and falls back to the id, and asking a second module the same
    question is how two answers appear. The caller has it already.

    transport=None means the model path is off, and the reply says so. It is
    NOT an error: `make run` on a machine with no key still serves every screen,
    and the chat box has to be honest about being switched off rather than
    hidden, or a reviewer will assume it failed.
    """
    _require_scope(company_id)
    message_id = _new_message_id()

    # First, and unskippable. Everything below this line costs money or a query.
    screened = persona.screen(message or "")

    # An empty box is not a question. It never reaches the model -- a message
    # with no content is a 400 there, and a turn that spends a request on
    # nothing is a turn somebody pays for twice.
    if not (message or "").strip():
        return Turn(
            reply=EMPTY_MESSAGE_REPLY,
            pills=pill_module.choose(
                (), held=permissions_for_user(session, company_id, actor.id)
            ),
            used=[],
            withheld=0,
            message_id=message_id,
        )

    # One read, used for both the pill filter and the prompt's permission list.
    # It happens on a blocked turn too, which costs a query and buys one answer
    # to "what may this person do" instead of two that can disagree.
    held = permissions_for_user(session, company_id, actor.id)

    if screened.blocked:
        _audit_refusal(
            session,
            company_id=company_id,
            actor=actor,
            screened=screened,
            message_id=message_id,
        )
        return Turn(
            reply=screened.reply,
            pills=pill_module.choose(pill_module.from_screen(screened.pills), held=held),
            used=[],
            withheld=0,
            message_id=message_id,
        )

    if transport is None:
        return _announced(
            FALLBACK_NO_API_KEY, ANNOUNCEMENT_NO_API_KEY, held=held, message_id=message_id
        )

    # The declarations and the dispatcher resolve together, because a turn that
    # can name tools it cannot run is worse than one with neither.
    if toolset is None or dispatch is None:
        try:
            toolset = default_toolset() if toolset is None else toolset
            dispatch = default_dispatcher() if dispatch is None else dispatch
        except ToolsetUnavailable:
            return _announced(
                FALLBACK_NO_TOOLSET,
                ANNOUNCEMENT_NO_TOOLSET,
                held=held,
                message_id=message_id,
            )

    offered = [
        spec
        for spec in toolset.values()
        if not spec.permission or spec.permission in held
    ]
    wire_tools = [spec.as_wire() for spec in offered]

    where = f"{surface} (project {project_id})" if project_id else surface
    system = persona.system_prompt(
        actor=actor.display_name,
        company_name=company_name,
        permissions=sorted(held),
        surface=where,
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": message}]
    consulted: list[Consulted] = []
    used: list[dict[str, Any]] = []
    fallback = ""
    uncounted = False
    text = ""
    steps = 0

    while True:
        try:
            reply_turn = transport.converse(
                system=system, messages=messages, tools=wire_tools
            )
        except Exception as exc:  # noqa: BLE001 - the surface must name any failure
            return _announced(
                FALLBACK_TRANSPORT_FAILED,
                ANNOUNCEMENT_TRANSPORT_FAILED.format(error=exc),
                held=held,
                message_id=message_id,
                consulted=consulted,
                used=used,
            )

        if reply_turn.stop_reason == "refusal":
            return _announced(
                FALLBACK_MODEL_DECLINED,
                ANNOUNCEMENT_MODEL_DECLINED,
                held=held,
                message_id=message_id,
                consulted=consulted,
                used=used,
            )

        text = (reply_turn.text or "").strip()
        if not reply_turn.tool_calls:
            break

        if steps >= max_steps:
            # The cap is reached with tool calls still pending. Answering with
            # what is in hand and calling it finished is the failure this branch
            # exists to stop, so the reply says the answer is partial.
            fallback = fallback or FALLBACK_STEP_CAP
            text = (
                f"{text}\n\n{ANNOUNCEMENT_STEP_CAP.format(steps=steps)}"
                if text
                else ANNOUNCEMENT_STEP_CAP.format(steps=steps)
            )
            break

        steps += 1
        results: list[dict[str, Any]] = []
        for call in reply_turn.tool_calls:
            try:
                # The model's arguments go through UNTOUCHED, including any that
                # name company_id or actor. The dispatcher is the one place that
                # can refuse them, record the attempt and halt the turn; a tidy
                # strip here would make the attack succeed as a normal call and
                # leave no row saying it happened.
                result = dispatch(
                    session,
                    company_id=company_id,
                    actor=actor,
                    tool=call.name,
                    arguments=dict(call.arguments or {}),
                )
                found, withheld = _counts(call.name, result)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                fallback = fallback or FALLBACK_TOOL_FAILED
                results.append(_tool_result_block(call.id, f"{exc}", is_error=True))
                continue

            if withheld is None:
                uncounted = True
            refused = result.get("ok") is False
            # A refused tool still appears in the transcript, with nothing found.
            # A transcript that hides its refusals is a transcript of a different
            # conversation, and the reason is what makes the answer readable.
            used.append({"tool": result.get("tool", call.name), "found": found})
            consulted.append(
                Consulted(
                    tool=result.get("tool", call.name),
                    found=found,
                    withheld=withheld,
                    result=result,
                )
            )
            results.append(_tool_result_block(call.id, result, is_error=refused))

            if result.get("halt_turn"):
                # The tool layer saw an attack signature -- an identity argument,
                # an actor outside this company, a suspended account. The turn
                # stops here: no further tool runs and the model is not asked to
                # write around it.
                halted = str(result.get("reason") or result.get("note") or "")
                return _announced(
                    FALLBACK_TOOL_HALTED,
                    ANNOUNCEMENT_TOOL_HALTED.format(reason=halted),
                    held=held,
                    message_id=message_id,
                    consulted=consulted,
                    used=used,
                )

        messages.append({"role": "assistant", "content": _assistant_content(reply_turn)})
        messages.append({"role": "user", "content": results})

    withheld_total = sum(item.withheld or 0 for item in consulted)

    if not text:
        fallback = fallback or FALLBACK_UNREADABLE_RESPONSE
        text = ANNOUNCEMENT_UNREADABLE_RESPONSE

    # Coverage on every synthesis (ADR-22), applied to chat exactly as to a
    # deliverable. The model is told to name what it left out; this is what
    # happens when it does not, because a summary that quietly drops the
    # withheld half is worse than no summary and cannot be left to good manners.
    if withheld_total > 0 and not _mentions_withholding(text, withheld_total):
        note = WITHHELD_NOTE.format(
            count=withheld_total, plural=" was" if withheld_total == 1 else "s were"
        )
        text = f"{text}\n\n{note}"

    if uncounted:
        fallback = fallback or FALLBACK_UNCOUNTED_WITHHELD
        text = f"{text}\n\n{ANNOUNCEMENT_UNCOUNTED_WITHHELD}"

    return Turn(
        reply=text,
        pills=pill_module.choose(pill_module.from_results(consulted), held=held),
        used=used,
        withheld=withheld_total,
        message_id=message_id,
        fallback=fallback,
    )


def _assistant_content(turn: ModelTurn) -> Any:
    """What to replay as the assistant's turn on the next round.

    The provider's own blocks go back UNCHANGED where we have them. That is not
    tidiness: this model family requires its thinking blocks to be replayed
    exactly as received, and the way to guarantee that is never to rebuild them.
    The reconstruction below is only for a transport that returns no blocks of
    its own, and it invents nothing -- it can only reach here holding at least
    one tool call, because a turn with no tool calls has already broken out of
    the loop.
    """
    if turn.content:
        return turn.content
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.arguments)}
        for call in turn.tool_calls
    )
    return blocks


def _mentions_withholding(text: str, count: int) -> bool:
    """Did the model already say it, and say how many?

    Both halves are required. "some claims were withheld" is the hedge the
    persona forbids, and a bare number without the word is not a disclosure.
    """
    lowered = text.lower()
    return "withheld" in lowered and str(count) in lowered


def _announced(
    fallback: str,
    announcement: str,
    *,
    held: frozenset[str],
    message_id: str,
    consulted: Sequence[Consulted] = (),
    used: Sequence[dict[str, Any]] | None = None,
) -> Turn:
    """A turn that is only an announcement. Never an answer wearing one.

    Any tools that did run before the failure are still reported, because the
    transcript's job is to say what was consulted -- including on the turn that
    then fell over.
    """
    return Turn(
        reply=announcement,
        pills=pill_module.choose(pill_module.from_results(consulted), held=held),
        used=list(used or []),
        withheld=sum(item.withheld or 0 for item in consulted),
        message_id=message_id,
        fallback=fallback,
    )


__all__ = [
    "ANNOUNCEMENT_MODEL_DECLINED",
    "ANNOUNCEMENT_NO_API_KEY",
    "ANNOUNCEMENT_NO_TOOLSET",
    "ANNOUNCEMENT_STEP_CAP",
    "ANNOUNCEMENT_TOOL_HALTED",
    "ANNOUNCEMENT_TRANSPORT_FAILED",
    "ANNOUNCEMENT_UNCOUNTED_WITHHELD",
    "ANNOUNCEMENT_UNREADABLE_RESPONSE",
    "EFFORT",
    "FALLBACK_MODEL_DECLINED",
    "FALLBACK_NO_API_KEY",
    "FALLBACK_NO_TOOLSET",
    "FALLBACK_STEP_CAP",
    "FALLBACK_TOOL_FAILED",
    "FALLBACK_TOOL_HALTED",
    "FALLBACK_TRANSPORT_FAILED",
    "FALLBACK_UNCOUNTED_WITHHELD",
    "FALLBACK_UNREADABLE_RESPONSE",
    "MAX_OUTPUT_TOKENS",
    "MAX_TOOL_STEPS",
    "MODEL_ID",
    "AnthropicTransport",
    "ModelTurn",
    "ToolCall",
    "ToolSpec",
    "ToolsetUnavailable",
    "Dispatcher",
    "default_dispatcher",
    "Transport",
    "Turn",
    "default_toolset",
    "run_turn",
    "transport_from_environment",
]
