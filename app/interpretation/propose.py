"""Ask Claude whether one change matters. Let the verifier decide if it may say so.

This is the one place in the product where a model makes a judgement. It is
shown a single change the deterministic diff has already found -- the text
before, the text after, the section, the offsets -- and asked one question: does
this matter to the utility that has to live with it, and which exact sentence
says so. It is never asked what changed. The diff knows that, exactly, and can
be tested against known answers.

WHAT THIS MODULE USED TO BE, BECAUSE THE DRIFT IS THE LESSON. Until this
rewrite it sent the model the FULL TEXT OF BOTH VERSIONS and asked it to report
what changed, and its output schema carried no materiality field at all. That is
precisely the job ADR-004 reserves for the diff, and the reason ADR-004 reserves
it: a model reading two long documents silently drops changes and there is
nothing in the answer that says which ones. The module was written after the
ADR, was tested, and drifted anyway -- because every test asked what came *out*
of the call and none asked what went *in*. Nothing failed. The gap was found by
reading the ADR against the prompt. tests/test_propose.py now asserts that
neither version's full text reaches the model and that the prompt names the diff
as the thing that found the change, so the same drift fails a test rather than
waiting to be read.

THE HONEST LIMIT, SECOND. This path has **never** run against the real Anthropic
API in this repository. Nobody here has held an API key, so every test drives a
deterministic fake through an injected transport.

What is proven offline is the gate: the prompt is built by dispatching on the
change's stored status, the judgement is bound to a citation, the citation is
re-read against the stored source, and a judgement whose citation fails is
withheld rather than shown. The request shape is checked one step further than
prose allows -- tests/test_propose.py reads the installed SDK's own signature and
typed parameters and asserts that every argument name and nested key below exists
in them. That catches a keyword invented from memory.

What is NOT proven is anything only the endpoint can answer: that
`claude-opus-5` accepts this combination of parameters, that the model returns
the shape asked for, that the response parses on the first try, or how any of it
behaves under a rate limit. The first real call will find bugs in
AnthropicTransport, and it will not find them in the verifier. Treat the client
below as unexercised code and the gate above it as tested code, because that is
what they are.

THE JUDGEMENT PASSES THE GATE EVERY CLAIM PASSES. A model is good at reading a
paragraph and saying what it obliges, and bad at remembering exactly where it
read it. Offsets it produces are guesses. So nothing here trusts them: the
judgement names a (version_id, char_start, char_end, quoted_text), and
app/verification/verifier.py re-reads the stored source at those offsets and
refuses unless the words match after normalization. When it refuses, the verdict
and the reason for it are both withheld and the refusal takes their place --
not lowered in confidence, not shown with a caveat, withheld, exactly as a
misquoted claim is. ADR-003: a claim that cannot show its source does not get
made, and a model's judgement is a claim.

MATERIALITY IS NOT CONFIDENCE. ADR-006 owns the confidence floor and the review
queue below it. This is a different judgement with a different shape: a boolean
with a citation, no score, no threshold, nothing to tune. A number here would
become a second floor that nobody decided the height of, and on screen the two
would read as one thing.

NOTHING IS REPAIRED. A judgement whose offsets sit two characters off the quote
is withheld, not nudged into place. Snapping an offset to the nearest matching
span would make the gate a formality: every citation would be adjusted until it
passed, and the verifier would be checking the repair rather than the model.

THE CITATION HAS NO OCCURRENCE FIELD, AND THAT COSTS US A JUDGEMENT RATHER THAN
BUYING ONE. Where the quoted words appear more than once in the version, the
verifier withholds, because the citation cannot say which of them it rests on --
the same sentence in two sections is two obligations. The tempting fix is to
derive the occurrence from the offsets the model gave and hand that to the
verifier as the expected one. That check would agree with itself every time. So
the prompt asks for a span that is unique, and a judgement that quotes repeated
boilerplate is withheld.

THE MODEL IS NEVER ASKED WHETHER A VERSION IS DRAFT OR FINAL. ADR-005. The
status is read from the change's own field, dispatched on here through
app/interpretation/action.py, and stated to the model as a fact. Acting on a
draft wastes money on something that may not survive comment; treating a final
order as a draft misses a binding deadline. That decision belongs in a line of
Python a reviewer can read, not in a sentence a model produces. An unknown
status raises before the call is made.

OFFLINE BY DEFAULT. The transport is an injected dependency and the SDK is
imported inside the factory, not at module scope, so importing this module costs
nothing and reaches nothing. `make test` passes with no key and no network, and
tests/test_propose.py measures both rather than asserting them in prose.

WITH NO KEY THE PATH IS OFF AND SAYS SO. transport_from_environment() returns
None, judge_materiality() judges nothing, and the run carries FALLBACK_NO_API_KEY
with an announcement for the reader. It does not quietly answer "not material" --
best-practices.html section 26, and the one degradation that would make the whole
submission dishonest, because an unjudged change and a change judged harmless
look identical on screen and mean opposite things.

WHAT THIS MODULE DOES NOT DO. It does not write. Nothing here sets
`Change.materiality`, and nothing here appends to the audit chain. Both are
missing the same thing: app/state/audit.py has no ACTION_ constant for a
materiality judgement. The spelling that fits is `change.materiality_set`, which
tests/test_policy.py already writes as a bare string -- so the vocabulary this
product keeps in one file has a second home already, which is how the two codes
that drifted got that way. A caller that wants to persist a judgement needs
three things this module does not own: that constant, a column for the reason
and the citation beside the existing `materiality` column, and a writer that
records who judged. Until then the judgement is computed at read time and not
stored, which is what app/state/claims.py does with a verified claim and for the
same reason: a stored verdict is a promise about bytes that may have changed
since.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.interpretation.action import requires_effective_date
from app.text.normalize import normalize
from app.verification.verifier import Citation, verify_citation

# The model. A fixed id with no date suffix, matching the alias the client docs
# publish. Pinned here rather than read from the environment: which model judged
# a change is part of the record, and a deployment that silently swapped it would
# make every earlier evaluation unreproducible.
MODEL_ID = "claude-opus-5"

# Output ceiling for one judgement. Thinking and response text share this
# budget. Well under the point where a non-streaming request risks an HTTP
# timeout, which is why this stays a plain create() call.
MAX_OUTPUT_TOKENS = 16000

# Named fallbacks. Each one is a state the product must be able to say out loud.
FALLBACK_NO_API_KEY = "MODEL_PATH_OFF_NO_API_KEY"
FALLBACK_UNREADABLE_RESPONSE = "MODEL_PATH_OFF_UNREADABLE_RESPONSE"
FALLBACK_TRANSPORT_FAILED = "MODEL_PATH_OFF_TRANSPORT_FAILED"

ANNOUNCEMENT_NO_API_KEY = (
    "The model path is off: no ANTHROPIC_API_KEY is set, so nothing has judged "
    "whether this change matters. Read that as work not done, not as a finding "
    "that the change is unimportant."
)
ANNOUNCEMENT_UNREADABLE_RESPONSE = (
    "The model path is on but its answer could not be read as a judgement, so "
    "no judgement was made. Nothing was guessed from it."
)
ANNOUNCEMENT_TRANSPORT_FAILED = (
    "The model path is on but the call failed, so this change is unjudged. "
    "The error was: {error}"
)

# Why a judgement never reached the verifier. Each one is a refusal to invent.
DROP_MALFORMED = "the answer is missing a field a judgement needs, or its type is wrong"
DROP_OUT_OF_RANGE = "citation offsets fall outside the cited version's text"
DROP_EMPTY_QUOTE = "citation quotes nothing"
DROP_UNASKED_VERSION = "citation names a version this change does not span"
DROP_OUTSIDE_THE_CHANGE = (
    "citation falls outside the change the model was shown, so it quotes text "
    "it was never given"
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


class StoredChange(Protocol):
    """What building a review needs of a change row. `Change` satisfies it."""

    id: str
    change_type: str
    section: str | None
    status: str
    from_version_id: str
    to_version_id: str
    before_start: int | None
    before_end: int | None
    after_start: int | None
    after_end: int | None


@dataclass(frozen=True, slots=True)
class Span:
    """One side of a change: where it sits in a version, and what it says there.

    The text is sliced from the stored source rather than carried in from a
    caller, so what the model reads and what the verifier re-reads come from the
    same bytes.
    """

    version_id: str
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True, slots=True)
class ChangeUnderReview:
    """Exactly what the model is shown. One change, never a document.

    A pure addition has no `before` and a pure removal has no `after`, and the
    prompt says which kind of absence it is rather than printing an empty block.
    Both being None is refused: there is no change there to judge.
    """

    change_id: str
    change_type: str
    section: str | None
    status: str
    before: Span | None
    after: Span | None

    @property
    def sides(self) -> tuple[Span, ...]:
        return tuple(side for side in (self.before, self.after) if side is not None)


@dataclass(frozen=True, slots=True)
class MaterialityJudgement:
    """A verdict whose citation was just re-read and matched.

    actual_text is what the source really says at the cited offsets, carried so
    a reviewer sees the source rather than the model's own quote echoed back.

    There is no confidence field and there will not be one. See the module
    docstring: ADR-006 owns that axis, and a second number on this object would
    be read as the same thing.
    """

    material: bool
    why: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    actual_text: str


@dataclass(frozen=True, slots=True)
class WithheldJudgement:
    """A judgement the product declines to show. It cannot carry the verdict.

    No `material` field, no `why`, and slots=True so neither can be attached at
    runtime either -- the same design as WithheldClaim in app/state/claims.py,
    for the same reason: a template cannot render what the object does not have.
    A greyed-out verdict is still a verdict.

    `reason` is the verifier's own reason string, imported rather than restated,
    so the two cannot drift into describing different failures with the same
    words.
    """

    reason: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    source_excerpt: str


@dataclass(frozen=True, slots=True)
class DroppedJudgement:
    """An answer that never reached the verifier, and why.

    Also carries no verdict. An answer too malformed to check its citation is
    further from assertable than one that merely failed, not closer.
    """

    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class MaterialityRun:
    """One change, one answer, and at most one of the three ways it can land.

    `fallback` is None on a healthy run. When it is set, `announcement` holds
    the sentence the product must show the reader and the other three are None --
    a degraded run judges nothing rather than judging vaguely.
    """

    judgement: MaterialityJudgement | None = None
    withheld: WithheldJudgement | None = None
    dropped: DroppedJudgement | None = None
    fallback: str | None = None
    announcement: str | None = None


class _Unreadable(RuntimeError):
    """The model's answer was not a judgement."""


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
        "material": {"type": "boolean"},
        "why": {"type": "string"},
        "citation": {
            "type": "object",
            "properties": {
                "version_id": {"type": "string"},
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
                "quoted_text": {"type": "string"},
            },
            "required": ["version_id", "char_start", "char_end", "quoted_text"],
            "additionalProperties": False,
        },
    },
    "required": ["material", "why", "citation"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- the prompt --

_SYSTEM = """\
You judge whether one change to a regulatory proceeding matters to the utility \
that has to live with it. You are one half of a pair. The other half re-reads \
the source at the offsets you give and withholds your judgement outright when \
the quoted text is not there, so an invented citation costs you the judgement \
and gains nothing.

You are not asked to find the change or to say what moved. A deterministic diff \
found it and its exact offsets, and they are given below as fact.

Rules, in order of importance.

1. Answer with one object: material, true or false; why, in one sentence; and a \
citation.
2. The citation names one of the versions below, the character offsets of the \
exact span, and the text at that span copied character for character. Offsets \
are 0-based, char_end is exclusive, and they index that version's whole text -- \
each side below carries the offsets it sits at, so count from those.
3. Cite inside the change. The span you quote must lie within the offsets given \
for one of the sides below. Text outside them is a document you have not been \
shown.
4. Quote a span whose words appear only once in that version. Where the sentence \
you would quote is repeated elsewhere in the filing, take the shortest span \
around it that is unique -- a quote the gate cannot place is withheld.
5. false is a correct and expected answer. A change that does not matter is a \
finding, not a failure.
6. {status_line}
"""

#: Stated to the model as a fact about the record, never asked. ADR-005, and the
#: two lines never share a code path for the same reason the actions do not.
STATUS_LINE_FINAL = (
    "This change lands in a FINAL version. It binds, so judge it by what it "
    "obliges the utility to do and by when. Where the change moves a date, cite "
    "the text that states the date rather than computing one."
)
STATUS_LINE_DRAFT = (
    "This change lands in a DRAFT version. It is proposed and may not survive "
    "comment, so judge whether it is worth watching and commenting on, not "
    "whether it binds."
)

_USER = """\
CHANGE {change_id}
TYPE: {change_type}
SECTION: {section}
STATUS: {status}

{before_block}

{after_block}

Judge whether this change is material for the utility subject to this \
proceeding. Answer with a JSON object holding material, why, and a citation \
carrying version_id, char_start, char_end and quoted_text.
"""

_SIDE = """\
{term}
VERSION: {version_id}
OFFSETS: {start}-{end}
---
{text}
---"""

_ABSENT = {
    "BEFORE": (
        "BEFORE\nNothing. This passage is new, and no earlier version carries "
        "text corresponding to it."
    ),
    "AFTER": (
        "AFTER\nNothing. This passage is gone, and the version now in force "
        "carries no text corresponding to it."
    ),
}

UNLABELLED_SECTION = "unlabelled"


def _side_block(term: str, side: Span | None) -> str:
    if side is None:
        return _ABSENT[term]
    return _SIDE.format(
        term=term,
        version_id=side.version_id,
        start=side.char_start,
        end=side.char_end,
        text=side.text,
    )


def _prompt(change: ChangeUnderReview) -> tuple[str, str]:
    """Build the prompt by dispatching on the stored status. ADR-005.

    The status goes through requires_effective_date, which raises on a word it
    does not know. That happens before the transport is touched, so an unknown
    status costs a refusal rather than a call and a plausible answer.
    """
    binding = requires_effective_date(change.status)
    system = _SYSTEM.format(
        status_line=STATUS_LINE_FINAL if binding else STATUS_LINE_DRAFT
    )
    user = _USER.format(
        change_id=change.change_id,
        change_type=change.change_type,
        section=change.section or UNLABELLED_SECTION,
        status=change.status,
        before_block=_side_block("BEFORE", change.before),
        after_block=_side_block("AFTER", change.after),
    )
    return system, user


# --------------------------------------------------------------- the parsing --


def _payload(text: str) -> dict:
    """Read the model's answer as one judgement, or refuse to.

    A fenced code block is unwrapped. That is formatting around the answer, not
    the answer: unwrapping it repairs nothing about the judgement inside, which
    still has to survive every check below. Anything else that will not parse,
    or that parses to something other than an object, raises -- one change was
    sent, so a list is not a shape this can read, and taking the first item of
    one would be picking an answer out of several.
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

    if not isinstance(loaded, dict):
        raise _Unreadable("the answer was not a single judgement")
    return loaded


def _is_int(value: Any) -> bool:
    """A JSON integer. True is an int in Python and is not an offset."""
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------ the gate --


def judge_materiality(
    transport: Transport | None,
    change: ChangeUnderReview,
    sources: Mapping[str, str],
) -> MaterialityRun:
    """Ask whether this change matters, then let the verifier decide if we say so.

    TWO ARGUMENTS, TWO AUDIENCES, AND THE SPLIT IS THE POINT. `change` is what
    the model is shown -- one change, its two sides, its offsets. `sources` is
    what the gate reads: the whole text of the versions this change spans, which
    the model never sees. So the model can only cite what it was handed, and the
    check happens against the document rather than against the extract.

    NOTHING IS WRITTEN. The verdict is computed here and not stored, for the
    reason app/state/claims.py gives: a stored verdict is a promise about bytes
    that may have changed since.
    """
    _refuse_what_cannot_be_judged(change, sources)
    system, user = _prompt(change)

    if transport is None:
        return MaterialityRun(
            fallback=FALLBACK_NO_API_KEY, announcement=ANNOUNCEMENT_NO_API_KEY
        )

    try:
        answer = transport.complete(system=system, user=user)
    except Exception as error:  # noqa: BLE001 -- see below
        # Broad on purpose. The transport is injected, so its failures are not
        # a closed set this module can name: a timeout, a 429, a refusal, a
        # provider outage. Every one of them means the same thing to a reader --
        # the model path did not answer -- and the alternative is a stack trace
        # where a sentence belongs. The error text is carried, not swallowed.
        return MaterialityRun(
            fallback=FALLBACK_TRANSPORT_FAILED,
            announcement=ANNOUNCEMENT_TRANSPORT_FAILED.format(error=error),
        )

    try:
        payload = _payload(answer)
    except _Unreadable:
        return MaterialityRun(
            fallback=FALLBACK_UNREADABLE_RESPONSE,
            announcement=ANNOUNCEMENT_UNREADABLE_RESPONSE,
        )

    return _judge(payload, change, sources)


def _refuse_what_cannot_be_judged(
    change: ChangeUnderReview, sources: Mapping[str, str]
) -> None:
    """Refuse before the call, never after it. Absence is denial.

    A change with no side at all is not a change, and a change whose source text
    is not in hand cannot have its citation re-read -- so the judgement would be
    unverifiable by construction, which is the one thing this module exists to
    prevent. Both raise rather than returning a fallback: a fallback describes a
    degradation the product can announce, and these two are a caller's mistake.
    """
    if not change.sides:
        raise ValueError(
            f"change {change.change_id!r} has neither a before nor an after side; "
            "there is nothing here to judge"
        )
    for side in change.sides:
        if side.version_id not in sources:
            raise ValueError(
                f"no source text for version {side.version_id!r}; refusing to "
                "judge a change whose citation could not be re-read"
            )


def _judge(
    payload: dict,
    change: ChangeUnderReview,
    sources: Mapping[str, str],
) -> MaterialityRun:
    """Structural checks, then the verifier. The same gate a claim passes."""
    checked = _structure(payload, change, sources)
    if isinstance(checked, DroppedJudgement):
        return MaterialityRun(dropped=checked)

    material, why, citation = checked

    # expected_occurrence is deliberately not passed. See the module docstring:
    # the only value available here would be derived from the offsets the model
    # gave, so the check would be comparing the model with itself.
    result = verify_citation(citation, sources[citation.version_id])

    if not result.verified:
        return MaterialityRun(
            withheld=WithheldJudgement(
                reason=result.reason or "",
                citation_version_id=citation.version_id,
                citation_start=citation.char_start,
                citation_end=citation.char_end,
                citation_quote=citation.quoted_text,
                source_excerpt=result.actual_text or "",
            )
        )

    return MaterialityRun(
        judgement=MaterialityJudgement(
            material=material,
            why=why,
            citation_version_id=citation.version_id,
            citation_start=citation.char_start,
            citation_end=citation.char_end,
            citation_quote=citation.quoted_text,
            actual_text=result.actual_text or "",
        )
    )


def _structure(
    payload: dict,
    change: ChangeUnderReview,
    sources: Mapping[str, str],
) -> tuple[bool, str, Citation] | DroppedJudgement:
    """Everything that can be settled without reading the source.

    Order matters only in what it reports, not in what it admits: an answer
    failing two of these is dropped either way, and the first reason is the one
    an analyst can act on.
    """
    material = payload.get("material")
    why = payload.get("why")
    citation = payload.get("citation")

    # bool first and on its own. `material: 1` is an int in JSON and would pass
    # a truthiness test while meaning nothing a person could defend.
    if not isinstance(material, bool):
        return DroppedJudgement(
            DROP_MALFORMED, f"material was {material!r}, which is not true or false"
        )
    if not isinstance(why, str) or not why.strip():
        return DroppedJudgement(DROP_MALFORMED, "no reason given")
    if not isinstance(citation, dict):
        return DroppedJudgement(DROP_MALFORMED, "no citation")

    version_id = citation.get("version_id")
    start = citation.get("char_start")
    end = citation.get("char_end")
    quote = citation.get("quoted_text")

    if not isinstance(version_id, str) or not isinstance(quote, str):
        return DroppedJudgement(
            DROP_MALFORMED, "version_id or quoted_text is not a string"
        )
    if not _is_int(start) or not _is_int(end):
        return DroppedJudgement(DROP_MALFORMED, "offsets are not integers")

    if version_id not in sources:
        return DroppedJudgement(
            DROP_UNASKED_VERSION,
            f"cited {version_id!r}, which is not a version this change spans",
        )

    # Empty by the same definition the verifier uses, imported rather than
    # re-spelt: whitespace that normalizes away is not a quote.
    if not normalize(quote):
        return DroppedJudgement(DROP_EMPTY_QUOTE, f"quoted_text was {quote!r}")

    source = sources[version_id]
    if start < 0 or end > len(source) or start > end:
        return DroppedJudgement(
            DROP_OUT_OF_RANGE,
            f"({start}, {end}) against a source of {len(source)} characters",
        )

    # ADR-004 at the far end of the call. The model was shown one change; a
    # citation outside it is a claim about text it was never handed, and it can
    # verify perfectly while still being that. Dropped rather than withheld,
    # because the failure is in what was cited rather than in whether it matched.
    side = next(
        (side for side in change.sides if side.version_id == version_id), None
    )
    if side is None:
        return DroppedJudgement(
            DROP_OUTSIDE_THE_CHANGE,
            f"cited {version_id!r}, which is not a side of this change",
        )
    if start < side.char_start or end > side.char_end:
        return DroppedJudgement(
            DROP_OUTSIDE_THE_CHANGE,
            f"cited ({start}, {end}), outside the change at "
            f"({side.char_start}, {side.char_end})",
        )

    return material, why, Citation(version_id, start, end, quote)


# ------------------------------------------------------------------ building --


def change_under_review(
    change: StoredChange, sources: Mapping[str, str]
) -> ChangeUnderReview:
    """Turn a stored change row into the thing the model is shown.

    The side texts are sliced out of the stored source here rather than accepted
    from a caller. A caller passing the text in could pass text that is not what
    the offsets address, and the model would then be judging one passage while
    the verifier read another.
    """
    return ChangeUnderReview(
        change_id=change.id,
        change_type=change.change_type,
        section=change.section,
        status=change.status,
        before=_span(
            sources,
            change.from_version_id,
            change.before_start,
            change.before_end,
        ),
        after=_span(
            sources,
            change.to_version_id,
            change.after_start,
            change.after_end,
        ),
    )


def _span(
    sources: Mapping[str, str], version_id: str, start: int | None, end: int | None
) -> Span | None:
    """One side, or None where the change has no side there.

    None means a pure addition or a pure removal, which is a fact about the
    change. A version whose text is missing is not that, and raises.
    """
    if start is None or end is None:
        return None
    if version_id not in sources:
        raise ValueError(
            f"no source text for version {version_id!r}; refusing to build a "
            "change whose text cannot be read"
        )
    return Span(version_id, start, end, sources[version_id][start:end])


# ------------------------------------------------------------------ tenancy --


def judge_materiality_for_company(
    session: Any,
    company_id: str,
    change_id: str,
    *,
    transport: Transport | None,
) -> MaterialityRun:
    """The scoped entry point: resolve this company's change, then judge it.

    Both reads go through the tenant chokepoints -- change_for_company in
    app/state/claims.py and versions_for_company in app/state/queries.py --
    rather than being queried here. A second scoped read of the same tables is a
    second thing to get wrong, and one of the two would be the one nobody
    audited.

    THE SOURCES ARE NARROWED TO THE TWO VERSIONS THIS CHANGE SPANS. The company
    owns more, and handing them all over would let a citation into an unrelated
    filing verify and be reported as a drop for the wrong reason. The gate reads
    exactly the documents the change is about.

    A change this company cannot read raises. Absence is denial: the caller named
    a change, and judging a different one is worse than refusing. The imports are
    deferred so that everything above this function stays free of persistence, as
    app/verification/verifier.py does for the same reason.
    """
    from app.state.claims import change_for_company
    from app.state.queries import _require_scope, versions_for_company

    _require_scope(company_id)

    change = change_for_company(session, company_id, change_id)
    if change is None:
        raise ValueError(
            f"no change {change_id!r} readable for company {company_id!r}; "
            "refusing to judge a change this company cannot read"
        )

    spanned = {change.from_version_id, change.to_version_id}
    sources = {
        version.id: version.source_text
        for version in versions_for_company(session, company_id)
        if version.id in spanned
    }

    return judge_materiality(
        transport, change_under_review(change, sources), sources
    )


__all__ = [
    "ANNOUNCEMENT_NO_API_KEY",
    "ANNOUNCEMENT_TRANSPORT_FAILED",
    "ANNOUNCEMENT_UNREADABLE_RESPONSE",
    "DROP_EMPTY_QUOTE",
    "DROP_MALFORMED",
    "DROP_OUTSIDE_THE_CHANGE",
    "DROP_OUT_OF_RANGE",
    "DROP_UNASKED_VERSION",
    "FALLBACK_NO_API_KEY",
    "FALLBACK_TRANSPORT_FAILED",
    "FALLBACK_UNREADABLE_RESPONSE",
    "MAX_OUTPUT_TOKENS",
    "MODEL_ID",
    "STATUS_LINE_DRAFT",
    "STATUS_LINE_FINAL",
    "UNLABELLED_SECTION",
    "AnthropicTransport",
    "ChangeUnderReview",
    "DroppedJudgement",
    "MaterialityJudgement",
    "MaterialityRun",
    "Span",
    "StoredChange",
    "Transport",
    "WithheldJudgement",
    "change_under_review",
    "judge_materiality",
    "judge_materiality_for_company",
    "transport_from_environment",
]
