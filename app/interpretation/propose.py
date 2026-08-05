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

THE VERDICT IS STORED, AND THAT IS NOT THE THING ADR-003 REFUSES. READ THIS
BEFORE YOU DELETE ANYTHING BELOW, BECAUSE THE OPTIMISATION IS ONE LINE AWAY.

Until this change the judgement was computed on every render of the change
screen and thrown away. With a key set, every page load was a model call: it
cost money, it took seconds, and two loads could disagree with each other about
the same change. Nothing recorded who judged, when, or on what evidence.
ADR-78 conceded exactly that as cost one and cost two, and the brief asks for a
living, auditable project state -- which a judgement that vanishes on the next
render is not.

So `materiality_for_company` now writes the verdict beside `Change.materiality`
and appends `change.materiality_set` to the chain, and a later read answers from
the row instead of the model.

THE DISTINCTION THAT MAKES THAT LEGITIMATE, in one sentence: what is stored is
THE MODEL'S OUTPUT together with its citation, and that citation is
RE-VERIFIED ON EVERY READ. Nothing stores the fact that the citation verified.
That fact has no shelf life -- it is a statement about bytes, and bytes move --
so it is recomputed from the stored source every single time the verdict is
shown, exactly as app/state/claims.py recomputes it for a stored claim. A stored
verdict whose quote is no longer at its offsets is WITHHELD on the next render:
no job runs, nothing is rebuilt, the page simply refuses. Only the MODEL CALL is
avoided by the store. The gate is not.

"EVERY READ" IS A CLAIM ABOUT THE WHOLE PRODUCT, AND IT WAS ONCE FALSE HERE.
The sentence above is worth exactly as much as the number of readers that obey
it, so the count belongs in the docstring rather than the reader's trust. When
the verdict first became a stored thing, this paragraph already said "every
read" and the code gated ONE reader -- the change screen, which is also the one
that writes. The project list and the chat tool went on reading the raw column,
and were safe only for as long as it had been permanently NULL. A verdict whose
bytes had moved was refused on one screen and asserted as fact one link away.

So there are TWO gated entry points and no third way in:

  materiality_for_company    judges, writes, audits. The change screen.
  shown_materiality_for_company  shows only. No model, no write, no transport
                             argument to add one. The project list, the chat
                             tool, and anything shipped after this sentence.

Both run `_reread` and both narrow the sources to the two versions the change
spans, so they cannot disagree about the same row.
tests/test_propose.py::test_no_surface_reads_the_materiality_column_raw walks
the syntax tree of app/ and fails on any other module that touches the attribute
at all, which is what keeps "every read" a fact rather than an aspiration.

WHAT WOULD MAKE IT A CACHED VERDICT, so that you can recognise the edit when you
are tempted by it. Skipping verify_citation when a row is present. Trusting
`materiality` because `materiality_judged_at` is set. Storing a "verified" flag
and reading it. Comparing a hash of the version instead of re-reading the quote.
Each of those turns this into the thing ADR-003 exists to refuse -- a claim
asserted from memory -- and every test that does not move the bytes underneath a
stored verdict keeps passing while you do it. That is why
tests/test_propose.py::test_a_stored_verdict_whose_source_moved_is_withheld
edits the cited characters and calls no model at all, and why the docstring you
are reading is itself pinned by a test.

THE ONE THING THAT BUYS ANOTHER CALL is a stale citation. When the stored quote
no longer verifies, the source has moved and the judgement was made about text
that is no longer there, so the model is asked again and a fresh verdict that
passes the gate replaces the stale row. A retry that cannot answer -- no key, a
timeout -- leaves the refusal standing rather than falling back to the stored
word, because "this change's stored verdict no longer verifies" is the more
specific true sentence and the fallback would be the cache arriving by the back
door.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol

from app.interpretation.action import requires_effective_date
from app.text.normalize import normalize
from app.verification.verifier import (
    REASON_VERSION_UNREADABLE,
    Citation,
    verify_citation,
)

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
# The same refusal pointed at the store rather than at the model. A row that
# says it was judged and carries no citation, or carries a word this build does
# not recognise as a verdict, is not a judgement this product may read out --
# whatever put it there. Dropped rather than withheld, for the reason the drops
# above are: the failure is in what was recorded, not in whether it matched.
DROP_STORED_INCOMPLETE = (
    "a stored verdict is missing something a judgement needs, so it cannot be "
    "checked against the source and is not read as one"
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

    model_id AND judged_at ARE PART OF THE VERDICT, not decoration on it. Which
    model said this and when it said it are the first two things anybody asks of
    a machine judgement, and neither is recoverable afterwards. They travel on
    this object so that a fresh judgement and one read back out of the database
    are the same shape -- the screen prints the same sentence either way and
    does not need to know which it was handed.

    actual_text still comes from the source read a moment ago, on both paths.
    That is what stops a stored verdict quoting itself.
    """

    material: bool
    why: str
    citation_version_id: str
    citation_start: int
    citation_end: int
    citation_quote: str
    actual_text: str
    model_id: str
    judged_at: datetime


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
    *,
    judged_at: datetime | None = None,
) -> MaterialityRun:
    """Ask whether this change matters, then let the verifier decide if we say so.

    TWO ARGUMENTS, TWO AUDIENCES, AND THE SPLIT IS THE POINT. `change` is what
    the model is shown -- one change, its two sides, its offsets. `sources` is
    what the gate reads: the whole text of the versions this change spans, which
    the model never sees. So the model can only cite what it was handed, and the
    check happens against the document rather than against the extract.

    NOTHING IS WRITTEN HERE. This function asks and gates; it holds no session
    and touches no table. materiality_for_company below is where a verdict that
    survives the gate is recorded, and keeping the two apart is what lets the
    whole gate run in CI against a dictionary of strings.

    judged_at IS THE MOMENT THIS VERDICT WAS MADE, and it is an argument rather
    than a clock read at the far end because the same moment has to land on the
    row and on the audit event. Two reads of the clock would let the record
    disagree with the log about when the product knew.
    """
    _refuse_what_cannot_be_judged(change, sources)
    moment = judged_at or datetime.now(timezone.utc)
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

    return _judge(payload, change, sources, moment)


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
    judged_at: datetime,
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
            model_id=MODEL_ID,
            judged_at=judged_at,
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


# ------------------------------------------------------ the stored verdict --
#
# Everything above this line asks and gates. Everything below remembers, and
# then refuses to trust what it remembered. Read the module docstring first if
# you are here to make this faster.


@dataclass(frozen=True, slots=True)
class _StoredVerdict:
    """A verdict read back off a row, before anything has been checked about it.

    Deliberately NOT a MaterialityJudgement. That type means "a verdict whose
    citation was just re-read and matched", and a row cannot be that until the
    source has been read: naming this one the same thing is how a reader ends up
    passing it to a template. It carries the citation and no actual_text,
    because the only honest source of actual_text is the read that has not
    happened yet.
    """

    material: bool
    why: str
    citation: Citation
    model_id: str
    judged_at: datetime


def _stored_verdict(change: Any) -> _StoredVerdict | DroppedJudgement | None:
    """What the row says, or a named refusal, or nothing at all.

    THREE ANSWERS, AND THE THIRD IS NOT AN ERROR. None means nothing has ever
    judged this change, which is the state every row in the corpus is in and the
    reason the model is asked at all.

    The moment is the flag. `materiality_judged_at` is written last-and-together
    with the rest, so a row carrying it is a row this product wrote; a row
    carrying a word in `materiality` and nothing else came from a loader, from a
    hand-written UPDATE, or from an older build. That row is refused BY NAME
    rather than read as a verdict, because a bare word in a 32-character column
    cannot show its source and an assertion that cannot show its source does not
    get made, whichever table it came out of. It is not overwritten either --
    somebody put it there, and this module is not the place to decide they were
    wrong.
    """
    from app.state.models import MATERIALITY_MATERIAL, MATERIALITY_VERDICTS

    if change.materiality_judged_at is None:
        return None

    missing = [
        name
        for name in (
            "materiality",
            "materiality_why",
            "materiality_citation_version_id",
            "materiality_citation_start",
            "materiality_citation_end",
            "materiality_citation_quote",
            "materiality_model_id",
        )
        if getattr(change, name) is None
    ]
    if missing:
        return DroppedJudgement(
            DROP_STORED_INCOMPLETE,
            f"change {change.id} says it was judged at {change.materiality_judged_at} "
            f"and carries no {', '.join(missing)}",
        )
    if change.materiality not in MATERIALITY_VERDICTS:
        return DroppedJudgement(
            DROP_STORED_INCOMPLETE,
            f"change {change.id} holds materiality {change.materiality!r}, which "
            f"is not one of {MATERIALITY_VERDICTS}",
        )

    return _StoredVerdict(
        material=change.materiality == MATERIALITY_MATERIAL,
        why=change.materiality_why,
        citation=Citation(
            change.materiality_citation_version_id,
            change.materiality_citation_start,
            change.materiality_citation_end,
            change.materiality_citation_quote,
        ),
        model_id=change.materiality_model_id,
        judged_at=change.materiality_judged_at,
    )


def _reread(stored: _StoredVerdict, sources: Mapping[str, str]) -> MaterialityRun:
    """Run the gate over a stored verdict. THIS IS THE FUNCTION NOT TO SKIP.

    It is the whole reason storing the verdict does not make it a cached one.
    The bytes behind the citation are read out of the database as they are right
    now and the quote is matched against them, with the same call and the same
    refusals a model's fresh answer meets one screen up. A verdict that survives
    is shown with the SOURCE's text, never with the quote the row carried.

    A citation naming a version this change no longer spans is withheld rather
    than dropped, and the difference is worth a sentence: the record is intact
    and the document it points at cannot be read for this company, which is a
    statement about the source and is what REASON_VERSION_UNREADABLE says
    everywhere else in the product.
    """
    source = sources.get(stored.citation.version_id)
    if source is None:
        return MaterialityRun(
            withheld=WithheldJudgement(
                reason=REASON_VERSION_UNREADABLE,
                citation_version_id=stored.citation.version_id,
                citation_start=stored.citation.char_start,
                citation_end=stored.citation.char_end,
                citation_quote=stored.citation.quoted_text,
                source_excerpt="",
            )
        )

    # expected_occurrence is not passed, for the reason the fresh path does not
    # pass it: the only value available is derived from the offsets already on
    # the row, so the check would be the record agreeing with itself.
    result = verify_citation(stored.citation, source)

    if not result.verified:
        return MaterialityRun(
            withheld=WithheldJudgement(
                reason=result.reason or "",
                citation_version_id=stored.citation.version_id,
                citation_start=stored.citation.char_start,
                citation_end=stored.citation.char_end,
                citation_quote=stored.citation.quoted_text,
                source_excerpt=result.actual_text or "",
            )
        )

    return MaterialityRun(
        judgement=MaterialityJudgement(
            material=stored.material,
            why=stored.why,
            citation_version_id=stored.citation.version_id,
            citation_start=stored.citation.char_start,
            citation_end=stored.citation.char_end,
            citation_quote=stored.citation.quoted_text,
            actual_text=result.actual_text or "",
            model_id=stored.model_id,
            judged_at=stored.judged_at,
        )
    )


def _record_verdict(
    session: Any, company_id: str, change: Any, judgement: MaterialityJudgement
) -> None:
    """Write the verdict onto the row and the decision into the chain.

    THE SEVEN COLUMNS ARE WRITTEN IN ONE PLACE, which is what makes
    _stored_verdict's refusal above meaningful: a row with a moment and no
    citation cannot have come from here.

    THE ACTOR IS THE MODEL AND THE ROW SAYS SO. actor_kind is ACTOR_MODEL, so
    record_event refuses to attach a user id and nothing downstream can read
    this as a person's judgement. app/auth/policy.py already treats an act on a
    change as authorship of the claims underneath it, so this row also removes
    whoever it names from the approval of what follows -- which is another
    reason the action code may only have one spelling, and it now has one.

    THE MOMENT IS THE JUDGEMENT'S, not the write's. occurred_at is passed rather
    than defaulted so the log and the row cannot disagree about when this was
    known; a second clock read here would be a small, permanent, unexplainable
    discrepancy in the one table that exists to be trusted under dispute.

    NO REASON IS INVENTED. The audit row's reason is the verdict word and the
    model's own sentence, and its citation is the coordinate the verdict rests
    on -- so an auditor reading the log alone can go and re-read the source
    themselves without opening the product.
    """
    from app.state.audit import ACTION_MATERIALITY_SET, ACTOR_MODEL, record_event
    from app.state.models import MATERIALITY_MATERIAL, MATERIALITY_NOT_MATERIAL

    verdict = MATERIALITY_MATERIAL if judgement.material else MATERIALITY_NOT_MATERIAL

    change.materiality = verdict
    change.materiality_why = judgement.why
    change.materiality_citation_version_id = judgement.citation_version_id
    change.materiality_citation_start = judgement.citation_start
    change.materiality_citation_end = judgement.citation_end
    change.materiality_citation_quote = judgement.citation_quote
    change.materiality_model_id = judgement.model_id
    change.materiality_judged_at = judgement.judged_at

    record_event(
        session,
        company_id=company_id,
        actor=f"model:{judgement.model_id}",
        action=ACTION_MATERIALITY_SET,
        subject_type="change",
        subject_id=change.id,
        reason=f"{verdict}: {judgement.why}",
        citation=(
            f"{judgement.citation_version_id}:"
            f"{judgement.citation_start}:{judgement.citation_end}"
        ),
        occurred_at=judgement.judged_at,
        actor_kind=ACTOR_MODEL,
    )


# ------------------------------------------------------------------ tenancy --


def materiality_for_company(
    session: Any,
    company_id: str,
    change_id: str,
    *,
    transport: Transport | None,
    judged_at: datetime | None = None,
) -> MaterialityRun:
    """The scoped entry point: read the verdict, or earn one, and always re-check it.

    THE ORDER IS THE DESIGN. Resolve the change under the scope; narrow the
    sources to the two versions it spans; read whatever verdict the row already
    carries and RUN THE GATE OVER IT; only then, and only when there is nothing
    to show, spend a model call.

    Both reads go through the tenant chokepoints -- change_for_company in
    app/state/claims.py and versions_for_company in app/state/queries.py --
    rather than being queried here. A second scoped read of the same tables is a
    second thing to get wrong, and one of the two would be the one nobody
    audited.

    THE SOURCES ARE NARROWED TO THE TWO VERSIONS THIS CHANGE SPANS. The company
    owns more, and handing them all over would let a citation into an unrelated
    filing verify and be reported as a drop for the wrong reason. The gate reads
    exactly the documents the change is about -- and that narrowing is why a
    stored citation pointing anywhere else comes back unreadable rather than
    quietly verifying against a filing nobody was looking at.

    A change this company cannot read raises. Absence is denial: the caller named
    a change, and judging a different one is worse than refusing. The imports are
    deferred so that everything above this function stays free of persistence, as
    app/verification/verifier.py does for the same reason.

    THE FOUR WAYS THIS ENDS, and only the first two touch the database.

      Nothing stored -> ask the model. A verdict that passes the gate is written
      and audited; a withheld, dropped or degraded run writes NOTHING, because
      there is no verdict to record and a row nobody may show would still be a
      row the project list reads out of the column.

      Stored and it still verifies -> show it. No model call. This is the common
      case and the whole saving.

      Stored and it no longer verifies -> the source moved under a judgement made
      about text that is not there any more, so the verdict is withheld AND the
      model is asked again. A fresh answer that reaches the gate replaces the
      stale row and is what the reader sees, whether it is a verdict, a refusal
      or a drop -- it is about the document as it now reads.

      Stored, stale, and the retry could not answer at all -> the stale refusal
      stands. A run that never reached the model says only "not just now", while
      the refusal says "this change's stored verdict does not verify", and
      falling back to the stored word here is the cached verdict arriving by the
      back door.

    IT WRITES ON A READ, AND THAT IS A REAL COST TO NAME. The change screen is a
    GET, so the first view of an unjudged change is a GET that appends to the
    audit chain. The alternative is a job that does not exist and a screen that
    says "unjudged" until it runs. What makes it defensible is that the write is
    the RECORD of a judgement that was genuinely made at that moment, by
    something the row names, and that a second GET writes nothing at all.
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

    stored = _stored_verdict(change)
    if isinstance(stored, DroppedJudgement):
        return MaterialityRun(dropped=stored)

    held: MaterialityRun | None = None
    if stored is not None:
        held = _reread(stored, sources)
        if held.judgement is not None:
            return held

    fresh = judge_materiality(
        transport, change_under_review(change, sources), sources, judged_at=judged_at
    )

    # A degraded run answered nothing about this change. Where a stale verdict is
    # already refused on this read, that refusal is the more specific truth and
    # it stands; where there is no stored verdict at all, the announcement is
    # what the reader gets, and it is the honest one.
    if fresh.fallback is not None and held is not None:
        return held

    if fresh.judgement is not None:
        _record_verdict(session, company_id, change, fresh.judgement)

    return fresh


# ---------------------------------------------- the readers that never judge --


#: Why there is no verdict on a change nothing has ever judged.
#:
#: A SENTENCE RATHER THAN AN EMPTY STRING, and it is the third state the column
#: exists to keep. An unjudged change and a change whose verdict was refused are
#: opposite facts -- one has never been looked at, the other was looked at and
#: cannot be shown -- and a reader handed a blank for both prints one word for
#: two meanings. app/state/models.py refuses that collapse in the column itself,
#: for the same reason, and this keeps the refusal alive one layer up.
NOT_YET_JUDGED = "nothing has judged how material this change is"


@dataclass(frozen=True, slots=True)
class ShownMateriality:
    """What a screen that cannot judge may say about one change's materiality.

    NO `material` BOOLEAN, and slots=True so nothing can attach one at runtime.
    The same design as WithheldJudgement above and WithheldClaim in
    app/state/claims.py: when the citation does not verify, `verdict` is None
    and there is no other field a template could reach for. A greyed-out verdict
    is still a verdict.

    `judged` is what keeps the two absences apart. It says a verdict is on the
    row, whether or not it may be shown, so a caller can print "not assessed"
    and "withheld" as the different things they are. `reason` is empty only when
    `verdict` is set; every refusal names itself.
    """

    change_id: str
    judged: bool
    verdict: str | None
    reason: str


def shown_materiality_for_company(
    session: Any,
    company_id: str,
    change_ids: Iterable[str],
) -> dict[str, ShownMateriality]:
    """The gate for every screen that SHOWS a verdict without earning one.

    THE BUG THIS FUNCTION EXISTS TO CLOSE, written down because it already
    happened once and the shape of it will happen again.

    Until the verdict was stored, `Change.materiality` was NULL on every row in
    the product. That made every reader of the column safe by accident: there
    was nothing in it to leak. The change that started WRITING the column gated
    exactly one reader -- the change screen, which is also the one that writes
    -- and left two others reading the raw attribute with no citation, no
    verify_citation and no re-read of the source: the project list, which prints
    one row per change and is where the word is seen most often, and the chat
    tool, which hands the word to a model that will restate it in prose. So a
    verdict earned on the change screen, whose cited bytes then moved, was
    WITHHELD on that screen and asserted as fact one link away. That is a claim
    made from the record alone about text that is no longer there, which is the
    thing ADR-003 exists to refuse, and the whole suite stayed green over it
    because no test judged a change and then read a different screen.

    THE FIX IS ONE FUNCTION, NOT TWO PATCHED CALL SITES, and the difference
    matters: patching the two would leave the THIRD reader -- the one nobody has
    written yet -- to repeat the mistake. This is now the only supported way to
    read that column for display, and
    tests/test_propose.py::test_no_surface_reads_the_materiality_column_raw
    walks the syntax tree of app/ to keep it that way.

    IT RUNS THE SAME GATE AS materiality_for_company AND THE SAME NARROWING.
    Both matter. The gate is `_reread`, so a stored verdict is shown only
    because its quote is still at its offsets in the source as the database
    holds it right now. The narrowing is the two versions the change spans, so a
    stored citation pointing anywhere else comes back unreadable rather than
    quietly verifying against an unrelated filing. A reader that skipped either
    would be MORE permissive than the screen that earned the verdict, and the
    two surfaces would print different words about the same row -- which is the
    contradiction, not the cure.

    IT CALLS NO MODEL AND WRITES NOTHING, and there is no transport argument to
    change that. A list of thirty rows must not be thirty model calls, thirty
    audit rows and thirty writes on a GET. So a stale verdict here is simply
    withheld; earning a fresh one is the change screen's job, one click away.
    That asymmetry is deliberate and it is the honest half: the reader refuses,
    it never guesses, and it never quietly repairs the row underneath a person
    who only asked to see a list.

    ONE SOURCE READ FOR THE WHOLE BATCH. The versions are fetched once through
    versions_for_company and then narrowed per change, rather than fetched per
    change, because thirty rows would otherwise be thirty scans of the same
    table. The scope is checked once, up front, and each change is resolved
    through change_for_company -- the audited chokepoint -- so a change id this
    company cannot read RAISES rather than being dropped from the map. Absence
    is denial: a missing key would let a caller print "not assessed" for a row
    it was never allowed to see, and that reads as a fact about the change
    rather than as the scope refusal it is.
    """
    from app.state.claims import change_for_company
    from app.state.queries import _require_scope, versions_for_company

    from app.state.models import MATERIALITY_MATERIAL, MATERIALITY_NOT_MATERIAL

    _require_scope(company_id)

    # dict.fromkeys rather than set(): the caller's order is the screen's order,
    # and a set would hand back rows in a different one each run.
    wanted = list(dict.fromkeys(change_ids))
    if not wanted:
        return {}

    sources = {
        version.id: version.source_text
        for version in versions_for_company(session, company_id)
    }

    shown: dict[str, ShownMateriality] = {}
    for change_id in wanted:
        change = change_for_company(session, company_id, change_id)
        if change is None:
            raise ValueError(
                f"no change {change_id!r} readable for company {company_id!r}; "
                "refusing to report a materiality this company cannot read"
            )

        stored = _stored_verdict(change)
        if stored is None:
            shown[change_id] = ShownMateriality(
                change_id=change_id, judged=False, verdict=None, reason=NOT_YET_JUDGED
            )
            continue
        if isinstance(stored, DroppedJudgement):
            shown[change_id] = ShownMateriality(
                change_id=change_id, judged=True, verdict=None, reason=stored.reason
            )
            continue

        spanned = {change.from_version_id, change.to_version_id}
        run = _reread(
            stored,
            {
                version_id: text
                for version_id, text in sources.items()
                if version_id in spanned
            },
        )
        if run.judgement is None:
            shown[change_id] = ShownMateriality(
                change_id=change_id,
                judged=True,
                verdict=None,
                reason=run.withheld.reason,
            )
            continue

        shown[change_id] = ShownMateriality(
            change_id=change_id,
            judged=True,
            verdict=(
                MATERIALITY_MATERIAL
                if run.judgement.material
                else MATERIALITY_NOT_MATERIAL
            ),
            reason="",
        )

    return shown


__all__ = [
    "ANNOUNCEMENT_NO_API_KEY",
    "ANNOUNCEMENT_TRANSPORT_FAILED",
    "ANNOUNCEMENT_UNREADABLE_RESPONSE",
    "DROP_EMPTY_QUOTE",
    "DROP_MALFORMED",
    "DROP_OUTSIDE_THE_CHANGE",
    "DROP_OUT_OF_RANGE",
    "DROP_STORED_INCOMPLETE",
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
    "NOT_YET_JUDGED",
    "ShownMateriality",
    "Span",
    "StoredChange",
    "Transport",
    "WithheldJudgement",
    "change_under_review",
    "judge_materiality",
    "materiality_for_company",
    "shown_materiality_for_company",
    "transport_from_environment",
]
