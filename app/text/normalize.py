"""The single normalization used on both sides of every citation comparison.

Two rules govern what belongs here. Fold a difference only when a regulator, a
PDF extractor or a word processor could have introduced it without anyone
intending a change of meaning. Never fold a difference that could carry
meaning: digits, units, dates and case are all left alone, because "20 MW" and
"10 MW" are the whole point.

WHAT IS FOLDED, EXACTLY. The docstring used to promise that digits are never
folded while the code applied NFKC to everything, which folded "20" followed by
a superscript two to "202", and the vulgar fraction one-half to "1/2" -- a
footnote marker or a tariff fraction changing value inside the function whose
job is to preserve value. NFKC is now applied selectively:

  folded      compatibility forms that cannot carry a number's value --
              ligatures, the no-break space, full-width and half-width forms,
              squared unit glyphs, and canonical composition, so a letter
              written as base plus combining accent equals the single
              character a word processor would have produced.
  not folded  characters tagged <super>, <sub> or <fraction>, and any
              character whose folding begins with a decimal digit. The tags
              cover position against a number: a superscript two is a footnote
              marker, not a digit of the number beside it, and U+00BD is a
              half, not the three characters 1, fraction slash and 2. The
              digit test covers the rest by what folding does, not by which
              tag a character happens to carry.

              The tag list alone was wrong, and said so in a sentence calling
              those three "the three tags where folding rewrites a number". A
              circled digit is a footnote and enumeration marker in exactly
              the way a superscript is, but it is tagged <circle>, so "20" and
              a circled one folded to "201". A sweep of the code space found
              150 such characters the tags missed: 99 tagged <compat>, of
              which U+2488 DIGIT ONE FULL STOP is the plainest, and 51 tagged
              <circle>. Testing the folded digit catches all 150 without
              touching the <compat> forms that carry no number, so U+3392
              still folds to MHz.

              One exception: a character that is itself a decimal digit
              (category Nd). A full-width two and a mathematical bold two are
              the digit two in another face, so folding keeps the value they
              already carried and "２０ MW" still matches "20 MW". Protecting
              them would send a styling difference to review, and would also
              contradict the full-width folding listed above, since Unicode
              calls both Nd. This is a judgement, not a proof: a bold digit
              used as a marker beside a plain number would fold wrongly. I
              have not seen one in a filing, and I do not know of a way to
              tell the two uses apart from the characters alone.

              The digit test guards the left edge only, where a marker sits.
              Twelve <square> unit glyphs fold to something ending in a digit
              -- U+33A0 to "cm2" -- so a number to their right could close up.
              Those are the squared units folded on purpose, and "cm2" run
              against a following number is not text a filing contains.

  deleted     the soft hyphen U+00AD, unconditionally.
  rewritten   a hyphen or dash immediately followed by a line break, with any
              whitespace after it, becomes a bare hyphen. A line break here is
              any of LF, CR, line tabulation, form feed, next line, U+2028 and
              U+2029 -- not just CR and LF, which is what the set held while
              this paragraph already said "a line break". The form feed is the
              one that mattered: pdftotext writes it at a page boundary, so a
              word broken across a page is the hyphenated word a paginated
              filing is likeliest to contain, and it was the one case that
              failed.

PDF REALITY. Extracted PDF text carries soft hyphens and hyphenated line
breaks, and both defeat exact matching. The hyphen at a line break is kept, not
removed: deciding whether "demon-\\nstrate" was a hyphenated word break or a
genuine hyphen is a guess, and this module exists so that nothing guesses. So
"cost-\\ncausation" normalizes to "cost-causation" and "demon-\\nstrate"
normalizes to "demon-strate" -- the second will not match "demonstrate", which
sends the citation to review rather than asserting a joined word nobody wrote.

A LIMIT WORTH STATING. A soft hyphen sitting at a line break -- "main<AD>\n
tain", which some extractors emit where others emit "main-\ntain" -- is deleted
like any other, and the line break then collapses to a space, giving "main
tain". That is the wrong text. It is also the safe wrong text: it matches
nothing, so the citation fails and goes to review. Consuming the break instead
would produce "maintain", which is very probably right and is still a guess,
and a guess that fails open rather than closed. ADR-003 fails toward review, so
this stays as it is, documented and pinned by a test rather than left to be
rediscovered.

THE PROJECTION. normalize() returns only the text. normalized_projection()
returns the same text plus a map from each normalized character back to the raw
characters that produced it, which is what lets a search run in normalized
space and report raw offsets. There is one implementation: normalize() is the
projection's text, so the two cannot drift into disagreeing about what a
normalized document looks like.
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

SOFT_HYPHEN = "\u00ad"  # written as an escape: the character itself is invisible

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}

_DASHES = {
    "‐": "-", "‑": "-", "‒": "-",
    "–": "-", "—": "-", "―": "-", "−": "-",
}

# Compatibility decompositions that rewrite a number rather than a glyph.
# Folding these is what turned "20<super>2</super>" into "202".
_VALUE_BEARING_TAGS = ("<super>", "<sub>", "<fraction>")

# Classification by the same expression that used to do the collapsing, so the
# set of characters treated as whitespace cannot drift from `\s+`.
_ONE_SPACE = re.compile(r"\s")

# Every character that ends a line, not just the two an ASCII text file uses.
# The form feed is the one that mattered: pdftotext writes it at a page break,
# so it is the break a paginated filing is guaranteed to contain. Each of these
# is also whitespace under _ONE_SPACE, which it must be -- the hyphen rule
# consumes a break only when a hyphen precedes it, and every other position
# falls through to the whitespace path. A test pins that both ways.
# Written as escapes for the same reason as the soft hyphen: on the page these
# characters are invisible, or would break the line they are written on.
_LINE_BREAKS = (
    "\n"        # line feed
    "\r"        # carriage return
    "\v"        # line tabulation U+000B
    "\f"        # form feed U+000C, the page break
    "\u0085"    # next line
    "\u2028"    # line separator
    "\u2029"    # paragraph separator
)


def _is_space(char: str) -> bool:
    return _ONE_SPACE.match(char) is not None


# The answer depends only on the character and the Unicode tables, and a
# document draws on a few hundred distinct characters, so it is worth keeping.
# This runs on both sides of every citation comparison, over whole filings.
@lru_cache(maxsize=4096)
def _carries_value(char: str) -> bool:
    """True when folding this character would rewrite a number.

    Two ways a character qualifies. It carries a tag whose whole point is
    position against a number -- superscript, subscript, fraction. Or its
    folding begins with a decimal digit, so the digit would close up against a
    number to its left: "20" and a circled one are a quantity and a footnote
    marker, and NFKC alone turns them into "201".

    The exception is a character that is already a decimal digit. A full-width
    two and a mathematical bold two are the digit two wearing another face, and
    folding them keeps the value they carried, so they fold.
    """
    if unicodedata.decomposition(char).startswith(_VALUE_BEARING_TAGS):
        return True
    if unicodedata.category(char) == "Nd":
        return False
    folded = unicodedata.normalize("NFKC", char)
    return bool(folded) and unicodedata.category(folded[0]) == "Nd"


def _fold(chunk: str) -> str:
    """Selective NFKC plus the quote and dash tables, for one base character.

    The chunk is a base character with any combining marks that belong to it,
    so canonical composition still works. A base character whose decomposition
    would rewrite a number is passed through untouched.
    """
    folded = chunk if _carries_value(chunk[0]) else unicodedata.normalize("NFKC", chunk)
    return "".join(_DASHES.get(ch, _QUOTES.get(ch, ch)) for ch in folded)


@dataclass(frozen=True)
class Projection:
    """Normalized text, plus where each of its characters came from.

    starts[i] and ends[i] are the raw offsets of the run of source characters
    that produced normalized character i. The run is usually one character. It
    is longer when a ligature expanded, and it is the whole collapsed run when
    a stretch of whitespace became a single space.
    """

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]

    def raw_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a half-open span of the normalized text back to raw offsets."""
        if start < 0 or end > len(self.text) or start >= end:
            raise ValueError(f"not a span of this projection: ({start}, {end})")
        return self.starts[start], self.ends[end - 1]


def normalized_projection(text: str) -> Projection:
    """Normalize once, carrying every character's raw origin with it.

    One left-to-right pass, so it is O(n) in the length of the source. The
    alternative it replaced -- normalizing a fixed-width raw window at every
    offset -- is O(n*m) and, worse, wrong: it assumes normalization preserves
    length, and whitespace collapse does not.
    """
    if text is None:
        raise TypeError("normalize() requires a string, not None")

    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending_space = False
    pending_start = 0
    pending_end = 0

    index = 0
    length = len(text)
    while index < length:
        char = text[index]

        # A soft hyphen is a rendering hint. It is deleted, and it does not
        # count as whitespace, so the words either side of it stay joined.
        if char == SOFT_HYPHEN:
            index += 1
            continue

        # A chunk is one base character with the combining marks that belong to
        # it, so canonical composition still happens. Soft hyphens are absorbed
        # here rather than only at the top of the loop: a soft hyphen sitting
        # between a letter and its accent would otherwise leave the accent
        # stranded as a base of its own, and normalize() would not be
        # idempotent -- a second pass would compose what the first left apart.
        chunk_end = index + 1
        while chunk_end < length and (
            unicodedata.combining(text[chunk_end]) or text[chunk_end] == SOFT_HYPHEN
        ):
            chunk_end += 1

        if chunk_end == index + 1 and char < "\x80":
            folded = char  # ASCII is NFKC-stable and appears in neither table.
        else:
            folded = _fold(text[index:chunk_end].replace(SOFT_HYPHEN, ""))

        ended_on_hyphen = False
        for out_char in folded:
            if _is_space(out_char):
                if not pending_space:
                    pending_space = True
                    pending_start = index
                # The run ends where the chunk that produced this space ends,
                # not at the next chunk's start. The two differ when a chunk
                # folds to a space followed by something else -- an ideographic
                # space carrying a stray combining mark, which bad PDF
                # extraction does emit. Closing the run at the next chunk's
                # start then gave the space an empty raw span, so a reviewer
                # asked to see the cited characters would have been shown none.
                pending_end = chunk_end
                continue
            if out and pending_space:
                out.append(" ")
                starts.append(pending_start)
                ends.append(pending_end)
            pending_space = False
            out.append(out_char)
            starts.append(index)
            ends.append(chunk_end)
            ended_on_hyphen = out_char == "-"

        # Line-break hyphenation. A hyphen against a line break was a line
        # break, not a space; the hyphen stays and the break goes. Checked
        # after folding so every dash variant a PDF can emit is covered, and
        # only when the break touches the hyphen -- "cost - \n causation" keeps
        # its spaces because the dash there is punctuation between words.
        if ended_on_hyphen and chunk_end < length and text[chunk_end] in _LINE_BREAKS:
            after = chunk_end
            while after < length and (_is_space(text[after]) or text[after] == SOFT_HYPHEN):
                after += 1
            index = after
            pending_space = False
            continue

        index = chunk_end

    # Trailing whitespace is never emitted, and leading whitespace finds `out`
    # empty, which is the strip() the collapsing expression used to do.
    return Projection("".join(out), tuple(starts), tuple(ends))


def normalize(text: str) -> str:
    """Fold the differences that must not decide whether a citation verifies."""
    return normalized_projection(text).text
