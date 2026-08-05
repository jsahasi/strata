"""Obligation extraction, scored by cost rather than by average.

    .venv/bin/python -m app.evals.obligations --predictions FILE
    .venv/bin/python -m app.evals.obligations --no-extractor

WHY THIS IS A SECOND SCORECARD AND NOT A SIXTH METRIC. `app/evals/run.py`
scores the deterministic spine against `data/manifest.json`, a synthetic corpus
of three versions and five labelled changes. This scores a different task
against different evidence: 51 passages of real regulatory text from four
states, each labelled by a model, single annotator, no adjudication with whether it places a duty on a utility. The
two share the `Metric` shape, the `Sample` rule and the exit-code contract --
they are imported here, never restated -- and nothing in this module is wired
into `make eval`, which still prints five metrics and must keep doing so.

--------------------------------------------------------------------------
THE ONE IDEA: FOUR OUTCOMES, FOUR COSTS, AND NO AVERAGE
--------------------------------------------------------------------------

An extractor reading a paragraph of a commission order can be wrong in two
directions, and the two are not worth the same.

  WRONG ASSERTION. The system said a duty exists where the gold set says none
  does, or named a different duty than the one the passage creates. This is a
  false claim about a regulatory obligation, made confidently, by a product
  whose entire pitch is that it does not do that. An analyst who trusts it
  files something nobody asked for, or briefs a director on a deadline that
  does not exist. TARGET IS ZERO. Any non-zero fails the build.

  MISSED. The gold set says there is a duty and the system declined to extract
  it. The cost is an afternoon: somebody reads the paragraph themselves. Real,
  and survivable, and it is the cost this product is designed to pay. Reported,
  never blocking.

  CORRECT ASSERTION and CORRECT REFUSAL. The two good outcomes, counted apart
  because they are not interchangeable either. A system that refuses everything
  scores full marks on correct refusals; a system that asserts everything scores
  full marks on correct assertions. Either number alone is a lie, and the only
  defence is to print both beside the two failure counts.

SO THERE IS NO F1 HERE, AND `combined_f1()` RAISES RATHER THAN RETURNING ONE.
An F1 averages precision and recall, which means it trades a catastrophe
against an inconvenience at a fixed exchange rate nobody chose. Two systems
with the same F1 -- one that asserts four duties that do not exist, one that
misses four that do -- are not the same system, and on this product they are
not even close. The refusal is a function that raises, so a future caller
reaching for the single number gets a sentence rather than a number, and the
same sentence is printed on the scorecard where a reader will meet it.

IF YOU WANT ONE NUMBER, TAKE THE WRONG-ASSERTION COUNT. It is the only figure
here that is worth quoting on its own, and it is an integer with a target of
zero rather than a rate with a denominator somebody will forget.

--------------------------------------------------------------------------
WHAT COUNTS AS THE WRONG DUTY, AND WHY THE SCORER WILL NOT DECIDE IT
--------------------------------------------------------------------------

"The system asserted the wrong duty" needs a comparison between two sentences
of English. Nothing offline can make that call, and a scorer that made it with
a word-overlap rule would be grading regulatory meaning with the same machinery
`app/state/mapping.py` refuses to grade it with one layer up.

So the scorer does not decide it. A prediction may carry `duty_verdict`, which
is a person's adjudication of whether the extracted duty is the gold duty:

  "same"       -> CORRECT ASSERTION
  "different"  -> WRONG ASSERTION, and it blocks
  absent       -> UNADJUDICATED ASSERTION

An unadjudicated assertion is NOT credited as correct. That is the whole point
of having a fifth bucket: the alternative is to score an assertion nobody
checked as a success, which is the same failure as scoring a refusal as a
success. It does not block either, because nobody has established it is wrong.
It sits on the page as work outstanding.

--------------------------------------------------------------------------
ABSENCE IS DENIAL, APPLIED TO THE RUN ITSELF
--------------------------------------------------------------------------

`refuse_a_short_run()` is `refuse_a_short_proposal()`'s argument transplanted:
a run that says nothing about a passage reads exactly like a run over a smaller
gold set. Every labelled passage must carry an extraction record, and a record
naming a passage the gold set does not hold is refused too. A missing record is
never quietly folded into "refused" -- a refusal is a decision the system made,
and silence is a decision nobody made.

The same rule runs upward. An empty gold set does not score 100 per cent, it
raises: a scorer that reports perfection over no data is how a green build
lies. A gold set with no positive labels cannot measure a miss, and one with no
negatives cannot measure a correct refusal, so both refuse rather than print a
metric over an empty denominator.

--------------------------------------------------------------------------
THE SAMPLE, AND WHY IT IS FOUR AND NOT FIFTY-ONE
--------------------------------------------------------------------------

`app/evals/report.py` owns the rule: a rate prints only when the independent
sample reaches ten, and the sample is derived from the identity of the evidence
rather than declared by the metric. The identity here is the JURISDICTION.

Fifty-one passages is the work done. It is not fifty-one independent tests of
an extractor. Nineteen of them come from Georgia commission orders, which share
a drafter, a template and a decretal vocabulary -- "ORDERED FURTHER, that the
Company shall" is one drafting tradition read nineteen times, and an extractor
tuned to it would score nineteen wins that say nothing about Missouri. Folding
by source document instead gives 16, which clears the floor and would print a
percentage; that reading is available and is printed in the notes, but it is
not the one this scorecard rests on, because two orders in one docket are not
independent evidence about drafting style either.

Four jurisdictions is below the floor, so this scorecard prints counts and no
rate at all. The per-jurisdiction breakdown is therefore not a garnish: it is
the sample, itemised, and a result driven by one state's drafting style is
visible in it rather than hidden inside a total.

--------------------------------------------------------------------------
THE AMBIGUOUS SHARE, PRINTED BEFORE ANY SCORE
--------------------------------------------------------------------------

Seventeen of the 51 passages carry `ambiguous: true` -- the labeller's own note
that a competent analyst could argue the other way. A score computed over
passages experts would dispute needs that caveat attached to it always, and
attached in front rather than in a footnote, because the caveat is what tells a
reader how much of the number is measurement and how much is one person's
reading. It is stated up front, and again per jurisdiction.

One annotator, no second pass, no adjudication. So the ambiguous flag is a
judgement of contestability, not a measured disagreement rate, and this module
cannot turn it into one. Both gold sets say so in their own `known_limits`, and
those lines are printed verbatim rather than summarised here, so the caveat
cannot drift from the data it is about.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.evals.metrics import Metric, Observation
from app.evals.report import Sample, count_phrase

# Imported rather than copied. These are the renderer's own helpers and they
# are private to app/evals/run.py, but a second scorecard that laid its own
# columns out would drift from the first one the week after it was written --
# and a reviewer reading two eval outputs should not have to notice which
# module printed which. If run.py's shape changes, this import breaks loudly.
from app.evals.run import BLOCKER, WIDTH, _bullet, _field, _headline, _rule

DEFAULT_GOLDSET_DIR = Path(__file__).resolve().parents[2] / "data" / "evals"
DEFAULT_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "data" / "real"

# ---------------------------------------------------------------------------
# The four outcomes, and the fifth that is not an outcome but an absence
# ---------------------------------------------------------------------------

#: Asserted a duty the gold set says is not there, or named a different duty.
#: The catastrophic case. Target zero, and non-zero fails the build.
WRONG_ASSERTION = "WRONG_ASSERTION"

#: The gold set says there is a duty here and the system declined to extract
#: it. A person reads the paragraph by hand. Reported, never blocking.
MISSED = "MISSED"

#: Asserted the duty the gold set holds, and a person adjudicated it the same.
CORRECT_ASSERTION = "CORRECT_ASSERTION"

#: The gold set says no duty here and the system declined to assert one.
CORRECT_REFUSAL = "CORRECT_REFUSAL"

#: Asserted where the gold set holds a duty, and nobody has said whether it is
#: THAT duty. Not credited, not blocked, printed as work outstanding.
UNADJUDICATED_ASSERTION = "UNADJUDICATED_ASSERTION"

OUTCOMES = (
    WRONG_ASSERTION,
    MISSED,
    CORRECT_ASSERTION,
    CORRECT_REFUSAL,
    UNADJUDICATED_ASSERTION,
)

#: The two values a person's adjudication of an asserted duty may take.
DUTY_SAME = "same"
DUTY_DIFFERENT = "different"
DUTY_VERDICTS = (DUTY_SAME, DUTY_DIFFERENT)

#: What a prediction file records when no extractor ran at all.
SOURCE_NO_EXTRACTOR = "NO_OBLIGATION_EXTRACTOR_IN_THIS_BUILD"

#: Printed on the scorecard, and raised by `combined_f1`. One sentence in two
#: places rather than two sentences that can disagree.
NO_COMBINED_SCORE = (
    "This scorecard emits no F1 and no single combined score. An F1 averages a "
    "wrong assertion against a miss, and on this product those two cost "
    "different orders: a wrong assertion is a false claim about a regulatory "
    "duty, a miss is an analyst reading the paragraph by hand. Averaging them "
    "hides the only number that matters. If you want one number, take the "
    "wrong-assertion count -- an integer whose target is zero."
)

#: The sentence the no-extractor run prints instead of a result. A fallback
#: that does not announce itself is the failure best-practices.html section 26
#: is about, and an empty run is the loudest fallback in this file.
NO_EXTRACTOR_ANNOUNCEMENT = (
    "NO EXTRACTOR RAN. This build has no offline path that reads a passage and "
    "says what duty it creates, so every passage below is recorded as a "
    "refusal and nothing was asserted. A system that asserts nothing cannot "
    "assert wrongly, so the blocking metric passes and that pass means "
    "nothing. The figure that carries information today is the miss count. Do "
    "not quote the exit code of this run."
)

# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class ScoreRefused(ValueError):
    """Scoring this would produce a number that misleads. The reason is inside.

    A subclass of ValueError so callers already catching ValueError keep
    catching it, and a distinct name so a caller that has to answer differently
    can tell it from a bad argument.
    """


class EmptyGoldSet(ScoreRefused):
    """No labels, so no score. Never 100 per cent over nothing."""


class SingleNumberRefused(ScoreRefused):
    """Somebody asked this scorecard for one number that averages two costs."""


def combined_f1(*_args, **_kwargs) -> float:
    """Refuse. Always. The refusal is the feature, and the reason is the message.

    This exists so the refusal is reachable from code rather than living only
    in a comment. A future caller wiring an obligation score into a dashboard
    finds a function with the right name, calls it, and gets the argument.
    """
    raise SingleNumberRefused(NO_COMBINED_SCORE)


# ---------------------------------------------------------------------------
# Jurisdictions
# ---------------------------------------------------------------------------

#: Full state names to the two-letter codes the file names use. Kept short and
#: guarded rather than kept complete: a name that is not here REFUSES at load
#: rather than being bucketed under a guess, so the list cannot silently miss a
#: state the way a hand-kept stopword list silently misses a word.
_STATE_CODES = {
    "georgia": "GA",
    "indiana": "IN",
    "kentucky": "KY",
    "missouri": "MO",
    "utah": "UT",
}


def normalise_jurisdiction(name: str) -> str:
    """A declared jurisdiction as a two-letter code, or a refusal."""
    text = (name or "").strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    code = _STATE_CODES.get(text.casefold())
    if code is None:
        raise ScoreRefused(
            f"{name!r} is not a jurisdiction this scorer can place. State the "
            "two-letter code in the gold set, or add the name to "
            "app/evals/obligations.py::_STATE_CODES. It is refused rather "
            "than bucketed under a guess, because a passage counted under the "
            "wrong state makes the per-jurisdiction breakdown worse than "
            "absent."
        )
    return code


def jurisdiction_of(source_file: str) -> str:
    """The state a source file belongs to, from its name.

    Every file in `data/real` is named `<state>-<docket>-<what-it-is>.txt`, so
    the first token is the jurisdiction. Derived rather than stored, and then
    checked against what the gold set itself declares -- see `load_goldset`.
    Two readings that agree are worth more than one reading nobody checked.
    """
    head = (source_file or "").split("-", 1)[0]
    if not head:
        raise ScoreRefused(
            f"{source_file!r} does not begin with a jurisdiction token, so the "
            "passage cannot be placed in the per-jurisdiction breakdown."
        )
    return normalise_jurisdiction(head)


# ---------------------------------------------------------------------------
# The gold set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldPassage:
    """One labelled passage: the text, the verdict, and the labeller's doubt."""

    id: str
    goldset: str
    source_file: str
    jurisdiction: str
    start: int
    end: int
    quote: str
    creates_obligation: bool
    obligation_text: str | None
    who_is_bound: str | None
    ambiguous: bool
    why: str


@dataclass(frozen=True)
class GoldSet:
    """One labelled file, with the limits its own labeller wrote down."""

    name: str
    path: Path
    jurisdictions: tuple[str, ...]
    known_limits: tuple[str, ...]
    passages: tuple[GoldPassage, ...]


#: The two keys the two committed gold sets use for the same list. They were
#: written by different passes and neither is wrong; reading both here costs
#: five lines and is cheaper than a migration nobody has time for. A file
#: carrying BOTH is refused, because then nothing here knows which is the set.
_LABEL_KEYS = ("passages", "labels")

_REQUIRED_FIELDS = (
    "id",
    "source_file",
    "start",
    "end",
    "quote",
    "creates_obligation",
    "ambiguous",
)


def load_goldset(path: Path | str) -> GoldSet:
    """Read one labelled file. Refuse anything this scorer cannot place.

    Every refusal here is a case where guessing would put a passage in the
    wrong bucket rather than leave it out: a missing field, a jurisdiction the
    file does not declare, a label list under two names at once.
    """
    path = Path(path)
    raw = json.loads(path.read_bytes().decode("utf-8"))

    present = [key for key in _LABEL_KEYS if key in raw]
    if len(present) != 1:
        raise ScoreRefused(
            f"{path.name} holds its labels under {present or 'no'} key of "
            f"{list(_LABEL_KEYS)}. Exactly one is required: with none there is "
            "nothing to score, and with both nothing here knows which list is "
            "the gold set."
        )
    rows = raw[present[0]]

    declared = tuple(
        normalise_jurisdiction(name) for name in raw.get("jurisdictions", ())
    )
    name = raw.get("name") or path.stem

    passages: list[GoldPassage] = []
    for row in rows:
        missing = [field for field in _REQUIRED_FIELDS if field not in row]
        if missing:
            raise ScoreRefused(
                f"{path.name}: a label is missing {sorted(missing)}. "
                f"Row: {row.get('id', '<no id>')!r}."
            )
        code = jurisdiction_of(row["source_file"])
        if declared and code not in declared:
            raise ScoreRefused(
                f"{path.name}: {row['id']!r} sits in a {code} file, and the "
                f"gold set declares {list(declared)}. A passage counted under "
                "a state its own file contradicts would skew the "
                "per-jurisdiction breakdown, so this refuses."
            )
        passages.append(
            GoldPassage(
                id=row["id"],
                goldset=name,
                source_file=row["source_file"],
                jurisdiction=code,
                start=int(row["start"]),
                end=int(row["end"]),
                quote=row["quote"],
                creates_obligation=bool(row["creates_obligation"]),
                obligation_text=row.get("obligation_text"),
                who_is_bound=row.get("who_is_bound"),
                ambiguous=bool(row["ambiguous"]),
                why=row.get("why", ""),
            )
        )

    ids = [passage.id for passage in passages]
    if len(ids) != len(set(ids)):
        repeated = sorted({one for one in ids if ids.count(one) > 1})
        raise ScoreRefused(
            f"{path.name} labels {repeated} more than once, so every count "
            "over it would double-count them."
        )

    return GoldSet(
        name=name,
        path=path,
        jurisdictions=declared,
        known_limits=tuple(raw.get("known_limits", ())),
        passages=tuple(passages),
    )


def load_goldsets(directory: Path | str | None = None) -> tuple[GoldSet, ...]:
    """Every `goldset_*.json` in a directory, in name order.

    A directory holding none of them refuses rather than returning an empty
    tuple that would go on to score nothing as everything.
    """
    directory = Path(directory) if directory is not None else DEFAULT_GOLDSET_DIR
    found = sorted(directory.glob("goldset_*.json"))
    if not found:
        raise EmptyGoldSet(
            f"no goldset_*.json under {directory}. There is nothing to score, "
            "and a scorecard over nothing is not an empty scorecard -- it is a "
            "green one."
        )
    return tuple(load_goldset(path) for path in found)


def all_passages(goldsets: tuple[GoldSet, ...]) -> tuple[GoldPassage, ...]:
    """Every labelled passage across the sets, refusing an id used twice."""
    passages = tuple(
        passage for goldset in goldsets for passage in goldset.passages
    )
    ids = [passage.id for passage in passages]
    if len(ids) != len(set(ids)):
        repeated = sorted({one for one in ids if ids.count(one) > 1})
        raise ScoreRefused(
            f"{repeated} is labelled in more than one gold set. Two labels for "
            "one passage may disagree, and nothing here would notice."
        )
    return passages


# ---------------------------------------------------------------------------
# What the system said
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    """One passage's outcome from a run of the system.

    `asserted` is the only field the scorer needs to find a wrong assertion
    against a negative label. `duty_verdict` is a PERSON's adjudication of
    whether an asserted duty is the gold duty, and it is absent by default,
    because nothing offline can decide it and a scorer that decided it would be
    grading regulatory meaning with word overlap.
    """

    passage_id: str
    asserted: bool
    obligation_text: str = ""
    who_is_bound: str = ""
    duty_verdict: str | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.duty_verdict is not None and self.duty_verdict not in DUTY_VERDICTS:
            raise ScoreRefused(
                f"{self.passage_id}: duty_verdict is {self.duty_verdict!r}; it "
                f"is one of {list(DUTY_VERDICTS)} or absent. An unrecognised "
                "verdict would be read as 'nobody adjudicated it', which is a "
                "different fact."
            )
        if not self.asserted and self.duty_verdict is not None:
            raise ScoreRefused(
                f"{self.passage_id}: a refusal carries no duty to adjudicate, "
                f"and this one carries {self.duty_verdict!r}."
            )


def no_extractor_run(passages: tuple[GoldPassage, ...]) -> tuple[Extraction, ...]:
    """A refusal for every passage, recorded rather than assumed.

    THIS IS NOT A RESULT AND THE SCORECARD SAYS SO IN CAPITALS. It exists
    because the shape of the scorecard has to be visible on a build with no
    extractor in it, and because the alternative -- letting a missing
    predictions file mean "everything refused" -- is a silence the reader would
    have to know to look for.
    """
    return tuple(
        Extraction(passage_id=passage.id, asserted=False, source=SOURCE_NO_EXTRACTOR)
        for passage in passages
    )


def load_predictions(path: Path | str) -> tuple[Extraction, ...]:
    """Read a run's output. The format is documented here and nowhere else.

        {
          "run": "a name for this run",
          "produced_by": "what made it, and when",
          "extractions": [
            {"id": "ga-44280-01", "asserted": true,
             "obligation_text": "...", "who_is_bound": "...",
             "duty_verdict": "same"},
            {"id": "ga-44280-05", "asserted": false}
          ]
        }

    `duty_verdict` is a person's adjudication and is omitted where nobody has
    made one. Omitting it is honest; inventing it is not.
    """
    path = Path(path)
    raw = json.loads(path.read_bytes().decode("utf-8"))
    if "extractions" not in raw:
        raise ScoreRefused(
            f"{path.name} holds no 'extractions' list. See "
            "app/evals/obligations.py::load_predictions for the shape."
        )
    source = raw.get("run") or path.stem
    return tuple(
        Extraction(
            passage_id=row["id"],
            asserted=bool(row["asserted"]),
            obligation_text=row.get("obligation_text") or "",
            who_is_bound=row.get("who_is_bound") or "",
            duty_verdict=row.get("duty_verdict"),
            source=source,
        )
        for row in raw["extractions"]
    )


def refuse_a_short_run(
    extractions: tuple[Extraction, ...] | list[Extraction],
    passages: tuple[GoldPassage, ...] | list[GoldPassage],
) -> None:
    """Raise unless the run says something about every labelled passage.

    A RUN WITH A PASSAGE MISSING IS NOT A SHORTER RUN, IT IS A WRONG ONE. It
    reads exactly like a complete run over a smaller gold set, and the reader
    draws the conclusion the missing row would have contradicted. The same
    argument app/state/mapping.py::refuse_a_short_proposal makes about a
    proposal that leaves a duty out, applied to the run instead of the read.

    A record naming a passage nobody labelled is refused for the mirror
    reason: it is scored against nothing, so it can only inflate a denominator.
    """
    said = [row.passage_id for row in extractions]
    wanted = {passage.id for passage in passages}
    seen = set(said)
    missing = sorted(one for one in wanted if one not in seen)
    extra = sorted(one for one in seen if one not in wanted)
    if missing or extra:
        raise ScoreRefused(
            "the run does not cover the gold set: silent on "
            f"{missing}, unexpected {extra}. A passage the run said nothing "
            "about is not a refusal -- a refusal is a decision the system "
            "made, and silence is a decision nobody made."
        )
    if len(said) != len(seen):
        repeated = sorted({one for one in said if said.count(one) > 1})
        raise ScoreRefused(
            f"the run reports {repeated} more than once, so its counts would "
            "double-count them."
        )


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Judged:
    """One passage, what the system did with it, and which of the five it is."""

    gold: GoldPassage
    extraction: Extraction
    outcome: str

    @property
    def line(self) -> str:
        """One line a reader can act on: the passage, the state, the doubt."""
        flag = ", labelled ambiguous" if self.gold.ambiguous else ""
        return f"{self.gold.id} [{self.gold.jurisdiction}{flag}]"


def judge(gold: GoldPassage, extraction: Extraction) -> Judged:
    """Place one passage in one of the five buckets. No arithmetic, no scoring.

    The whole taxonomy is these seven lines, and it is kept in one function so
    that a change to what counts as catastrophic is one diff rather than a
    search through a renderer.
    """
    if gold.creates_obligation:
        if not extraction.asserted:
            outcome = MISSED
        elif extraction.duty_verdict == DUTY_DIFFERENT:
            outcome = WRONG_ASSERTION
        elif extraction.duty_verdict == DUTY_SAME:
            outcome = CORRECT_ASSERTION
        else:
            outcome = UNADJUDICATED_ASSERTION
    else:
        outcome = WRONG_ASSERTION if extraction.asserted else CORRECT_REFUSAL
    return Judged(gold=gold, extraction=extraction, outcome=outcome)


def _why_wrong(judged: Judged) -> str:
    """The sentence beside a wrong assertion. What was claimed, against what."""
    if judged.gold.creates_obligation:
        return (
            f"{judged.line} -- asserted a duty a person adjudicated as NOT the "
            f"duty in this passage. Gold: {judged.gold.obligation_text!r}. "
            f"Asserted: {judged.extraction.obligation_text!r}."
        )
    return (
        f"{judged.line} -- asserted a duty where the gold set says there is "
        f"none. Asserted: {judged.extraction.obligation_text!r}. The label's "
        f"reason: {judged.gold.why}"
    )


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------


def _sample(rows: tuple[Judged, ...], what: str) -> Sample:
    """The jurisdictions behind a count, with the passage count beside them.

    See the module docstring. Nineteen Georgia passages are nineteen readings
    of one drafting tradition, not nineteen independent tests, so the state is
    the identity and four is the n. That is below the floor in
    app/evals/report.py, which is why nothing on this scorecard prints a rate.
    """
    return Sample(
        unit="jurisdictions",
        probe_unit=what,
        identities=tuple(row.gold.jurisdiction for row in rows),
    )


def wrong_assertion_metric(judgements: tuple[Judged, ...]) -> Metric:
    """The blocking one. Target zero, and any non-zero fails the build."""
    wrong = tuple(row for row in judgements if row.outcome == WRONG_ASSERTION)
    return Metric(
        key="obligation_wrong_assertions",
        title="Wrong assertions",
        measures=(
            "Whether the system asserted a regulatory duty the gold set says "
            "is not there, or asserted a duty a person adjudicated as not the "
            "duty the passage creates."
        ),
        method=(
            "Every labelled passage is placed in one of five buckets by "
            "app/evals/obligations.py::judge. A passage counts against this "
            "metric when the system asserted and the gold label is negative, "
            "or when it asserted on a positive label and a person recorded "
            "duty_verdict 'different'. Nothing here compares two sentences of "
            "English; the adjudication is a person's and comes in with the run."
        ),
        threshold=(
            "Zero, with no exception. A false claim about a regulatory duty is "
            "the failure this product exists to prevent, so one of them is a "
            "release blocker and no number of correct answers offsets it."
        ),
        hits=len(judgements) - len(wrong),
        total=len(judgements),
        unit="labelled passages carry no wrong assertion",
        sample=_sample(judgements, "labelled passages"),
        blocking=True,
        failures=tuple(_why_wrong(row) for row in wrong),
        notes=(
            f"{len(wrong)} wrong assertions. THIS IS THE ONE NUMBER ON THIS "
            "PAGE WORTH QUOTING ALONE, and it is an integer with a target of "
            "zero rather than a rate whose denominator gets forgotten.",
        ),
    )


def miss_metric(judgements: tuple[Judged, ...]) -> Metric:
    """Real cost, survivable, never blocking. An analyst reads it by hand."""
    positives = tuple(row for row in judgements if row.gold.creates_obligation)
    if not positives:
        raise ScoreRefused(
            "the gold set labels no obligations at all, so nothing here can "
            "measure a miss. A metric over an empty denominator is not a "
            "metric."
        )
    correct = tuple(row for row in positives if row.outcome == CORRECT_ASSERTION)
    missed = tuple(row for row in positives if row.outcome == MISSED)
    unadjudicated = tuple(
        row for row in positives if row.outcome == UNADJUDICATED_ASSERTION
    )
    wrong = tuple(row for row in positives if row.outcome == WRONG_ASSERTION)
    ambiguous = sum(1 for row in positives if row.gold.ambiguous)
    return Metric(
        key="obligation_misses",
        title="Obligations extracted, and the ones missed",
        measures=(
            "Of the duties the gold set says are in these passages, how many "
            "the system extracted and a person adjudicated as the same duty."
        ),
        method=(
            "Counted over the positive labels only. A positive label the "
            "system refused is a MISS; one it asserted with no adjudication is "
            "counted in neither column and printed on its own line, because "
            "crediting an assertion nobody checked is the same failure as "
            "crediting a refusal."
        ),
        threshold=(
            "None, deliberately. A miss costs an analyst an afternoon reading "
            "the paragraph themselves, which is the cost this product is "
            "designed to pay rather than the one it refuses to pay. This "
            "number is reported and never blocks a release."
        ),
        hits=len(correct),
        total=len(positives),
        unit="labelled obligations extracted and adjudicated correct",
        sample=_sample(positives, "positive labels"),
        blocking=False,
        failures=(),
        notes=(
            f"Of {len(positives)} positive labels: {len(correct)} correct, "
            f"{len(missed)} missed, {len(unadjudicated)} asserted but "
            f"unadjudicated, {len(wrong)} asserted wrongly. The four sum to "
            "the total and are printed apart because they cost different "
            "things.",
            f"{ambiguous} of the {len(positives)} positive labels are marked "
            "ambiguous by the labeller, so a miss on one of those is a "
            "disagreement about a hard passage rather than a plain failure.",
        )
        + tuple(f"missed: {row.line}" for row in missed[:12])
        + (
            (f"...and {len(missed) - 12} more misses.",) if len(missed) > 12 else ()
        ),
    )


def correct_refusal_metric(judgements: tuple[Judged, ...]) -> Metric:
    """The other good outcome. Full marks here alone means nothing."""
    negatives = tuple(
        row for row in judgements if not row.gold.creates_obligation
    )
    if not negatives:
        raise ScoreRefused(
            "the gold set holds no negative labels, so nothing here can "
            "measure a correct refusal, and the wrong-assertion metric has no "
            "hard negatives to work against."
        )
    refused = tuple(row for row in negatives if row.outcome == CORRECT_REFUSAL)
    ambiguous = sum(1 for row in negatives if row.gold.ambiguous)
    return Metric(
        key="obligation_correct_refusals",
        title="Correct refusals",
        measures=(
            "Of the passages the gold set says create no duty on the utility, "
            "how many the system declined to assert on."
        ),
        method=(
            "Counted over the negative labels only. These are deliberate hard "
            "negatives: recitals, denials, grants of authority, disclaimers of "
            "obligation, duties on Staff or on customers, and proposals no "
            "commission has adopted. Ten of the 28 contain 'shall' and 14 carry any mandatory modal -- half the negatives read like duties and are not."
        ),
        threshold=(
            "None on its own, and that is the point. A system that refuses "
            "every passage scores full marks here, so this number is only "
            "readable beside the extraction count above it. Neither is a "
            "result alone."
        ),
        hits=len(refused),
        total=len(negatives),
        unit="hard negatives correctly refused",
        sample=_sample(negatives, "negative labels"),
        blocking=False,
        failures=(),
        notes=(
            "A system that asserts nothing scores full marks on this line. "
            "Read it against the extraction count, never alone.",
            f"{ambiguous} of the {len(negatives)} negative labels are marked "
            "ambiguous -- the labeller's own note that an analyst could "
            "reasonably call them obligations.",
        ),
    )


def goldset_offsets_metric(
    passages: tuple[GoldPassage, ...], corpus_root: Path
) -> Metric:
    """Does each labelled quote still slice out of the file it names?

    The same move `app/evals/run.py` makes for the manifest, for the same
    reason: a gold set whose offsets no longer re-read is scoring against text
    that moved, and every verdict computed over it expired without saying so
    (best-practices.html section 28). Blocking, because a score over a corpus
    that shifted underneath it is worse than no score.
    """
    failures: list[str] = []
    verified = 0
    cache: dict[str, str | None] = {}
    for passage in passages:
        if passage.source_file not in cache:
            path = corpus_root / passage.source_file
            cache[passage.source_file] = (
                path.read_bytes().decode("utf-8") if path.is_file() else None
            )
        text = cache[passage.source_file]
        if text is None:
            failures.append(
                f"{passage.id} -- {passage.source_file} is not under "
                f"{corpus_root}, so its quote could not be re-read."
            )
        elif text[passage.start : passage.end] != passage.quote:
            failures.append(
                f"{passage.id} -- {passage.source_file}"
                f"[{passage.start}:{passage.end}] no longer slices to the "
                "quote the gold set records."
            )
        else:
            verified += 1
    return Metric(
        key="goldset_offsets",
        title="Gold set offsets",
        measures=(
            "Whether every labelled quote still re-reads from the source file "
            "at the offsets the gold set records."
        ),
        method=(
            "Each source file is read as bytes and decoded, and "
            "text[start:end] is compared with the recorded quote. Plain string "
            "slicing; nothing in app/ is asked whether the answer is right."
        ),
        threshold=(
            "Every offset. A gold set whose quotes have moved is scoring "
            "against text that is no longer there, and every count computed "
            "over it is stale rather than wrong -- which is worse, because "
            "stale numbers look fine."
        ),
        hits=verified,
        total=len(passages),
        unit="labelled quotes re-read from their source",
        sample=Sample(
            unit="source documents",
            probe_unit="labelled quotes",
            identities=tuple(passage.source_file for passage in passages),
        ),
        blocking=True,
        failures=tuple(failures),
        notes=(
            f"{len({p.source_file for p in passages})} source documents under "
            f"{corpus_root}.",
        ),
    )


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObligationScorecard:
    """The same shape app/evals/run.py::Scorecard carries, over other evidence."""

    goldsets: tuple[GoldSet, ...]
    judgements: tuple[Judged, ...]
    metrics: tuple[Metric, ...]
    observations: tuple[Observation, ...]
    run_note: str = ""

    @property
    def failures(self) -> tuple[Metric, ...]:
        return tuple(metric for metric in self.metrics if not metric.passed)

    @property
    def blocking_failures(self) -> tuple[Metric, ...]:
        return tuple(metric for metric in self.failures if metric.blocking)

    @property
    def exit_code(self) -> int:
        return 1 if self.blocking_failures else 0

    @property
    def counts(self) -> Counter:
        """One count per outcome, every outcome present even at zero.

        Zeroes are kept for the reason app/state/mapping.py keeps its empty
        rows: an outcome missing from a table reads as an outcome that cannot
        happen.
        """
        tally = Counter({outcome: 0 for outcome in OUTCOMES})
        tally.update(row.outcome for row in self.judgements)
        return tally

    @property
    def wrong_assertions(self) -> int:
        """The one number. Quotable alone; nothing else here is."""
        return self.counts[WRONG_ASSERTION]

    @property
    def ambiguous(self) -> int:
        return sum(1 for row in self.judgements if row.gold.ambiguous)


def _ambiguity_observation(card_passages: tuple[GoldPassage, ...]) -> Observation:
    total = len(card_passages)
    ambiguous = sum(1 for passage in card_passages if passage.ambiguous)
    return Observation(
        title="AMBIGUOUS SHARE -- read this before any count below",
        lines=(
            f"{ambiguous} of {total} labelled passages are marked ambiguous by "
            "the labeller: passages where a competent regulatory analyst could "
            "argue the other way. Every count on this page is computed over "
            "all of them, so roughly a third of the evidence is contestable "
            "and the caveat travels with the number.",
            "One annotator on each set, no second pass and no adjudication. "
            "The ambiguous flag is therefore one person's judgement of what "
            "another analyst might dispute -- it is NOT a measured "
            "inter-annotator disagreement rate, and this scorer cannot turn it "
            "into one.",
            "Hard cases were chosen on purpose. Neither set is a random "
            "sample, so nothing here estimates accuracy over a whole document.",
        ),
    )


def _jurisdiction_observation(judgements: tuple[Judged, ...]) -> Observation:
    """Counts per state, so a result carried by one drafting style is visible.

    Nineteen of the 51 passages are Georgia commission orders. An extractor
    tuned to "ORDERED FURTHER, that the Company shall" would post a strong
    total and be useless in Missouri, and only this table would show it.
    """
    codes = sorted({row.gold.jurisdiction for row in judgements})
    lines = []
    for code in codes:
        rows = [row for row in judgements if row.gold.jurisdiction == code]
        tally = Counter(row.outcome for row in rows)
        positives = sum(1 for row in rows if row.gold.creates_obligation)
        ambiguous = sum(1 for row in rows if row.gold.ambiguous)
        lines.append(
            f"{code}: {len(rows)} passages -- obligations {positives}, hard "
            f"negatives {len(rows) - positives}, ambiguous {ambiguous} | "
            f"wrong {tally[WRONG_ASSERTION]}, missed {tally[MISSED]}, "
            f"correct {tally[CORRECT_ASSERTION]}, refused "
            f"{tally[CORRECT_REFUSAL]}, unadjudicated "
            f"{tally[UNADJUDICATED_ASSERTION]}"
        )
    lines.append(
        "A wrong-assertion count that sits entirely in one state is a fact "
        "about that state's drafting, not about the extractor, and the total "
        "alone would hide it either way round."
    )
    return Observation(title="BY JURISDICTION", lines=tuple(lines))


def _outcome_observation(card: "ObligationScorecard") -> Observation:
    tally = card.counts
    return Observation(
        title="THE FIVE OUTCOMES, COUNTED APART",
        lines=(
            f"WRONG ASSERTION      {tally[WRONG_ASSERTION]:>4}   "
            "catastrophic. A false claim about a regulatory duty. Target zero.",
            f"MISSED               {tally[MISSED]:>4}   "
            "an analyst reads the paragraph by hand. Real cost, survivable.",
            f"CORRECT ASSERTION    {tally[CORRECT_ASSERTION]:>4}   "
            "extracted the duty, and a person adjudicated it the same duty.",
            f"CORRECT REFUSAL      {tally[CORRECT_REFUSAL]:>4}   "
            "declined to assert on a hard negative.",
            f"UNADJUDICATED        {tally[UNADJUDICATED_ASSERTION]:>4}   "
            "asserted on a positive label, nobody has checked which duty. Not "
            "credited, not blamed.",
            NO_COMBINED_SCORE,
        ),
    )


def score_obligations(
    goldsets: tuple[GoldSet, ...],
    extractions: tuple[Extraction, ...],
    *,
    corpus_root: Path | str | None = None,
    run_note: str = "",
) -> ObligationScorecard:
    """Judge every labelled passage, then build the metrics over the judgements.

    Refuses before it scores, in four places, and every one of them is a case
    where a number would have been produced over evidence that cannot carry it:
    no gold set, no labels, a run that is silent about a passage, and a gold set
    with nothing to be wrong about in one direction or the other.
    """
    if not goldsets:
        raise EmptyGoldSet(
            "no gold sets were loaded. Scoring nothing does not produce an "
            "empty scorecard, it produces a green one, so this refuses."
        )
    passages = all_passages(goldsets)
    if not passages:
        raise EmptyGoldSet(
            "the gold set holds no labelled passages. There is nothing to "
            "score. A scorer that answers '100 per cent, 0 of 0' over an empty "
            "set is how a green build lies, so this refuses instead."
        )

    refuse_a_short_run(extractions, passages)
    by_id = {row.passage_id: row for row in extractions}
    judgements = tuple(judge(passage, by_id[passage.id]) for passage in passages)

    root = Path(corpus_root) if corpus_root is not None else DEFAULT_CORPUS_ROOT
    metrics = (
        wrong_assertion_metric(judgements),
        miss_metric(judgements),
        correct_refusal_metric(judgements),
        goldset_offsets_metric(passages, root),
    )
    card = ObligationScorecard(
        goldsets=goldsets,
        judgements=judgements,
        metrics=metrics,
        observations=(),
        run_note=run_note,
    )
    return ObligationScorecard(
        goldsets=goldsets,
        judgements=judgements,
        metrics=metrics,
        observations=(
            _ambiguity_observation(passages),
            _jurisdiction_observation(judgements),
            _outcome_observation(card),
        ),
        run_note=run_note,
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def render(card: ObligationScorecard) -> str:
    """The scorecard as a reviewer reads it. ASCII, 80 columns, no rate."""
    lines: list[str] = [
        _rule("="),
        "STRATA OBLIGATION EXTRACTION SCORECARD",
        _rule("="),
        "Four outcomes with four costs, counted apart. No F1, no accuracy, no",
        "single combined score -- see the refusal printed below the counts.",
        "",
    ]
    if card.run_note:
        lines.append(_bullet("!", card.run_note))
        lines.append("")

    lines.append("Scored against these labelled sets:")
    for goldset in card.goldsets:
        states = ", ".join(goldset.jurisdictions) or "unstated"
        lines.append(
            f"  {goldset.name:<20} {len(goldset.passages):>3} passages  "
            f"[{states}]  {goldset.path}"
        )
    lines.append("")

    # The ambiguous share and the per-jurisdiction table come BEFORE the
    # metrics. A caveat printed after a number is a caveat the reader has
    # already decided not to need.
    for observation in card.observations:
        lines += [_rule(), observation.title, _rule()]
        for line in observation.lines:
            lines.append(_bullet("-", line))
        lines.append("")

    for ordinal, metric in enumerate(card.metrics, start=1):
        lines += [_rule(), _headline(ordinal, metric), _rule()]
        lines.append(_field("Measures", metric.measures))
        lines.append(_field("How", metric.method))
        lines.append(_field("Threshold", metric.threshold))
        lines.append(
            _field(
                "Result",
                count_phrase(
                    metric.hits,
                    metric.total,
                    metric.unit,
                    sample_size=metric.sample_size,
                    sample_unit=metric.sample_unit,
                ),
            )
        )
        lines.append(
            _field("Blocking", "yes" if metric.blocking else "no -- reported only")
        )
        for failure in metric.failures:
            lines.append(_bullet("!", failure))
        for note in metric.notes:
            lines.append(_bullet("-", note))
        if not metric.passed and metric.blocking:
            lines.append("")
            lines.append(
                _bullet(
                    " ",
                    f"{BLOCKER}: {metric.title} did not clear its threshold. "
                    f"Do not ship this. {metric.threshold}",
                )
            )
        lines.append("")

    lines += [_rule(), "CAVEAT -- the labellers' own limits, printed verbatim", _rule()]
    for goldset in card.goldsets:
        lines.append(_bullet("-", f"{goldset.name}:"))
        for limit in goldset.known_limits:
            lines.append(_bullet(" ", limit))
    states = len({row.gold.jurisdiction for row in card.judgements})
    documents = len({row.gold.source_file for row in card.judgements})
    lines.append(
        _bullet(
            "-",
            f"The sample behind the three extraction metrics is {states} "
            f"jurisdictions over {len(card.judgements)} labelled passages. "
            f"Folding by source document instead gives {documents} and by gold "
            f"set gives {len(card.goldsets)}; none of the three is claimed as "
            "an independent sample of regulatory drafting, and the smallest "
            "governs, so not one of those metrics prints a rate.",
        )
    )
    lines.append(
        _bullet(
            "-",
            "The one percentage on this page belongs to the offsets check, "
            "where each source file really is independent evidence that the "
            "bytes have not moved. It says nothing about extraction.",
        )
    )
    lines.append("")

    passed = len(card.metrics) - len(card.failures)
    lines.append(_rule("="))
    lines.append(f"VERDICT: {passed} of {len(card.metrics)} metrics pass.")
    # Named apart, because "1 of 4 pass" beside one blocker reads as three
    # blockers, and a reader who cannot tell which failures stop a release
    # learns to ignore all of them.
    reported = [
        metric.title for metric in card.failures if not metric.blocking
    ]
    if reported:
        lines.append(f"Reported, not blocking: {'; '.join(reported)}.")
    lines.append(f"WRONG ASSERTIONS: {card.wrong_assertions}. Target zero.")
    if card.blocking_failures:
        names = ", ".join(metric.title for metric in card.blocking_failures)
        lines.append(f"{BLOCKER}: {names}.")
        lines.append("Exit 1. This is a gate, not a report.")
    else:
        lines.append("No release blocker. Exit 0.")
        lines.append(
            "A build with no wrong assertions is not a build that works."
        )
        lines.append(
            "Read the miss count and the ambiguous share before quoting this."
        )
    lines.append(_rule("="))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals.obligations",
        description=(
            "Score obligation extraction against the labelled gold sets, by "
            "cost rather than by average."
        ),
    )
    parser.add_argument(
        "--goldsets",
        default=None,
        help="directory holding goldset_*.json (default: ./data/evals)",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="directory holding the source text files (default: ./data/real)",
    )
    parser.add_argument(
        "--predictions",
        default=None,
        help="a run's output; see load_predictions for the shape",
    )
    parser.add_argument(
        "--no-extractor",
        action="store_true",
        help=(
            "score a run in which nothing was asserted. Not a result: it "
            "prints the shape of the scorecard on a build with no extractor."
        ),
    )
    args = parser.parse_args(argv)

    try:
        goldsets = load_goldsets(args.goldsets)
        passages = all_passages(goldsets)
        if args.predictions:
            extractions = load_predictions(args.predictions)
            note = ""
        elif args.no_extractor:
            extractions = no_extractor_run(passages)
            note = NO_EXTRACTOR_ANNOUNCEMENT
        else:
            # NOT a default. A scorecard over a run nobody made would be the
            # emptiest kind of green, so the absence is refused out loud.
            print(
                "Nothing to score: no --predictions file was given. Pass one "
                "(see app/evals/obligations.py::load_predictions for the "
                "shape), or pass --no-extractor to see what this scorecard "
                "does with a system that asserts nothing -- which is a shape, "
                "not a result."
            )
            return 1
        card = score_obligations(
            goldsets, extractions, corpus_root=args.corpus, run_note=note
        )
    except ScoreRefused as refusal:
        print(_rule("="))
        print("STRATA OBLIGATION EXTRACTION SCORECARD -- REFUSED")
        print(_rule("="))
        print("\n".join(_bullet("!", str(refusal)).splitlines()))
        print(
            "\n".join(
                _bullet(
                    " ",
                    "No score was computed. This is the intended answer: a "
                    "number produced over evidence that cannot carry it is "
                    "worse than none.",
                ).splitlines()
            )
        )
        print(_rule("="))
        return 1

    print(render(card))
    return card.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CORRECT_ASSERTION",
    "CORRECT_REFUSAL",
    "DUTY_DIFFERENT",
    "DUTY_SAME",
    "MISSED",
    "NO_COMBINED_SCORE",
    "NO_EXTRACTOR_ANNOUNCEMENT",
    "OUTCOMES",
    "SOURCE_NO_EXTRACTOR",
    "UNADJUDICATED_ASSERTION",
    "WIDTH",
    "WRONG_ASSERTION",
    "EmptyGoldSet",
    "Extraction",
    "GoldPassage",
    "GoldSet",
    "Judged",
    "ObligationScorecard",
    "ScoreRefused",
    "SingleNumberRefused",
    "all_passages",
    "combined_f1",
    "correct_refusal_metric",
    "goldset_offsets_metric",
    "judge",
    "jurisdiction_of",
    "load_goldset",
    "load_goldsets",
    "load_predictions",
    "main",
    "miss_metric",
    "no_extractor_run",
    "normalise_jurisdiction",
    "refuse_a_short_run",
    "render",
    "score_obligations",
    "wrong_assertion_metric",
]
