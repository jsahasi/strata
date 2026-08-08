"""Guards for the visual system: the font is ours, the record stays flat.

Both rules are cheap to break by accident and expensive to notice late. A CDN
link is one paste away, and this project has already had to correct a
sub-processor page that had gone untrue. A gradient behind evidence is the
same class of error in a different medium.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_FONT = ROOT / "app/web/static/fonts/manrope-latin.woff2"
SITE_FONT = ROOT / "deploy/site/fonts/manrope-latin.woff2"

CDN_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "use.typekit.net",
    "cdn.jsdelivr.net",
    "unpkg.com",
)

SURFACES = list((ROOT / "app/web/templates").glob("*.html")) + list(
    (ROOT / "deploy/site").glob("*.html")
) + [ROOT / "app/web/static/strata.css", ROOT / "deploy/site/site.css"]


@pytest.mark.parametrize("font", [APP_FONT, SITE_FONT])
def test_the_display_face_is_in_the_repository(font):
    assert font.exists(), f"{font} is missing; the design depends on it"
    assert font.read_bytes()[:4] == b"wOF2", f"{font} is not a woff2"


@pytest.mark.parametrize("host", CDN_HOSTS)
def test_no_surface_reaches_a_font_cdn_at_runtime(host):
    """A reviewer with no network must get the whole design, not a fallback."""
    offenders = [
        path.relative_to(ROOT)
        for path in SURFACES
        if path.exists() and host in path.read_text()
    ]
    assert not offenders, (
        f"{host} is referenced by {offenders}. The font ships in the "
        "repository; a CDN adds a third party to every page load and breaks "
        "an offline run."
    )


# The four record surfaces named in the redesign spec (the diff, the claims,
# the quoted source, the citation viewer), widened by grepping strata.css and
# app/web/templates for the class names those surfaces actually render under.
# ".quote" is the blockquote that carries the quoted-source text itself
# (app/web/templates/change.html:93,150,261 and elsewhere); ".mismatch" is the
# "what the source says" / "what the citation quoted" pair that only a
# withheld claim carries (strata.css:1934). Neither was in the starting list
# and both are evidence, not chrome. ".passage" matches no class in this
# stylesheet today; it stays in because narrowing a guard is not this task's
# job and a passage-scoped class is a plausible name for a later surface.
RECORD_SELECTORS = (
    ".diff",
    ".claim",
    ".source",
    ".citation",
    ".passage",
    ".quote",
    ".mismatch",
)

# Two selectors that trip the substring+gradient scan below and are not
# drift: they are named, documented exceptions read straight out of
# docs/web-design.html's "signature" section and strata.css's own comments.
#
# .claim--withheld carries "the broken amber rule" -- a repeating-linear-
# gradient clipped to a 3px left-edge strip (background-size: 3px 100%) that
# draws a dashed line where a verified claim draws a solid one. docs/
# web-design.html ("The signature") lists it among the properties "kept
# exactly as they are" on the one surface the redesign deliberately gives no
# effects to. It is a line, not a wash: nothing sits behind the claim's own
# text, which stays on flat --surface-sunk.
#
# .source--internal::before is the matching device for provenance: a doubled
# hairline (also clipped to a 3px strip) marking a source as the company's
# own account rather than the public filing, "not a dashed or broken one, so
# it never collides with provisional or withheld" (strata.css, the sources
# section). Same shape, same reason: an accent rail, not a background behind
# read evidence.
#
# Both predate this guard. THE EXEMPTION IS CONDITIONAL, AND THE CONDITION IS
# CHECKED. Naming a selector and waving it through would mean a full-bleed wash
# could land on .claim--withheld tomorrow and this guard would stay silent. A
# rule stated in a comment that nothing enforces is the shape of drift this
# whole file exists to catch, so each exception carries the proof that it is
# still a narrow strip, and the exemption holds only while that proof is there.
#
# Each entry is (where the proof lives, the proof). "block" means the clamp is
# in the exempted rule itself; ("rule", selector) means it is inherited from a
# named sibling, and the proof is looked for in THAT rule and nowhere else.
# .claim--withheld clamps its own gradient with `background-size: 3px 100%`;
# .source--internal::before takes its 3px from the shared `.source::before`.
#
# The scope matters and getting it wrong silently disarms this. A first version
# looked for the proof anywhere in the stylesheet, and `background-size: 3px
# 100%` occurs five times, so deleting the clamp from .claim--withheld still
# found it in four unrelated rules and the exemption held. The guard passed a
# mutation it was written to catch.
KNOWN_GRADIENT_EXCEPTIONS = {
    ".claim--withheld": ("block", "background-size: 3px 100%"),
    ".source--internal::before": (("rule", ".source::before"), "width: 3px"),
}


def _declaration_blocks(css):
    """Split a stylesheet into one chunk per rule, selector included.

    The obvious `css.split("}")` breaks the moment a comment contains a brace
    -- and this file's comments do: `.skip:focus { left: var(--s4) }` and
    `* { transition: ... }` both appear as prose inside /* */ blocks. Splitting
    on the raw text desyncs every block boundary after the first stray brace,
    which can hand a later rule's declarations to an earlier rule's selector
    or vice versa -- a guard built on that is checking rules it never
    correctly identified. Comments carry no CSS this test cares about, so they
    are stripped first; what is left nests only through @media/@supports,
    where the selector text before the first "{" still contains the inner
    selector, so a plain split on "}" is safe.
    """
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return without_comments.split("}")


def test_no_record_surface_carries_a_gradient():
    """Evidence is shown on paper, not on a painted background.

    The whole argument of this product is that a claim traces to the words
    behind it. A gradient behind those words is decoration applied to the one
    surface that must not look decorated. Chrome may be soft; the record is
    flat.
    """
    css = (ROOT / "app/web/static/strata.css").read_text()
    offenders = []
    for block in _declaration_blocks(css):
        if "gradient" not in block:
            continue
        selector = block.split("{")[0]
        exempt = False
        for known, (scope, proof) in KNOWN_GRADIENT_EXCEPTIONS.items():
            if known not in selector:
                continue
            # Still a strip? Then the documented exception holds. If the clamp
            # has gone, the rule has grown into the wash the exception was
            # never written to cover, and it falls through to the check below.
            if scope == "block":
                exempt = proof in block
            else:
                _, owner = scope
                exempt = any(
                    proof in other
                    for other in _declaration_blocks(css)
                    if other.split("{")[0].strip() == owner
                )
            break
        if exempt:
            continue
        for name in RECORD_SELECTORS:
            if name in selector:
                offenders.append(selector.strip()[:60])
    assert not offenders, (
        f"gradient on a record surface: {offenders}. Chrome is soft, the "
        "record is flat -- see the visual redesign spec, section 5. If one of "
        "these is a documented left-edge rail rather than a wash, it needs an "
        "entry in KNOWN_GRADIENT_EXCEPTIONS carrying the clamp that proves it "
        "is still a strip."
    )


# ------------------------------------------------- a shadow that composes --

COMPOSING_SHADOW_TOKENS = ("--lift-1", "--lift-2")


def test_a_composing_shadow_token_is_never_the_word_none():
    """`box-shadow: none, <ring>` is invalid, and the browser drops the ring.

    --lift-1 and --lift-2 are written to sit in a comma-separated box-shadow
    beside something else. clerk.css draws a focused pill as
    `box-shadow: var(--lift-1), 0 0 0 4px var(--focus-halo)`. Set the token to
    `none` in any scheme and that value becomes `none, 0 0 0 4px ...`, which is
    not legal box-shadow syntax -- so the whole declaration is discarded and
    the focus ring goes with it, on the one medium nobody re-checks.

    It shipped that way in @media print. Nothing failed, because the dock is
    hidden on paper today; the next rule to combine a lift with a ring on a
    printable surface would have lost its ring silently.

    `0 0 transparent` paints exactly what `none` paints and stays legal in a
    list. Any scheme may switch the lift off; none may switch it off this way.
    """
    css = (ROOT / "app/web/static/strata.css").read_text()
    offenders = []
    for token in COMPOSING_SHADOW_TOKENS:
        for match in re.finditer(rf"{re.escape(token)}\s*:\s*([^;]+);", css):
            if match.group(1).strip() == "none":
                offenders.append(f"{token}: none")
    assert not offenders, (
        f"{offenders} -- these tokens compose inside a box-shadow list, so "
        "`none` invalidates the whole declaration and drops any ring beside "
        "them. Use `0 0 transparent`, which paints the same nothing."
    )
