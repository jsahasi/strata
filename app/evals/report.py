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

**Refusing rather than softening.** `rate()` raises when the sample is too
small. It does not return "n/a" or an empty string, because a fallback that
degrades quietly is the failure mode this project was built around
(best-practices.html §26).

**Never rounding up into a claim.** A rate is floored, not rounded. 26 of 27 is
96.2 per cent, and nothing here may print "100%" for anything short of every
one.
"""

import math

# Below this, counts only. Ten is not a magic threshold at which statistics
# begin -- it is the point below which a rate is obviously indefensible, and
# a line has to sit somewhere. Everything this harness measures is under it
# except the offset count, which is deliberate: the caveat says so out loud.
MIN_SAMPLE_FOR_RATE = 10


class RateRefused(ValueError):
    """A rate was asked for over a sample that cannot support one."""


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
  proceeding, five labelled changes, nine spans of one repeated sentence. The
  largest independent sample here is 20 recorded offsets; change detection
  rests on 5. At that size a single miss moves any rate by twenty points, so
  this harness prints counts and withholds a rate below a sample of ten.

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
