"""What a number is allowed to say here, and what it is not.

This corpus holds five labelled changes. A recall figure over five items looks
like a measurement and is arithmetic on a sample far too small to carry one:
a single miss moves it twenty points, and the figure survives being quoted
long after the denominator is forgotten. Reporting it would be the exact
failure this repository catches elsewhere -- a claim asserting more than its
evidence supports.

So one module owns the rule, and every metric renders through it. A new metric
cannot forget it.

**The rule.** A rate is printed only when the *independent* sample is ten or
more. Probes are not the sample. Twenty-seven occurrence probes run over nine
spans; three probes against one span do not make three samples, so nine is the
number that governs and no rate is printed. Where a rate is allowed, the counts
and the sample size are printed on the same line, so the rate cannot be quoted
without them.

**The sample is derived, never declared.** A metric hands over the identity of
every row of evidence it gathered -- `Sample` counts the distinct ones. It used
to take the number instead, and the rule then held everywhere except the one
place where breaking it bought a headline: twenty recorded offsets were reported
as twenty samples and printed "100%", though nine of the twenty quote the same
boilerplate sentence. Deflation a call site has to remember is deflation a call
site will forget, and it will forget it in the direction that flatters. So the
number can no longer be passed in.

**Refusing rather than softening.** `rate()` raises when the sample is too
small. It does not return "n/a" or an empty string, because a fallback that
degrades quietly is the failure mode this project was built around
(best-practices.html §26).

**Never rounding up into a claim.** A rate is floored, not rounded. 26 of 27 is
96.2 per cent, and nothing here may print "100%" for anything short of every
one.
"""

import math
from dataclasses import dataclass

# Below this, counts only. Ten is not a magic threshold at which statistics
# begin -- it is the point below which a rate is obviously indefensible, and
# a line has to sit somewhere. Nothing this corpus measures reaches it, so this
# harness prints no rate at all today. That is the honest reading of a corpus
# built by hand around six subjects, and the caveat says so out loud.
MIN_SAMPLE_FOR_RATE = 10


class RateRefused(ValueError):
    """A rate was asked for over a sample that cannot support one."""


@dataclass(frozen=True)
class Sample:
    """The evidence behind a count, and how much of it is independent.

    `identities` holds one entry per row of evidence gathered, repeats kept.
    Two rows carry the same identity when they are the same evidence -- the
    same span asked three questions, the same sentence read in nine places.
    The size is the count of distinct identities, so a metric states what it
    looked at and cannot state how much that was worth.

    Which identity makes two rows the same evidence is a judgement, and each
    metric makes it in the open, in the argument beside its own `Sample`.
    """

    unit: str
    probe_unit: str
    identities: tuple[str, ...]

    def __post_init__(self) -> None:
        # A sample derived from nothing is still nothing. Without this a metric
        # that gathered no evidence reports 0 of 0, and 0 == 0 reads as a pass,
        # so an empty corpus would come back green. Refuse at the point the
        # emptiness is visible rather than let it travel to the scorecard.
        if not self.identities:
            raise ValueError(
                f"a sample of {self.unit} was built from no evidence. A metric "
                f"that gathered nothing has not passed; it has not run."
            )

    @property
    def probes(self) -> int:
        """Rows of evidence gathered."""
        return len(self.identities)

    @property
    def size(self) -> int:
        """Independent items among them. This is the n a rate answers to."""
        return len(set(self.identities))

    @property
    def phrase(self) -> str:
        """The unit, naming the raw count too wherever rows were folded in.

        A reader who sees 'n = 6' should see the 20 it came from in the same
        breath. Twenty offsets verifying is a real result and stays on the page;
        it is just not twenty samples.
        """
        if self.probes == self.size:
            return self.unit
        return f"{self.unit} over {self.probes} {self.probe_unit}"


def rate(hits: int, total: int, sample_size: int) -> str:
    """A percentage, or a refusal. Never a softer answer."""
    if total <= 0:
        raise RateRefused("a rate needs a denominator above zero")
    if sample_size < MIN_SAMPLE_FOR_RATE:
        raise RateRefused(
            f"refusing to print a rate over a sample of {sample_size}; "
            f"{MIN_SAMPLE_FOR_RATE} is the floor. Print the counts instead: "
            f"{hits} of {total}."
        )
    if hits == total:
        return "100%"
    floored = math.floor(1000 * hits / total) / 10
    return f"{floored:.1f}%"


def count_phrase(
    hits: int,
    total: int,
    unit: str,
    *,
    sample_size: int,
    sample_unit: str,
) -> str:
    """The one way a result is written: raw counts, then what backs them.

    `sample_size` is the number of independent items behind the counts, which
    is often smaller than `total`. It is keyword-only because passing it into
    the wrong slot would silently authorise a rate the evidence does not carry.

    Take both keywords from a `Sample` -- `sample.size` and `sample.phrase`.
    Every metric in this harness does, and `Metric` gives no other way. The
    parameters stay plain integers and strings so the renderer does not need to
    know about `Sample`, which means a caller could still hand over a number it
    made up. Nothing here can stop that; what it can do is leave no such caller
    in the tree.
    """
    base = f"{hits} of {total} {unit}"
    if sample_size >= MIN_SAMPLE_FOR_RATE:
        return f"{base}  [{rate(hits, total, sample_size)}, n = {sample_size} {sample_unit}]"
    return (
        f"{base}  [n = {sample_size} {sample_unit}; "
        f"no rate, n < {MIN_SAMPLE_FOR_RATE}]"
    )


CAVEAT = """\
CAVEAT -- read before quoting any number above.

  Sample size. One hand-built corpus: three versions of one invented
  proceeding, five labelled changes, nine spans of one repeated sentence.
  Six subjects in total, and every number above is a reading of one of them.
  The largest independent sample here is 9 spans; change detection rests on 5.
  At that size a single miss moves any rate by twenty points, so this harness
  prints counts and withholds a rate below a sample of ten -- which today means
  it prints no rate at all.

  Twenty offsets, six subjects. The citation metric re-reads 20 recorded
  offsets and all 20 verify. That is a real result and it is not a 100 per cent
  rate over 20 samples: 9 of the 20 quote the same boilerplate sentence, two
  more quote a wording that survived a revision unchanged, and the 20 offsets
  are readings of 6 subjects -- 10 distinct quoted strings at the most generous
  count, which ties the floor rather than clearing it. The harness reports the
  count and refuses the percentage.

  What these numbers support. That the deterministic parts of Strata still do
  what they did yesterday, against a corpus whose answers were computed
  independently of the code. This is a regression gate, and a strict one.

  What they do not support. Any claim of accuracy on real filings. Any
  comparison with another system. Any recall figure -- and precision cannot be
  computed at all, because the manifest labels five changes rather than every
  change, so the corpus carries no negative labels.

  What is not measured. No model runs here and no network call is made, so
  nothing in this scorecard says anything about model behaviour. The model
  path is evaluated separately or not at all; it is not evaluated here, and
  a green scorecard must not be read as though it were.

  Freshness. Every verdict above is bound to the corpus digests printed at the
  top (best-practices.html section 28). Change the corpus and these verdicts
  expire; they do not carry forward."""
