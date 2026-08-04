"""The single normalization used on both sides of every citation comparison.

Two rules govern what belongs here. Fold a difference only when a regulator,
a PDF extractor or a word processor could have introduced it without anyone
intending a change of meaning. Never fold a difference that could carry
meaning: digits, units, dates and case are all left alone, because "20 MW"
and "10 MW" are the whole point.
"""

import re
import unicodedata

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}

_DASHES = {
    "‐": "-", "‑": "-", "‒": "-",
    "–": "-", "—": "-", "―": "-", "−": "-",
}

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold the differences that must not decide whether a citation verifies."""
    if text is None:
        raise TypeError("normalize() requires a string, not None")

    # NFKC folds ligatures (fi -> fi) and non-breaking space to space.
    folded = unicodedata.normalize("NFKC", text)

    folded = "".join(_QUOTES.get(ch, ch) for ch in folded)
    folded = "".join(_DASHES.get(ch, ch) for ch in folded)

    return _WHITESPACE.sub(" ", folded).strip()
