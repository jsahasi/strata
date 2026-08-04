"""Ask Claude what changed between two versions. Let the verifier decide what may be said.

THE HONEST LIMIT, FIRST. This path has **never** run against the real Anthropic
API in this repository. Nobody here has held an API key, so every test drives a
deterministic fake through an injected transport.

What is proven offline is the gate: the prompt is built by dispatching on the
version's stored status, every proposal is bound to a citation, every citation
is re-read against the stored source, and a proposal that fails is withheld or
dropped rather than shown. The request shape is checked one step further than
prose allows -- tests/test_propose.py reads the installed SDK's own signature
and typed parameters and asserts that every argument name and nested key below
exists in them. That catches a keyword invented from memory.

What is NOT proven is anything only the endpoint can answer: that
`claude-opus-5` accepts this combination of parameters, that the model returns
the shape asked for, that the response parses on the first try, or how any of it
behaves under a rate limit. The first real call will find bugs in
AnthropicTransport, and it will not find them in the verifier. Treat the client
below as unexercised code and the gate above it as tested code, because that is
what they are.

WHY A PROPOSER IS SAFE TO ADD AT ALL. A model is good at reading two documents
and saying what moved, and bad at remembering exactly where it read it. Offsets
it produces are guesses. So nothing here trusts them: every proposal names a
(version_id, char_start, char_end, quoted_text), and app/verification/verifier.py
re-reads the stored source at those offsets and refuses the claim unless the
words match after normalization -- and, where the quote appears more than once,
unless the proposal also named the occurrence it relies on. There is no
confidence score, no hedge, no greyed-out rendering. ADR-003: a claim that
cannot show its source does not get made. That is the product, and this module
is a caller of it, not a second path around it.

NOTHING IS REPAIRED. A proposal whose offsets sit two characters off the quote
is withheld, not nudged into place. Snapping an offset to the nearest matching
span would make the gate a formality: every citation would be adjusted until it
passed, and the verifier would be checking the repair rather than the model.
Malformed proposals are dropped with the reason recorded, one by one, so a
single bad entry does not discard the good ones beside it.

THE MODEL IS NEVER ASKED WHETHER A VERSION IS DRAFT OR FINAL. ADR-005. The
status is read from the version's explicit field and dispatched on here, using
app/interpretation/action.py, and the permitted actions are handed to the model
as a closed list. Acting on a draft wastes money on something that may not
survive comment; treating a final order as a draft misses a binding deadline.
That decision belongs in a line of Python a reviewer can read, not in a
sentence a model produces. An unknown status raises before the call is made.

OFFLINE BY DEFAULT. The transport is an injected dependency and the SDK is
imported inside the factory, not at module scope, so importing this module costs
nothing and reaches nothing. `make test` passes with no key and no network, and
tests/test_propose.py measures both rather than asserting them in prose.

WITH NO KEY THE PATH IS OFF AND SAYS SO. transport_from_environment() returns
None, propose_changes() proposes nothing, and the run carries FALLBACK_NO_API_KEY
with an announcement for the reader. It does not quietly fall back to the seeded
corpus and present it as model output -- best-practices.html section 26, and the
one degradation that would make the whole submission dishonest.

WHAT THIS MODULE DOES NOT DO. It does not write. Nothing here creates a Claim
row, and nothing here appends to the audit chain: there is no ACTION_ constant
in app/state/audit.py for "a model proposed this", and inventing a second
spelling of one is how the two that already drifted got that way. A caller that
wants to persist a proposal needs both -- a new ACTION_ constant and a writer
that records who proposed and who accepted -- and that is a change to files this
module does not own.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from app.interpretation.action import action_vocabulary, requires_effective_date
from app.text.normalize import normalize
from app.verification.verifier import Citation, verify_citation

# The model. A fixed id with no date suffix, matching the alias the client docs
# publish. Pinned here rather than read from the environment: which model wrote
# a claim is part of the record, and a deployment that silently swapped it would
# make every earlier evaluation unreproducible.
MODEL_ID = "claude-opus-5"

# Output ceiling for one proposal round. Thinking and response text share this
# budget. Well under the point where a non-streaming request risks an HTTP
# timeout, which is why this stays a plain create() call.
MAX_OUTPUT_TOKENS = 16000

# Named fallbacks. Each one is a state the product must be able to say out loud.
FALLBACK_NO_API_KEY = "MODEL_PATH_OFF_NO_API_KEY"
FALLBACK_UNREADABLE_RESPONSE = "MODEL_PATH_OFF_UNREADABLE_RESPONSE"
FALLBACK_TRANSPORT_FAILED = "MODEL_PATH_OFF_TRANSPORT_FAILED"

ANNOUNCEMENT_NO_API_KEY = (
    "The model path is off: no ANTHROPIC_API_KEY is set, so no claims were "
    "proposed. Anything on screen came from the loaded corpus, not from a model."
)
ANNOUNCEMENT_UNREADABLE_RESPONSE = (
    "The model path is on but its answer could not be read as proposals, so "
    "none were made. Nothing was guessed from it."
)
ANNOUNCEMENT_TRANSPORT_FAILED = (
    "The model path is on but the call failed, so no claims were proposed. "
    "The error was: {error}"
)

# Why a proposal never reached the verifier. Each one is a refusal to invent.
DROP_MALFORMED = "proposal is missing a field a citation needs, or its type is wrong"
DROP_OUT_OF_RANGE = "citation offsets fall outside the cited version's text"
DROP_EMPTY_QUOTE = "citation quotes nothing"
DROP_UNASKED_VERSION = "citation names a version the caller did not ask about"
DROP_ACTION_OUT_OF_VOCABULARY = (
    "proposed action is not one this version's status allows"
)


class Transport(Protocol):
    """One turn with a model: text in, text out.

    Deliberately narrow. Everything above it -- the prompt, the parsing, the
    dropping, the verification -- is pure and runs in CI, and everything the
    network touches is on the far side of this one method. That is what makes
    the fake in tests/test_propose.py a fair stand-in rather than a mock of
    something more interesting.
    """

    def complete(self, *, system: str, user: str) -> str: ...


class SourceVersion(Protocol):
    """What the proposer needs of a version. DocumentVersion satisfies it."""

    id: str
    status: str
    source_text: str


@dataclass(frozen=True, slots=True)
class ProposedClaim:
    """A model's claim whose citation was just re-read and matched.

    actual_text is what the source really says at the cited offsets, carried so
    a reviewer sees the source rather than the model's own quote echoed back.
    """

    statement: str
    action: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    cited_occurrence: int | None
    actual_text: str


@dataclass(frozen=True, slots=True)
class WithheldProposal:
    """A proposal the product declines to make. It cannot carry what it said.

    No statement field, and slots=True so one cannot be attached at runtime
    either -- the same design as WithheldClaim in app/state/claims.py, for the
    same reason: a template cannot render what the object does not have. A
    greyed-out assertion is still an assertion.

    `reason` is the verifier's own reason string, imported rather than restated,
    so the two cannot drift into describing different failures with the same
    words. Mapping it to the REASON_CODE_* vocabulary is the persistence layer's
    job and happens in app/state/claims.py, where the rows are read.
    """

    reason: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    cited_occurrence: int | None
    source_excerpt: str


@dataclass(frozen=True, slots=True)
class DroppedProposal:
    """A proposal that never reached the verifier, and why.

    Also carries no statement. A proposal too malformed to check its citation is
    further from assertable than one that merely failed, not closer.
    """

    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProposalRun:
    """One round: what may be shown, what may not, and what was thrown away.

    `fallback` is None on a healthy run. When it is set, `announcement` holds
    the sentence the product must show the reader, and the three lists are
    empty -- a degraded run proposes nothing rather than proposing less.
    """

    proposed: tuple[ProposedClaim, ...]
    withheld: tuple[WithheldProposal, ...]
    dropped: tuple[DroppedProposal, ...]
    action_vocabulary: tuple[str, ...]
    fallback: str | None = None
    announcement: str | None = None


class _Unreadable(RuntimeError):
    """The model's answer was not a list of proposals."""


# ------------------------------------------------------------- the transport --


def transport_from_environment(
    env: Mapping[str, str] | None = None,
) -> Transport | None:
    """A real transport when a key is configured, None when it is not.

    ONLY ANTHROPIC_API_KEY COUNTS, and that is narrower than the SDK's own
    credential search, which also reads a stored login profile. The narrowing is
    deliberate: the product has to answer "is the model path on" while rendering
    a page, without a network call and without constructing a client that might
    succeed later and fail now. A reviewer with a profile but no exported key
    sees the announced fallback, which is a true statement about this process.
    """
    values = os.environ if env is None else env
    if not values.get("ANTHROPIC_API_KEY"):
        return None
    return AnthropicTransport(_client(values["ANTHROPIC_API_KEY"]))


def _client(api_key: str):
    """Build the SDK client. Imported here so module import stays free.

    A top-level import would put the SDK, httpx and anyio into the import graph
    of anything that touches interpretation, including the eval harness, which
    asserts in a fresh process that no model client is loaded on its path.
    """
    import anthropic  # noqa: PLC0415 -- see docstring

    return anthropic.Anthropic(api_key=api_key)


class AnthropicTransport:
    """One Messages call. UNEXERCISED: see the module docstring.

    Three parameter choices, each of which a reader will otherwise reconstruct
    from memory and get wrong:

    THINKING IS ADAPTIVE, WITH NO BUDGET. `budget_tokens` is rejected with a 400
    on this model family; the model decides its own depth and `effort` tunes it.
    Effort is left at its default of high rather than passed, because passing a
    value equal to the default is one more untested parameter on a path that has
    never run.

    NO temperature, top_p OR top_k. They are rejected outright on this family.
    Determinism, if it is wanted, comes from the prompt and from the fact that
    the verifier re-reads the source either way.

    STRUCTURED OUTPUT, NOT A PLEA FOR JSON. output_config.format constrains the
    response to the schema below, which is what makes the parser's job small.
    The parser is still defensive, because this parameter is part of the
    untested half.
    """

    def __init__(self, client: Any, model: str = MODEL_ID):
        self._client = client
        self._model = model

    def complete(self, *, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
        # Checked before content is read. A declined request returns a normal
        # 200 with an empty content list, so indexing into it would raise an
        # IndexError describing nothing.
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("the model declined this request")
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "action": {"type": "string"},
                    "version_id": {"type": "string"},
                    "char_start": {"type": "integer"},
                    "char_end": {"type": "integer"},
                    "quoted_text": {"type": "string"},
                    "occurrence": {"type": ["integer", "null"]},
                },
                "required": [
                    "statement",
                    "action",
                    "version_id",
                    "char_start",
                    "char_end",
                    "quoted_text",
                    "occurrence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- the prompt --

_SYSTEM = """\
You compare two versions of a regulatory proceeding for a regulated utility and \
report what changed. You are one half of a pair. The other half re-reads the \
source at every offset you give and silently discards any claim whose quoted \
text does not match, so an invented citation costs you the claim and gains \
nothing.

Rules, in order of importance.

1. Every claim carries a citation into ONE of the two versions given below: the \
version id, the character offsets of the exact span, and the text at that span \
copied character for character. Offsets are 0-based, char_end is exclusive, and \
they index the text exactly as given -- do not count from a heading or a page.
2. If the words you quote appear more than once in that version, set occurrence \
to the 0-based index of the one you rely on. Otherwise set occurrence to null. \
The same sentence in two sections is two different obligations.
3. Claim only what the cited span shows on its own. A claim that needs a second \
sentence to stand up needs a second claim.
4. Returning an empty list is a correct and expected answer. Report nothing \
rather than reporting something you cannot cite.
5. {status_line}
"""

_ACTION_LINE = (
    "The action on every claim must be exactly one of: {actions}. That list is "
    "fixed by the record and is not yours to extend."
)

_EFFECTIVE_DATE_LINE = (
    " This order binds from a date. Where a claim concerns a deadline, cite the "
    "text that states it rather than computing one."
)

_USER = """\
FROM VERSION
STATUS: {from_status}
ID: {from_id}
---
{from_text}
---

TO VERSION
STATUS: {to_status}
ID: {to_id}
---
{to_text}
---

Report what changed from the FROM version to the TO version, as a JSON object \
with a single key "claims" holding a list. Each item has: statement, action, \
version_id, char_start, char_end, quoted_text, occurrence. Return an empty list \
if nothing in these two texts supports a claim you can cite.
"""


def _prompt(
    from_version: SourceVersion, to_version: SourceVersion
) -> tuple[str, str, tuple[str, ...]]:
    """Build the prompt by dispatching on the stored status. ADR-005.

    Both statuses are put through action_vocabulary, which raises on a word it
    does not know. That happens before the transport is touched, so an unknown
    status costs a refusal rather than a call and a plausible answer.
    """
    action_vocabulary(from_version.status)
    vocabulary = action_vocabulary(to_version.status)

    status_line = _ACTION_LINE.format(actions=", ".join(vocabulary))
    if requires_effective_date(to_version.status):
        status_line += _EFFECTIVE_DATE_LINE

    system = _SYSTEM.format(status_line=status_line)
    user = _USER.format(
        from_status=from_version.status,
        from_id=from_version.id,
        from_text=from_version.source_text,
        to_status=to_version.status,
        to_id=to_version.id,
        to_text=to_version.source_text,
    )
    return system, user, vocabulary


# --------------------------------------------------------------- the parsing --


def _payload(text: str) -> list[Any]:
    """Read the model's answer as a list of proposals, or refuse to.

    A fenced code block is unwrapped. That is formatting around the answer, not
    the answer: unwrapping it repairs nothing about any claim inside, and every
    proposal still has to survive the same checks. Anything else that will not
    parse, or that parses to something other than a list, raises -- the caller
    announces a fallback rather than salvaging fragments.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()

    try:
        loaded = json.loads(stripped)
    except (ValueError, TypeError) as error:
        raise _Unreadable(str(error)) from error

    if isinstance(loaded, dict):
        loaded = loaded.get("claims")
    if not isinstance(loaded, list):
        raise _Unreadable("the answer was not a list of proposals")
    return loaded


def _is_int(value: Any) -> bool:
    """A JSON integer. True is an int in Python and is not an offset."""
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------ the gate --


def propose_changes(
    transport: Transport | None,
    from_version: SourceVersion,
    to_version: SourceVersion,
) -> ProposalRun:
    """Ask for claims, then let the verifier decide which of them may be shown.

    Returns a ProposalRun. `proposed` are the claims whose citations were
    re-read and matched just now; `withheld` are the ones that failed the check,
    carrying the reason and never the statement; `dropped` are the ones too
    malformed to check.

    NOTHING IS WRITTEN. The verdict is computed here and not stored, for the
    reason app/state/claims.py gives: a stored verdict is a promise about bytes
    that may have changed since.
    """
    system, user, vocabulary = _prompt(from_version, to_version)

    if transport is None:
        return ProposalRun(
            (), (), (), vocabulary, FALLBACK_NO_API_KEY, ANNOUNCEMENT_NO_API_KEY
        )

    try:
        answer = transport.complete(system=system, user=user)
    except Exception as error:  # noqa: BLE001 -- see below
        # Broad on purpose. The transport is injected, so its failures are not
        # a closed set this module can name: a timeout, a 429, a refusal, a
        # provider outage. Every one of them means the same thing to a reader --
        # the model path did not answer -- and the alternative is a stack trace
        # where a sentence belongs. The error text is carried, not swallowed.
        return ProposalRun(
            (),
            (),
            (),
            vocabulary,
            FALLBACK_TRANSPORT_FAILED,
            ANNOUNCEMENT_TRANSPORT_FAILED.format(error=error),
        )

    try:
        payload = _payload(answer)
    except _Unreadable:
        return ProposalRun(
            (),
            (),
            (),
            vocabulary,
            FALLBACK_UNREADABLE_RESPONSE,
            ANNOUNCEMENT_UNREADABLE_RESPONSE,
        )

    sources = {
        from_version.id: from_version.source_text,
        to_version.id: to_version.source_text,
    }
    return _judge(payload, sources, vocabulary)


def _judge(
    payload: Sequence[Any],
    sources: Mapping[str, str],
    vocabulary: tuple[str, ...],
) -> ProposalRun:
    """Structural checks, then the verifier. One proposal at a time.

    Per proposal rather than per batch, so one malformed entry drops itself and
    leaves the rest standing. A batch-level refusal would let a single stray
    field discard claims that were perfectly citable.
    """
    proposed: list[ProposedClaim] = []
    withheld: list[WithheldProposal] = []
    dropped: list[DroppedProposal] = []

    for item in payload:
        checked = _structure(item, sources, vocabulary)
        if isinstance(checked, DroppedProposal):
            dropped.append(checked)
            continue

        statement, action, citation = checked
        result = verify_citation(
            citation,
            sources[citation.version_id],
            expected_occurrence=_occurrence(item),
        )

        if not result.verified:
            withheld.append(
                WithheldProposal(
                    reason=result.reason or "",
                    citation_version_id=citation.version_id,
                    citation_start=citation.char_start,
                    citation_end=citation.char_end,
                    citation_quote=citation.quoted_text,
                    cited_occurrence=_occurrence(item),
                    source_excerpt=result.actual_text or "",
                )
            )
            continue

        proposed.append(
            ProposedClaim(
                statement=statement,
                action=action,
                citation_version_id=citation.version_id,
                citation_start=citation.char_start,
                citation_end=citation.char_end,
                citation_quote=citation.quoted_text,
                cited_occurrence=_occurrence(item),
                actual_text=result.actual_text or "",
            )
        )

    return ProposalRun(tuple(proposed), tuple(withheld), tuple(dropped), vocabulary)


def _occurrence(item: Any) -> int | None:
    value = item.get("occurrence") if isinstance(item, dict) else None
    return value if _is_int(value) else None


def _structure(
    item: Any,
    sources: Mapping[str, str],
    vocabulary: tuple[str, ...],
) -> tuple[str, str, Citation] | DroppedProposal:
    """Everything that can be settled without reading the source.

    Order matters only in what it reports, not in what it admits: a proposal
    failing two of these is dropped either way, and the first reason is the one
    an analyst can act on.
    """
    if not isinstance(item, dict):
        return DroppedProposal(DROP_MALFORMED, f"expected an object, got {type(item)}")

    statement = item.get("statement")
    action = item.get("action")
    version_id = item.get("version_id")
    start = item.get("char_start")
    end = item.get("char_end")
    quote = item.get("quoted_text")

    if not isinstance(statement, str) or not statement.strip():
        return DroppedProposal(DROP_MALFORMED, "no statement")
    if not isinstance(action, str) or not isinstance(version_id, str):
        return DroppedProposal(DROP_MALFORMED, "action or version_id is not a string")
    if not isinstance(quote, str):
        return DroppedProposal(DROP_MALFORMED, "quoted_text is not a string")
    if not _is_int(start) or not _is_int(end):
        return DroppedProposal(DROP_MALFORMED, "offsets are not integers")

    if version_id not in sources:
        return DroppedProposal(
            DROP_UNASKED_VERSION,
            f"cited {version_id!r}, which is neither version in this comparison",
        )

    # Empty by the same definition the verifier uses, imported rather than
    # re-spelt: whitespace that normalizes away is not a quote.
    if not normalize(quote):
        return DroppedProposal(DROP_EMPTY_QUOTE, f"quoted_text was {quote!r}")

    source = sources[version_id]
    if start < 0 or end > len(source) or start > end:
        return DroppedProposal(
            DROP_OUT_OF_RANGE,
            f"({start}, {end}) against a source of {len(source)} characters",
        )

    if action not in vocabulary:
        return DroppedProposal(
            DROP_ACTION_OUT_OF_VOCABULARY,
            f"{action!r} is not one of {', '.join(vocabulary)}",
        )

    return statement, action, Citation(version_id, start, end, quote)


# ----------------------------------------------------------------- tenancy --


def propose_changes_for_company(
    session: Any,
    company_id: str,
    *,
    from_version_id: str,
    to_version_id: str,
    transport: Transport | None,
) -> ProposalRun:
    """The scoped entry point: resolve both versions for this company, then run.

    The versions are read through versions_for_company in app/state/queries.py
    rather than queried here. A second scoped read of the same table is a second
    thing to get wrong, and one of the two would be the one nobody audited.

    A version this company cannot read raises. Absence is denial: the caller
    named two documents, and comparing one of them against a guess is worse than
    refusing. The import is deferred so that everything above this function
    stays free of persistence, as app/verification/verifier.py does for the same
    reason.
    """
    from app.state.queries import _require_scope, versions_for_company

    _require_scope(company_id)

    versions = {
        version.id: version for version in versions_for_company(session, company_id)
    }
    missing = [
        version_id
        for version_id in (from_version_id, to_version_id)
        if version_id not in versions
    ]
    if missing:
        raise ValueError(
            f"no version {missing[0]!r} readable for company {company_id!r}; "
            "refusing to compare against a version this company cannot read"
        )

    return propose_changes(
        transport, versions[from_version_id], versions[to_version_id]
    )


__all__ = [
    "ANNOUNCEMENT_NO_API_KEY",
    "ANNOUNCEMENT_TRANSPORT_FAILED",
    "ANNOUNCEMENT_UNREADABLE_RESPONSE",
    "DROP_ACTION_OUT_OF_VOCABULARY",
    "DROP_EMPTY_QUOTE",
    "DROP_MALFORMED",
    "DROP_OUT_OF_RANGE",
    "DROP_UNASKED_VERSION",
    "FALLBACK_NO_API_KEY",
    "FALLBACK_TRANSPORT_FAILED",
    "FALLBACK_UNREADABLE_RESPONSE",
    "MAX_OUTPUT_TOKENS",
    "MODEL_ID",
    "AnthropicTransport",
    "DroppedProposal",
    "ProposalRun",
    "ProposedClaim",
    "SourceVersion",
    "Transport",
    "WithheldProposal",
    "propose_changes",
    "propose_changes_for_company",
    "transport_from_environment",
]
