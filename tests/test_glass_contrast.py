"""The colour tokens, held to the figures the stylesheet claims for them.

Every number in this file exists because a comment in strata.css asserted it and
nothing checked it. That has now gone wrong four separate times in one token
block, always the same way: somebody computed a ratio against a flat token,
wrote it down as a rendered measurement, and the next reader inherited it as
checked fact. strata.css says so itself, twice, in the words "Nothing asserts
either figure in a test, so nothing would have caught it."

This is that test. It is offline, needs no browser, and runs in milliseconds,
which is the point -- a guard nobody can afford to run is not a guard.

WHAT IT CANNOT DO, stated so the next person does not trust it further than it
goes. It reads DECLARED tokens and computes WCAG ratios between them. The
masthead and the tab rail are translucent panes over a blurred backdrop, and
their rendered colour depends on whatever is scrolling underneath at the time.
No arithmetic here reaches that. Where a ratio involves --glass-solid it is a
ceiling and is labelled one. Rendered figures need Chromium and belong in a
screenshot pass, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
CSS = STATIC / "strata.css"


# ------------------------------------------------------------------ colour --


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.2 relative luminance. sRGB, D65, no colour management."""

    def channel(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """Contrast ratio between two hex colours, per WCAG 2.2."""
    la, lb = _relative_luminance(_hex_to_rgb(a)), _relative_luminance(_hex_to_rgb(b))
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ------------------------------------------------------------ reading them --
#
# A real CSS parser is not worth a dependency for this. What is needed is "the
# value this token has inside this block", and the blocks that matter are all
# `:root { ... }` inside a known @media condition. Anything this cannot find
# raises rather than defaulting, because a token that silently reads as None
# would make every assertion below pass for the wrong reason.

TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.MULTILINE)


def _root_blocks(css: str) -> list[tuple[str, str]]:
    """Every `:root {...}` body in the file, paired with the @media above it.

    Returns (condition, body). The condition is "" for the top-level block.
    Nesting in this stylesheet is at most one @media deep, which the parse
    below assumes and the guard test asserts.
    """
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r":root\s*\{", css):
        start = match.end()
        depth = 1
        i = start
        while depth and i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        body = css[start : i - 1]

        # Walk backwards to the @media this :root sits inside, if any.
        head = css[: match.start()]
        condition = ""
        depth = 0
        for j in range(len(head) - 1, -1, -1):
            if head[j] == "}":
                depth += 1
            elif head[j] == "{":
                if depth == 0:
                    at = head.rfind("@media", 0, j)
                    if at != -1 and "}" not in head[at:j]:
                        condition = " ".join(head[at:j].split())
                    break
                depth -= 1
        blocks.append((condition, body))
    return blocks


def _tokens_for(css: str, condition: str) -> dict[str, str]:
    for cond, body in _root_blocks(css):
        if cond == condition:
            return {m.group(1): m.group(2).strip() for m in TOKEN.finditer(body)}
    raise AssertionError(f"no :root block found for condition {condition!r}")


def _resolved(css: str, *conditions: str) -> dict[str, str]:
    """Base tokens, then each condition's overrides layered on in order."""
    out = _tokens_for(css, "")
    for condition in conditions:
        out.update(_tokens_for(css, condition))
    return out


LIGHT = ""
DARK = "@media (prefers-color-scheme: dark)"
HC_LIGHT = "@media (prefers-contrast: more) and (prefers-color-scheme: light)"
HC_DARK = "@media (prefers-contrast: more) and (prefers-color-scheme: dark)"


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schemes(css: str) -> dict[str, dict[str, str]]:
    return {
        "light": _resolved(css, LIGHT),
        "dark": _resolved(css, DARK),
        "hc-light": _resolved(css, HC_LIGHT),
        "hc-dark": _resolved(css, DARK, HC_DARK),
    }


def test_the_parser_found_the_blocks_it_claims_to_have_found(schemes):
    """If this fails, every other test in the file is passing on empty dicts."""
    for name, tokens in schemes.items():
        assert tokens.get("--ink"), f"{name}: no --ink"
        assert tokens.get("--ink-2"), f"{name}: no --ink-2"
        assert tokens.get("--ink-3"), f"{name}: no --ink-3"
        assert tokens.get("--bg"), f"{name}: no --bg"
    # The high-contrast blocks must actually override, not inherit silently.
    assert schemes["hc-light"]["--ink-3"] != schemes["light"]["--ink-3"]
    assert schemes["hc-dark"]["--ink-3"] != schemes["dark"]["--ink-3"]


# --------------------------------------------------- the collapse this guards --


@pytest.mark.parametrize("scheme", ["light", "dark", "hc-light", "hc-dark"])
def test_the_two_ink_levels_are_never_the_same_colour(schemes, scheme):
    """--ink-3 must never equal --ink-2. It shipped equal once, in both schemes.

    The `prefers-contrast: more` block raised --ink-3 to a value that was
    byte-identical to --ink-2 -- #414d52 in light, #a9b7ba in dark. --ink-3 is
    the second level of a deliberate two-level ink hierarchy at 36 sites in this
    stylesheet, 6 in clerk.css and 7 in workflow.css. Setting the two equal
    flattens every one of them to one level, for the single reader who asked to
    be able to see MORE.

    The worst case is .nav__link--off, which is a <span aria-disabled>, not an
    <a>. The only thing on screen saying that item leads nowhere is that it is
    drawn in --ink-3 while its neighbours are --ink-2. Equal tokens render a
    dead nav item pixel-identical to a live one -- and strata.css argues at
    length, three hundred lines above the rule that broke it, that this exact
    lie is the one never to tell.
    """
    tokens = schemes[scheme]
    assert tokens["--ink-3"].lower() != tokens["--ink-2"].lower(), (
        f"{scheme}: --ink-3 and --ink-2 are both {tokens['--ink-2']}. "
        "The 'this is a shade, not a value' signal is gone, and with it the "
        "only cue that .nav__link--off is not a link."
    )
    separation = contrast(tokens["--ink-3"], tokens["--ink-2"])
    assert separation >= 1.3, (
        f"{scheme}: only {separation:.3f}:1 between --ink-3 and --ink-2. "
        "Two ink levels a reader cannot tell apart are one ink level."
    )


@pytest.mark.parametrize("scheme", ["light", "dark", "hc-light", "hc-dark"])
def test_the_two_rule_levels_are_never_the_same_colour(schemes, scheme):
    """--rule must never equal --rule-strong. Same bug, same block, same fix.

    Found by fixing the class rather than the line: the high-contrast block set
    --rule to #a4afb3 and #3c4e55, which are --rule-strong exactly. Borders
    carry their own two-level hierarchy and it collapsed the same way.
    """
    tokens = schemes[scheme]
    assert tokens["--rule"].lower() != tokens["--rule-strong"].lower(), (
        f"{scheme}: --rule and --rule-strong are both {tokens['--rule']}"
    )


# ------------------------------------------------------- the pinned figures --
#
# Ratios strata.css states in prose. Pinned to two decimals so that moving a
# colour token without revisiting the comment that describes it fails here
# rather than in front of a reviewer. To change one of these, change the comment
# in the same edit -- that is the whole mechanism.

PINNED = [
    # scheme,     ink,       ground,           ratio, where strata.css says it
    ("light", "--ink", "--bg", 15.82, "token block"),
    ("light", "--ink-2", "--bg", 7.48, "prefers-contrast block"),
    ("light", "--ink-3", "--bg", 4.27, "prefers-contrast block, conceded under AA"),
    ("light", "--ink-2", "--surface-sunk", 6.82, "prefers-contrast block"),
    ("light", "--ink-3", "--surface-sunk", 3.89, "conceded under AA"),
    ("light", "--ink", "--glass-solid", 16.96, "token block, CEILING"),
    ("light", "--ink-2", "--glass-solid", 8.01, "masthead block, CEILING"),
    ("light", "--ink-3", "--glass-solid", 4.57, "masthead block, CEILING"),
    ("dark", "--ink-3", "--bg", 5.69, "prefers-contrast block"),
    ("dark", "--ink-3", "--surface", 5.15, "prefers-contrast block"),
    ("dark", "--ink-3", "--surface-sunk", 5.90, "prefers-contrast block"),
    ("hc-light", "--ink-3", "--bg", 6.80, "prefers-contrast block"),
    ("hc-light", "--ink-3", "--surface-sunk", 6.20, "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--bg", 8.20, "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--surface", 7.42, "prefers-contrast block"),
]


@pytest.mark.parametrize("scheme,ink,ground,expected,where", PINNED)
def test_the_ratios_the_stylesheet_writes_down_are_the_ratios_it_has(
    schemes, scheme, ink, ground, expected, where
):
    tokens = schemes[scheme]
    actual = contrast(tokens[ink], tokens[ground])
    assert actual == pytest.approx(expected, abs=0.01), (
        f"{scheme}: {ink} on {ground} is {actual:.2f}:1, but strata.css "
        f"({where}) says {expected}:1. Fix the colour or fix the comment, in "
        "this same edit."
    )


def test_asking_for_more_contrast_actually_gets_more_contrast(schemes):
    """The high-contrast ink must beat the base ink on every ground it lands on.

    Obvious, and worth asserting anyway: the first version of this block did
    raise the contrast, and destroyed the hierarchy doing it. This test and the
    collapse test above have to pass together, which is the real constraint.
    """
    for base, high in (("light", "hc-light"), ("dark", "hc-dark")):
        for ground in ("--bg", "--surface", "--surface-sunk"):
            was = contrast(schemes[base]["--ink-3"], schemes[base][ground])
            now = contrast(schemes[high]["--ink-3"], schemes[high][ground])
            assert now > was, f"{high}: --ink-3 on {ground} went {was:.2f} -> {now:.2f}"


def test_the_high_contrast_light_ink_clears_aa_on_every_opaque_ground(schemes):
    """Light --ink-3 is under AA on --bg and --surface-sunk. Here it must not be.

    This is what the query is FOR. A reader who asks for more contrast and is
    handed something still under 4.5:1 has been gestured at, not answered.
    Opaque grounds only: the panes are translucent and no arithmetic here can
    speak for what renders on them.
    """
    tokens = schemes["hc-light"]
    for ground in ("--bg", "--surface", "--surface-sunk"):
        ratio = contrast(tokens["--ink-3"], tokens[ground])
        assert ratio >= 4.5, f"hc-light: --ink-3 on {ground} is only {ratio:.2f}:1"


# ------------------------------------------------------- the stale literal --


def test_the_flat_pane_token_matches_the_translucent_one_it_stands_in_for(css):
    """--glass-solid must be --glass-1 composited over --bg.

    THIS IS THE BUG THAT STARTED ALL OF IT. --glass-tint was raised and the
    literals under it were not, so the declared source and the rendered value
    drifted apart for a day with a comment in between describing the intention
    as an outcome. --glass-solid is not decoration: the @supports fallback and
    `prefers-reduced-transparency: reduce` both paint it INSTEAD of the pane, so
    a reader who cannot have backdrop-filter sees this exact colour. If it drifts
    from the pane again, those readers get a masthead a different colour from
    everyone else's and nothing says so.

    One value of slack per channel, for the rounding in an alpha composite.
    """
    for condition, name in ((LIGHT, "light"), (DARK, "dark")):
        tokens = _resolved(css, condition) if condition else _tokens_for(css, "")
        glass_1 = tokens["--glass-1"]
        numbers = re.findall(r"[\d.]+", glass_1)
        assert len(numbers) == 4, f"{name}: cannot read --glass-1 {glass_1!r}"
        fr, fg, fb, alpha = (float(n) for n in numbers)
        br, bg_, bb = _hex_to_rgb(tokens["--bg"])
        composited = (
            round(fr * alpha + br * (1 - alpha)),
            round(fg * alpha + bg_ * (1 - alpha)),
            round(fb * alpha + bb * (1 - alpha)),
        )
        declared = _hex_to_rgb(tokens["--glass-solid"])
        drift = [abs(a - b) for a, b in zip(composited, declared)]
        assert max(drift) <= 1, (
            f"{name}: --glass-solid is {tokens['--glass-solid']} "
            f"= rgb{declared}, but --glass-1 over --bg composites to "
            f"rgb{composited}. The literal has gone stale against its source "
            "again."
        )


# ------------------------------------------------- the invariant off-shell --


def test_anything_that_lands_on_the_desk_carries_an_opaque_fill(css):
    """.skip renders outside .shell, on the darkened desk, and must stay opaque.

    strata.css argued the desk costs no contrast because "content never renders
    there -- .masthead, .main and .colophon each wrap their children in .shell".
    Those three do. .skip is a sibling of all three and `.skip:focus` puts it
    16px from the window edge, deep in the desk gutter, as the first thing a
    keyboard user reaches. The invariant was stated about the elements somebody
    happened to look at.

    It is safe only because .skip has an opaque background, so the ground never
    shows through it. That is the part worth holding: turn that fill
    transparent, or make it an rgba(), and a focused skip link is small text on
    a surface the stylesheet deliberately darkened.
    """
    match = re.search(r"^\.skip\s*\{(.*?)\}", css, re.DOTALL | re.MULTILINE)
    assert match, ".skip rule not found in strata.css"
    body = match.group(1)
    background = re.search(r"background(?:-color)?\s*:\s*([^;]+);", body)
    assert background, ".skip has no background; the desk now shows through it"
    value = background.group(1).strip()
    assert "rgba" not in value and "transparent" not in value, (
        f".skip background is {value!r}. It renders on the desk, which is "
        "darker than --bg, so its fill has to be opaque."
    )


# ---------------------------------------------------------- the switch-offs --


@pytest.mark.parametrize(
    "condition",
    [
        "@media print",
        "@media (prefers-contrast: more)",
        "@media (prefers-reduced-transparency: reduce)",
    ],
)
def test_the_ground_is_switched_off_where_the_stylesheet_says_it_is(css, condition):
    """--ground: none in all three, or the comment describing them is fiction."""
    tokens = _tokens_for(css, condition)
    assert tokens.get("--ground") == "none", (
        f"{condition}: --ground is {tokens.get('--ground')!r}, expected none"
    )


def test_nothing_in_the_ground_or_the_glass_animates(css):
    """Stated in a comment as a promise. A promise nobody checks is a wish.

    A ground or a pane that drifts, breathes or parallaxes would cost a repaint
    on every scrolled frame and would need its own reduced-motion escape. The
    comment above the reduced-motion block says none of them move; this is what
    makes that true.
    """
    offenders = []
    for token in ("--ground", "--glass-lift", "--edge", "--desk", "--sheet"):
        for match in re.finditer(rf"{token}\s*:\s*([^;]+);", css):
            value = match.group(1)
            if any(word in value for word in ("animation", "transition", "@keyframes")):
                offenders.append((token, value.strip()[:60]))
    assert not offenders, f"movement declared in a backdrop token: {offenders}"
