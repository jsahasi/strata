"""Accessibility rules that hold for every template and every stylesheet.

DERIVED, NOT LISTED. Nothing here names a screen. The template checks walk
`app/web/templates/*.html` and the stylesheet checks walk
`app/web/static/*.css`, so a template added next week is held to the same rule
without anybody remembering to add it. A hand-kept inventory of screens is the
thing that rots: it passes for a year and then a new page arrives and the guard
says nothing.

WHAT IS CHECKED, and each one is a defect this repository actually shipped.

  1. 2.1.1 / 4.1.2 -- an element carrying aria-disabled must be reachable by
     keyboard. base.html greys two nav items and states the reason for each,
     which is the right design: nav that appears and vanishes as you move
     cannot be learned. But the reason lived in a title attribute, which needs
     a mouse, a hover and a second of patience, never fires on touch, and is
     invisible to anybody scanning. A real user reported it as a dead link.
     A disabled control that cannot be focused is a control whose reason
     nobody can hear.

  2. 3.3.2 / 4.1.2 -- a form control must carry a name that a machine can
     read. An implicit <label> wrapper is not enough for this guard, and the
     stricter rule is deliberate: a wrapping label names the FIRST labelable
     descendant and silently names nothing when the markup moves, so it fails
     without a symptom. An id with a matching `for`, or an aria-label, is the
     association that survives an edit.

  3. 2.4.11 Focus Not Obscured, new in WCAG 2.2 -- the masthead and the tab
     rail are sticky, so a focus that lands underneath them is a focus the
     reader cannot see. Focusable elements carry scroll-margin-top so the
     browser clears the chrome when it scrolls them into view.

  4. 2.4.7 / 2.4.11 -- the focus ring is defined once, in strata.css, and no
     stylesheet may take it away from :focus-visible. A ring lost to a
     redesign is a regression that no screenshot catches, because the person
     taking the screenshot is holding a mouse.

  5. 2.3.3 / 2.4.7 -- a stylesheet that animates must answer
     prefers-reduced-motion itself, and a stylesheet carrying its own controls
     must draw its own focus ring. strata.css sweeps motion globally, but a
     component sheet that leans on that sweep breaks the day somebody scopes
     it. tour.css already states this rule in its own header; this test holds
     the rest of the sheets to it.

WHAT THIS CANNOT DO. It reads source, not a browser. It cannot tell you a ring
is visible against the surface behind it, cannot measure a rendered contrast
ratio, and cannot tell you whether the words in an accessible name are the
right words. tests/test_glass_contrast.py carries the token arithmetic; the
rest needs eyes.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"
STATIC = ROOT / "app" / "web" / "static"


# ------------------------------------------------------------- template read --

#: Jinja statements, comments and expressions. Stripped before the HTML parser
#: sees the file, because `{% if x %}checked{% endif %}` inside a start tag is
#: not HTML and a parser handed it invents an attribute called `{%`. Statements
#: collapse to a space, which leaves BOTH branches of an `if` in the text --
#: correct for this guard, which wants every control the template can ever
#: render, not the ones one particular context produces.
_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_STATEMENT = re.compile(r"\{%.*?%\}", re.S)
_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)


def _strip_jinja(text: str) -> str:
    """Jinja out, line numbers kept, so a failure names a line you can open."""

    def blank(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    def space(match: re.Match[str]) -> str:
        return " " + "\n" * match.group(0).count("\n")

    def token(match: re.Match[str]) -> str:
        return "x" + "\n" * match.group(0).count("\n")

    text = _COMMENT.sub(blank, text)
    text = _STATEMENT.sub(space, text)
    return _EXPRESSION.sub(token, text)


class _Tags(HTMLParser):
    """Every start tag in a template, with its attributes and its line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[int, str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((self.getpos()[0], tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _templates() -> list[Path]:
    found = sorted(TEMPLATES.glob("*.html"))
    assert found, f"no templates under {TEMPLATES}; the guard would pass on nothing"
    return found


def _tags_of(path: Path) -> list[tuple[int, str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(_strip_jinja(path.read_text()))
    parser.close()
    return parser.tags


# --------------------------------------------------------------- focusable --

#: Elements a browser puts in the tab order without being asked. `a` and `area`
#: only when they carry an href; `input`, `button`, `select` and `textarea`
#: only when they are not disabled.
_NATIVE = {"button", "input", "select", "textarea", "summary", "iframe"}
_NATIVE_WITH_HREF = {"a", "area"}


def _focusable(tag: str, attrs: dict[str, str | None]) -> bool:
    index = attrs.get("tabindex")
    if index is not None:
        try:
            return int(index.strip()) >= 0
        except ValueError:
            return False
    if "disabled" in attrs:
        return False
    if tag in _NATIVE_WITH_HREF:
        return bool(attrs.get("href"))
    return tag in _NATIVE


# ------------------------------------------------ 1. aria-disabled is heard --


def test_every_aria_disabled_element_is_reachable_by_keyboard() -> None:
    """A control that says it is disabled must be able to say why.

    aria-disabled means "here, and not actionable" -- which is the whole point
    of drawing it: an item that vanishes instead teaches the reader nothing.
    But the announcement only ever reaches somebody who lands on the element,
    and a <span> with no tabindex is a span nobody lands on. Keyboard and
    screen-reader readers were shown a grey word and no reason at all.
    """
    offenders = []
    for path in _templates():
        for line, tag, attrs in _tags_of(path):
            state = (attrs.get("aria-disabled") or "").strip().lower()
            if state != "true":
                continue
            if not _focusable(tag, attrs):
                offenders.append(f"{path.name}:{line} <{tag}>")

    assert not offenders, (
        "aria-disabled on an element no keyboard can reach. The reason it "
        "carries is announced on focus and nowhere else, so it is announced "
        'to nobody. Add tabindex="0": ' + ", ".join(offenders)
    )


def test_a_disabled_nav_item_states_its_reason_in_text_not_in_a_title() -> None:
    """The regression guard for the defect a user reported as a dead link.

    A title attribute needs a mouse, a hover and roughly a second of holding
    still. It never fires on touch, most screen readers do not speak it by
    default, and nobody scanning the page sees it. The two nav items that need
    a proceeding or a change in hand carry their reason as real text now, shown
    on hover AND on focus, and the text is inside the element so it is part of
    the accessible name rather than a description that may be dropped.
    """
    source = (TEMPLATES / "base.html").read_text()

    assert "nav__link--off" in source, "the greyed nav item is gone; rewrite this guard"
    assert 'title="{{ hint }}"' not in source, (
        "the reason for a greyed nav item is back in a title attribute. "
        "A title needs a mouse and a hover; this product's rule is that a "
        "thing which cannot be shown names its reason where anybody can read it."
    )
    assert "{{ hint }}" in source, "the hint is no longer rendered at all"


# --------------------------------------------------- 2. every control named --

#: Controls this guard has looked at and does not require to change, each with
#: the reason. Keyed by (template, name attribute) rather than by line, because
#: line numbers rot on the next edit. NEVER a bare skip: an entry with no
#: reason fails the test below.
UNNAMED_ALLOWED: dict[tuple[str, str], str] = {
    ("permissions.html", "codes"): (
        "Each permission checkbox sits inside its own <label> whose text is the "
        "permission code, so the control does have an accessible name and a "
        "screen reader announces it. What it lacks is an id, and the id would "
        "have to be unique across a loop that runs once per role and once per "
        "code -- a key this template does not carry today. permissions.html is "
        "another owner's file. The rule stands, the exception is written down."
    ),
}


def _unnamed_controls() -> list[tuple[str, str, int, str]]:
    """Every control that names itself to no machine: (file, name, line, tag)."""
    found = []
    for path in _templates():
        for line, tag, attrs in _tags_of(path):
            if tag not in {"input", "select", "textarea"}:
                continue
            if (attrs.get("type") or "").strip().lower() == "hidden":
                continue
            if any(key in attrs for key in ("id", "aria-label", "aria-labelledby")):
                continue
            found.append((path.name, (attrs.get("name") or "").strip(), line, tag))
    return found


def test_every_form_control_carries_a_name_a_machine_can_read() -> None:
    """3.3.2 and 4.1.2. A field nothing can name is a field nothing can ask for.

    An id with a matching `for`, an aria-label, or an aria-labelledby. A
    wrapping <label> is not accepted here on purpose: it names the first
    labelable descendant, so it names the wrong control the moment a second one
    is added and names nothing the moment the input moves out. It fails without
    a symptom, which is the worst kind of accessibility bug.
    """
    offenders = [
        f"{name}:{line} <{tag} name={field or '(none)'}>"
        for name, field, line, tag in _unnamed_controls()
        if (name, field) not in UNNAMED_ALLOWED
    ]

    assert not offenders, (
        "form controls with no id, no aria-label and no aria-labelledby. "
        "Nothing can name them to a screen reader, to voice control, or in an "
        "error message. Fix them, or add them to UNNAMED_ALLOWED with the "
        "reason: " + ", ".join(offenders)
    )


def test_every_allowance_is_still_live_and_still_argued() -> None:
    """An allowance with no reason is a skip, and a stale one is a lie.

    Both halves matter. A bare entry lets the next person wave a real defect
    through; an entry for a control that no longer exists tells a reader the
    rule is broken in a place it is not, and they stop trusting the list.
    """
    live = {(name, field) for name, field, _, _ in _unnamed_controls()}

    thin = [f"{key}" for key, why in UNNAMED_ALLOWED.items() if len(why.strip()) < 60]
    assert not thin, (
        "UNNAMED_ALLOWED entries with no argument behind them. Write the reason "
        "or fix the control; a bare skip is how a guard stops guarding: " + ", ".join(thin)
    )

    stale = [f"{key}" for key in UNNAMED_ALLOWED if key not in live]
    assert not stale, (
        "UNNAMED_ALLOWED names controls that are already named or already gone. "
        "Delete the entry: " + ", ".join(stale)
    )


# ------------------------------------------ 3. focus is not under the chrome --


def test_focusable_elements_clear_the_sticky_chrome() -> None:
    """2.4.11 Focus Not Obscured, and it is new in WCAG 2.2.

    The masthead sticks at 7rem and the tab rail sticks under it. Tab down a
    long page and the browser scrolls the next control to the top of the
    viewport -- which is behind both of them. The reader hears nothing, sees
    nothing move, and concludes the tab order is broken.
    """
    css = (STATIC / "strata.css").read_text()

    assert "--focus-anchor" in css, (
        "--focus-anchor is gone. It is the clearance a focused element keeps "
        "from the sticky chrome, and a scroll container lowers it for its own "
        "descendants."
    )

    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    wearing = [
        selector.strip()
        for selector, body in rules
        if "scroll-margin-top" in body and "var(--focus-anchor)" in body
    ]
    covered = [s for s in wearing if all(t in s for t in ("a,", "button,", "input,"))]

    assert covered, (
        "no rule gives links, buttons and inputs a scroll-margin-top of "
        "var(--focus-anchor). Without it a tab stop lands under the masthead."
    )


# ----------------------------------------------- 4. nothing kills the ring --

_OUTLINE_OFF = re.compile(r"outline\s*:\s*(none|0)\b", re.I)


def test_no_stylesheet_takes_the_focus_ring_away() -> None:
    """The ring is declared once, in strata.css, and stays declared.

    One exception is allowed and it has to be spelled out in the selector:
    `:focus:not(:focus-visible)`. That is the browser's own judgement that this
    focus came from a click rather than a key, and dropping the ring there is
    what every design system means when it writes outline:none. Anything
    broader takes the ring off a keyboard reader and no screenshot catches it,
    because the person taking screenshots is holding a mouse.
    """
    offenders = []
    for sheet in sorted(STATIC.glob("*.css")):
        text = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            if not _OUTLINE_OFF.search(body):
                continue
            if ":not(:focus-visible)" in selector:
                continue
            offenders.append(f"{sheet.name}: {selector.strip()}")

    assert not offenders, (
        "a rule removes the focus outline outside the one allowed case. "
        "Scope it to :focus:not(:focus-visible) or delete it: " + ", ".join(offenders)
    )


# ------------------------------- 5. a component sheet carries its own rules --

#: Sheets that draw controls of their own. Each has to draw the focus ring for
#: them and each has to answer prefers-reduced-motion for its own movement,
#: rather than leaning on the sweep at the foot of strata.css. Derived from the
#: directory, so a sheet added later is held to the same rule.
def _component_sheets() -> list[Path]:
    return [s for s in sorted(STATIC.glob("*.css")) if s.name != "strata.css"]


@pytest.mark.parametrize("sheet", _component_sheets(), ids=lambda p: p.name)
def test_a_component_sheet_draws_its_own_focus(sheet: Path) -> None:
    """The tour is the first thing a new reader meets, and it had no ring."""
    assert ":focus-visible" in sheet.read_text(), (
        f"{sheet.name} styles controls and never mentions :focus-visible. "
        "Inheriting the ring is fine until this sheet changes the surface "
        "under it, and then the ring is gone and nothing says so."
    )


@pytest.mark.parametrize("sheet", _component_sheets(), ids=lambda p: p.name)
def test_a_sheet_that_moves_answers_the_reader_who_asked_it_not_to(sheet: Path) -> None:
    """strata.css sweeps motion globally. A sheet must not depend on that.

    The sweep is one `*` rule at the foot of one file. Scope it, split the
    bundle, or load this sheet on a page that does not carry strata.css, and
    every animation here comes back for a reader who asked against movement --
    with nothing in this file saying it ever answered them.
    """
    text = sheet.read_text()
    moves = "animation:" in text or "transition:" in text
    if not moves:
        pytest.skip(f"{sheet.name} declares no motion")

    assert "prefers-reduced-motion" in text, (
        f"{sheet.name} animates and never mentions prefers-reduced-motion. "
        "tour.css states the rule in its own header: this file does not rely "
        "on the sweep in strata.css being there."
    )
