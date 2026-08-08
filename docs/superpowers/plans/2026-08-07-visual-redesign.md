# Strata Visual Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the visual language of the Strata corporate site and application in an institutional-fintech register, without breaking any of the 2,340 passing tests or the accessibility guarantees they encode.

**Architecture:** The application's 24 templates all extend `base.html` and consume `strata.css`, so a token- and component-level rewrite propagates everywhere for free. Work therefore runs foundations first (ratio tooling, font, palette), then the system, then the six demo-path screens, then the marketing site, then documentation. The marketing site has its own stylesheet (`site.css`) and does not share tokens with the app; the two surfaces are kept in step by hand, deliberately.

**Tech Stack:** Server-rendered Jinja templates, hand-written CSS, no framework, no build step, no CDN. Python 3 + pytest for guards. One self-hosted variable woff2.

**Spec:** `docs/superpowers/specs/2026-08-07-visual-redesign-design.html`

## Global Constraints

These apply to **every** task. They are copied from the spec and from the test suite; none is negotiable.

- **All 2,340 tests stay green.** `make test` passes; `make fresh-check` exits zero.
- **No build step.** No bundler, no preprocessor, no npm. CSS is hand-written and served as-is (ADR-012).
- **No CDN at runtime.** The font is committed to the repository and served from it. A reviewer with no network must get the full design.
- **Four colour schemes must exist and stay distinct:** `light`, `dark`, `hc-light` (`@media (prefers-contrast: more) and (prefers-color-scheme: light)`), `hc-dark`. Dropping dark mode is not available.
- **Every contrast ratio written in a comment must equal the computed ratio to 0.01.** Enforced by `PINNED` in `tests/test_glass_contrast.py`. Never estimate a ratio; always compute it.
- **Pinned tokens must stay plain hex.** `_hex_to_rgb` in the test cannot parse `color-mix()`, `rgba()` or `var()`. Tokens `--bg`, `--surface`, `--surface-sunk`, `--ink`, `--ink-2`, `--ink-3`, `--rule`, `--rule-strong`, `--glass-solid` are hex in every scheme.
- **`contrast(--ink-3, --ink-2) >= 1.3`** in all four schemes; `--rule != --rule-strong` in all four.
- **`hc-light --ink-3` >= 4.5:1** on `--bg`, `--surface` and `--surface-sunk`.
- **Asking for more contrast must get more contrast:** `hc-*` ink-3 ratios strictly exceed their `light`/`dark` counterparts on every ground.
- **`clerk.css` may write no hex and no `rgba()` of its own.** Enforced by `tests/test_chat_surface.py`. It consumes tokens only.
- **Every component sheet declares `:focus-visible`** (`tests/test_a11y_guards.py:347`).
- **Every sheet that moves answers `prefers-reduced-motion`** (`tests/test_a11y_guards.py:357`). Reduced motion shows the end state, never nothing.
- **No stylesheet removes the focus ring**, and `--focus-anchor` keeps focus clear of sticky chrome (`tests/test_a11y_guards.py:275,310`).
- **`nav__link--off` survives** and renders `{{ hint }}` as text, never in a `title` attribute (`tests/test_a11y_guards.py:173`).
- **`--glass-solid` must equal `--glass-1` composited over `--bg`,** within one per channel, in light and dark. Changing any one of the three means recomputing the other two. See Task 3 Step 4.
- **The `.skip` link's background stays opaque.** No `rgba`, no `transparent` — it renders outside `.shell` on the darkened desk (`test_glass_contrast.py:316`).
- **`--ground: none` stays true** in all three conditions that declare it, or the comment describing them is fiction (`test_glass_contrast.py:354`).
- **Nothing in a ground or glass token animates.** No `transition`, no `animation` in a backdrop token — stated as a promise in a comment and checked (`test_glass_contrast.py:362`). Task 9's motion tokens must not land there.
- **Change the head of a token family and you change the family.** This has now caused three defects in this plan: `--glass-tint` moved without `--glass-1/2/3`, `--accent` moved without `--accent-strong` and `--accent-wash`, and the light scheme moved without dark. Before committing any token change, `grep` for the token's own prefix (`--accent`, `--glass`, `--ink`, `--rule`) across **all four schemes** and confirm every member still belongs to the same family. Almost none of these pairings is covered by a test, so the grep is the guard.
- **Meaning never rests on colour alone.** Label, weight and shape carry state first.
- **Gradients and mesh are banned from record surfaces** — diff, claims, quoted source, citation viewer.
- **Prose follows Orwell's rules. No emoji.** Comments explaining a decision that still holds are moved across unchanged, not deleted.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `scripts/contrast_report.py` | Compute WCAG ratios from the live stylesheet; regenerate the `PINNED` table. The single source of truth for every published ratio. |
| `app/web/static/fonts/manrope-latin.woff2` | Display face, app surface. |
| `app/web/static/fonts/OFL.txt` | Licence, shipped beside the font. |
| `deploy/site/fonts/manrope-latin.woff2` | Display face, marketing surface. |
| `deploy/site/fonts/OFL.txt` | Licence, marketing surface. |
| `deploy/site/reveal.js` | Scroll reveals and the hero refusal sequence. No library. |
| `tests/test_design_guards.py` | New guards: font is self-hosted, no CDN reference, record surfaces carry no gradient. |

**Modified**

| File | Change |
|---|---|
| `app/web/static/strata.css` | Palette (4 schemes), type tokens, component language, motion tokens. |
| `app/web/templates/base.html` | Masthead, font preload, focus anchor. |
| `app/web/static/clerk.css`, `tour.css`, `workflow.css` | Retune to new tokens. No hex in `clerk.css`. |
| `tests/test_glass_contrast.py` | `PINNED` table regenerated. |
| `deploy/site/site.css` | Full restyle. |
| `deploy/site/index.html` | Hero rebuild, mechanism diagram, section markup for reveals. |
| `deploy/site/{security,privacy,terms,subprocessors,login,404}.html` | Inherit the system. |
| Six app templates | `login`, `project_list`, `project_detail`, `review_centre`, `change`, `workflow_route`. |
| Brand mark: `app/web/static/{logo,favicon}.svg`, `deploy/site/{logo,favicon}.svg`, `app/web/templates/base.html`, `deploy/site/index.html`, `scripts/film.py` | Recolour from `#0d5c6b`. |
| `docs/web-design.html`, `docs/.ai/decisions.html`, `docs/.ai/gaps.html` | New part, three ADRs, one new gap. |

---

## Task 1: The ratio script

The highest-leverage half hour in this plan. Every later palette task depends on it. Write it before touching a colour.

**Files:**
- Create: `scripts/contrast_report.py`
- Test: `tests/test_contrast_report.py`

**Interfaces:**
- Consumes: `contrast()` from `tests/test_glass_contrast.py`.
- Produces: `read_schemes(css_text) -> dict[str, dict[str, str]]`, `pinned_table(schemes) -> str`, and a `__main__` that prints the table.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contrast_report.py
"""The tool that writes every ratio this project publishes.

Ratios were estimated by hand once and the comment drifted from the colour.
tests/test_glass_contrast.py catches the drift; this script is what makes
fixing it a regeneration rather than an arithmetic exercise.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from contrast_report import pinned_table, read_schemes  # noqa: E402

CSS = """
:root { --bg: #ffffff; --ink: #000000; --ink-2: #414d68; --ink-3: #6b7791; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #000000; --ink: #ffffff; --ink-2: #adb7cd; --ink-3: #818da8; }
}
"""


def test_it_reads_a_token_out_of_the_light_block():
    assert read_schemes(CSS)["light"]["--bg"] == "#ffffff"


def test_it_reads_the_dark_block_separately():
    assert read_schemes(CSS)["dark"]["--bg"] == "#000000"


def test_black_on_white_is_the_known_21_to_1():
    table = pinned_table(read_schemes(CSS), [("light", "--ink", "--bg", "test")])
    assert '("light", "--ink", "--bg", 21.00, "test")' in table


def test_a_missing_token_raises_rather_than_defaulting():
    """A token that silently reads as None makes every ratio wrong and quiet."""
    try:
        pinned_table(read_schemes(CSS), [("light", "--nope", "--bg", "x")])
    except KeyError:
        return
    raise AssertionError("a missing token must raise, not default")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_contrast_report.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'contrast_report'`

- [ ] **Step 3: Write the script**

```python
# scripts/contrast_report.py
"""Compute the contrast ratios strata.css publishes, from strata.css itself.

WHY THIS EXISTS. tests/test_glass_contrast.py pins fifteen ratios to two
decimals and fails when a colour moves without its comment. That test is the
guard; this is the tool. Without it, changing a palette means doing WCAG
arithmetic by hand fifteen times, which is how the drift got there.

The parser is deliberately the same shape as the test's: find `:root {` inside
a known @media condition, read `--token: value;`. A real CSS parser is not
worth a dependency, and two parsers that disagree would be worse than one that
is simple.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_glass_contrast import contrast  # noqa: E402

TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.MULTILINE)

CONDITIONS = {
    "light": None,
    "dark": "@media (prefers-color-scheme: dark)",
    "hc-light": "@media (prefers-contrast: more) and (prefers-color-scheme: light)",
    "hc-dark": "@media (prefers-contrast: more) and (prefers-color-scheme: dark)",
}


def _blocks(css: str) -> list[tuple[int, str]]:
    """Every `:root { ... }` body with the offset it starts at."""
    out = []
    for match in re.finditer(r":root\s*\{", css):
        depth, i = 1, match.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append((match.start(), css[match.end() : i - 1]))
    return out


def _condition_at(css: str, offset: int) -> str | None:
    """The innermost @media wrapping this offset, or None."""
    best = None
    for match in re.finditer(r"@media[^{]*\{", css):
        if match.end() > offset:
            continue
        depth, i = 1, match.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        if match.end() <= offset < i:
            best = match.group(0).rstrip("{ ").strip()
    return best


def read_schemes(css: str) -> dict[str, dict[str, str]]:
    """Token values per scheme. Later blocks override earlier ones, as CSS does."""
    schemes: dict[str, dict[str, str]] = {name: {} for name in CONDITIONS}
    for offset, body in _blocks(css):
        condition = _condition_at(css, offset)
        tokens = dict(TOKEN.findall(body))
        for name, wanted in CONDITIONS.items():
            if condition == wanted or wanted is None and condition is None:
                schemes[name].update(
                    {k: v.strip() for k, v in tokens.items()}
                )
    # The high-contrast and dark schemes are overrides layered on light.
    for name in ("dark", "hc-light", "hc-dark"):
        merged = dict(schemes["light"])
        if name == "hc-dark":
            merged.update(schemes["dark"])
        merged.update(schemes[name])
        schemes[name] = merged
    return schemes


def pinned_table(schemes: dict[str, dict[str, str]], rows) -> str:
    """The PINNED literal, ready to paste into tests/test_glass_contrast.py."""
    lines = ["PINNED = ["]
    lines.append(
        "    # scheme,     ink,       ground,           ratio, "
        "where strata.css says it"
    )
    for scheme, ink, ground, where in rows:
        tokens = schemes[scheme]
        for token in (ink, ground):
            if token not in tokens:
                raise KeyError(f"{scheme}: {token} is not defined")
        ratio = contrast(tokens[ink], tokens[ground])
        lines.append(
            f'    ("{scheme}", "{ink}", "{ground}", {ratio:.2f}, "{where}"),'
        )
    lines.append("]")
    return "\n".join(lines)


ROWS = [
    ("light", "--ink", "--bg", "token block"),
    ("light", "--ink-2", "--bg", "prefers-contrast block"),
    ("light", "--ink-3", "--bg", "prefers-contrast block, conceded under AA"),
    ("light", "--ink-2", "--surface-sunk", "prefers-contrast block"),
    ("light", "--ink-3", "--surface-sunk", "conceded under AA"),
    ("light", "--ink", "--glass-solid", "token block, CEILING"),
    ("light", "--ink-2", "--glass-solid", "masthead block, CEILING"),
    ("light", "--ink-3", "--glass-solid", "masthead block, CEILING"),
    ("dark", "--ink-3", "--bg", "prefers-contrast block"),
    ("dark", "--ink-3", "--surface", "prefers-contrast block"),
    ("dark", "--ink-3", "--surface-sunk", "prefers-contrast block"),
    ("hc-light", "--ink-3", "--bg", "prefers-contrast block"),
    ("hc-light", "--ink-3", "--surface-sunk", "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--bg", "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--surface", "prefers-contrast block"),
]


if __name__ == "__main__":
    stylesheet = Path(__file__).resolve().parents[1] / "app/web/static/strata.css"
    print(pinned_table(read_schemes(stylesheet.read_text()), ROWS))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_contrast_report.py -v`
Expected: 4 passed

- [ ] **Step 5: Check it reproduces today's pinned table**

Run: `.venv/bin/python scripts/contrast_report.py`
Expected: output matching the current `PINNED` in `tests/test_glass_contrast.py`. If any row differs, the parser is wrong — fix it now, before it is trusted with a new palette.

- [ ] **Step 6: Commit**

```bash
git add scripts/contrast_report.py tests/test_contrast_report.py
git commit -m "The ratios this project publishes are computed, not typed"
```

---

## Task 2: Self-host the display face

**Files:**
- Create: `app/web/static/fonts/manrope-latin.woff2`, `app/web/static/fonts/OFL.txt`, `deploy/site/fonts/manrope-latin.woff2`, `deploy/site/fonts/OFL.txt`
- Create: `tests/test_design_guards.py`

**Interfaces:**
- Produces: CSS token `--face-display`, used by every later task.

- [ ] **Step 1: Write the failing guard**

```python
# tests/test_design_guards.py
"""Guards for the visual system: the font is ours, the record stays flat.

Both rules are cheap to break by accident and expensive to notice late. A CDN
link is one paste away, and this project has already had to correct a
sub-processor page that had gone untrue. A gradient behind evidence is the
same class of error in a different medium.
"""
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_design_guards.py -v`
Expected: FAIL, `app/web/static/fonts/manrope-latin.woff2 is missing`

- [ ] **Step 3: Fetch the font and licence**

```bash
mkdir -p app/web/static/fonts deploy/site/fonts
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
curl -sS -A "$UA" -o app/web/static/fonts/manrope-latin.woff2 \
  "https://fonts.gstatic.com/s/manrope/v20/xn7gYHE41ni1AdIRggexSvfedN4.woff2"
curl -sS -o app/web/static/fonts/OFL.txt \
  "https://raw.githubusercontent.com/google/fonts/main/ofl/manrope/OFL.txt"
cp app/web/static/fonts/manrope-latin.woff2 deploy/site/fonts/
cp app/web/static/fonts/OFL.txt deploy/site/fonts/
```

Expected: `manrope-latin.woff2` is 24,576 bytes. Verify with `ls -l app/web/static/fonts/`.

- [ ] **Step 4: Declare the face in `strata.css`, at the top of the file**

```css
/* THE ONE WEB FONT, AND WHY IT IS ONLY ON HEADLINES.
 *
 * ADR-012 said no web font, as part of no build step and no CDN. Two of those
 * three still hold and this changes only the third: the file is in this
 * repository, served by this application, fetched by nothing at runtime. A
 * reviewer with no network gets the whole design.
 *
 * Display only. Interface text keeps the system stack, because the dense
 * screens were measured against those metrics and because swapping body text
 * is what makes a page lurch on load. One file, 24KB, the whole 400-800 range.
 */
@font-face {
  font-family: "Manrope";
  src: url("/static/fonts/manrope-latin.woff2") format("woff2-variations");
  font-weight: 400 800;
  font-display: swap;
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6,
    U+02DA, U+02DC, U+2000-206F, U+2074, U+20AC, U+2122, U+2191, U+2193,
    U+2212, U+2215, U+FEFF, U+FFFD;
}
```

Then in the `:root` block, beside the existing face tokens:

```css
  --face-display: "Manrope", var(--face-ui);
```

- [ ] **Step 5: Do the same for `deploy/site/site.css`**

Identical `@font-face`, with `src: url("/fonts/manrope-latin.woff2")` — the site serves from its own root, not `/static/`.

- [ ] **Step 6: Run the guard and the full suite**

Run: `.venv/bin/python -m pytest tests/test_design_guards.py -v && make test`
Expected: guards pass; suite still 2,340 passed, 1 xfailed.

- [ ] **Step 7: Commit**

```bash
git add app/web/static/fonts deploy/site/fonts tests/test_design_guards.py \
        app/web/static/strata.css deploy/site/site.css
git commit -m "One face for headlines, in the repository rather than on somebody else's CDN"
```

---

## Task 3: The palette, all four schemes

The largest single task. The values below are already validated against every rule in the suite — do not substitute your own without re-running Task 1's script.

**Files:**
- Modify: `app/web/static/strata.css` (token block ~line 80, dark block ~line 499, `prefers-contrast` blocks ~line 3273-3300)
- Modify: `tests/test_glass_contrast.py` (`PINNED`, ~line 213)

**Interfaces:**
- Consumes: `scripts/contrast_report.py` from Task 1.
- Produces: the token values every later task styles against.

- [ ] **Step 1: Replace the light token values**

```css
  --bg: #f4f6fb;
  --surface: #ffffff;
  --surface-sunk: #e7ebf4;
  --rule: #d9dfeb;
  --rule-strong: #a8b2c6;
  --ink: #0f1729;
  --ink-2: #414d68;
  --ink-3: #6b7791;
```

- [ ] **Step 2: Replace the dark block values** (inside `@media (prefers-color-scheme: dark)`)

```css
    --bg: #0b1020;
    --surface: #141b2e;
    --surface-sunk: #070b16;
    --rule: #252e45;
    --rule-strong: #3d4863;
    --ink: #e9edf7;
    --ink-2: #adb7cd;
    --ink-3: #818da8;
```

- [ ] **Step 3: Replace the high-contrast blocks**

```css
@media (prefers-contrast: more) and (prefers-color-scheme: light) {
  :root {
    --ink: #05090f;
    --ink-2: #2b3547;
    --ink-3: #4d5872;
    --rule: #b9c2d4;
    --rule-strong: #7f8ba3;
  }
}

@media (prefers-contrast: more) and (prefers-color-scheme: dark) {
  :root {
    --ink: #ffffff;
    --ink-2: #ccd4e6;
    --ink-3: #9fabc4;
    --rule: #39435c;
    --rule-strong: #5a6683;
  }
}
```

- [ ] **Step 4: Update `--glass-solid` AND `--glass-1` together in both schemes**

`--glass-solid` is not decoration. The `@supports` fallback and
`prefers-reduced-transparency: reduce` both paint it **instead of** the translucent pane, so a reader
who cannot have `backdrop-filter` sees exactly this colour.
`test_the_flat_pane_token_matches_the_translucent_one_it_stands_in_for` requires it to equal
`--glass-1` composited over `--bg`, within one per channel. **Changing `--bg` without recomputing
both of these fails the suite** — and the docstring on that test records that this exact drift is the
bug the whole contrast harness was built to catch.

Light:
```css
  --glass-solid: #f8fafd;
  --glass-1: rgba(250, 252, 254, 0.62);
```

Dark:
```css
    --glass-solid: #161d31;
    --glass-1: rgba(29, 37, 59, 0.62);
```

Both composite to drift 0 against the new `--bg` values. Verify before moving on:

```bash
.venv/bin/python -m pytest tests/test_glass_contrast.py::test_the_flat_pane_token_matches_the_translucent_one_it_stands_in_for -v
```

Expected: PASS.

- [ ] **Step 5: Regenerate the pinned table**

Run: `.venv/bin/python scripts/contrast_report.py`

Expected output, which replaces `PINNED` in `tests/test_glass_contrast.py`:

```python
PINNED = [
    # scheme,     ink,       ground,           ratio, where strata.css says it
    ("light", "--ink", "--bg", 16.53, "token block"),
    ("light", "--ink-2", "--bg", 7.81, "prefers-contrast block"),
    ("light", "--ink-3", "--bg", 4.16, "prefers-contrast block, conceded under AA"),
    ("light", "--ink-2", "--surface-sunk", 7.07, "prefers-contrast block"),
    ("light", "--ink-3", "--surface-sunk", 3.76, "conceded under AA"),
    ("light", "--ink", "--glass-solid", 17.09, "token block, CEILING"),
    ("light", "--ink-2", "--glass-solid", 8.08, "masthead block, CEILING"),
    ("light", "--ink-3", "--glass-solid", 4.30, "masthead block, CEILING"),
    ("dark", "--ink-3", "--bg", 5.69, "prefers-contrast block"),
    ("dark", "--ink-3", "--surface", 5.15, "prefers-contrast block"),
    ("dark", "--ink-3", "--surface-sunk", 5.90, "prefers-contrast block"),
    ("hc-light", "--ink-3", "--bg", 6.57, "prefers-contrast block"),
    ("hc-light", "--ink-3", "--surface-sunk", 5.95, "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--bg", 8.20, "prefers-contrast block"),
    ("hc-dark", "--ink-3", "--surface", 7.42, "prefers-contrast block"),
]
```

- [ ] **Step 6: Update every ratio written in a comment in `strata.css`**

Find them: `grep -nE "[0-9]+\.[0-9]+:1" app/web/static/strata.css` — **24 of them**, which is more than the 15 in `PINNED`. Sort each into one of three kinds and treat it accordingly. Getting this wrong in either direction damages the file: recomputing a historical note erases a correction the file deliberately keeps, and leaving a live figure stale is the drift the harness exists to catch.

**Kind A — a live ratio between two tokens.** Recompute it. Use the Task 1 machinery:

```python
import sys; sys.path.insert(0, "tests")
from test_glass_contrast import contrast
sys.path.insert(0, "scripts")
from contrast_report import read_schemes
schemes = read_schemes(open("app/web/static/strata.css").read())
print(contrast(schemes["light"]["--ink-3"], schemes["light"]["--surface-sunk"]))
```

This covers pairs well beyond the pinned 15 — the focus ring against `--surface-sunk`, ink over a claim card, the tab rail. Identify the two tokens the sentence names and compute that pair.

**Kind B — a historical note recording a past error.** Leave it exactly as written. `strata.css` deliberately keeps four superseded glass figures (15.84, 16.15, 16.35, 16.41) with the reasoning that produced each, because the corrections are the value. Lines that begin "THE FIRST VERSION OF THIS COMMENT SAID", "AND THE SAME COMMENT WAS WRONG A SECOND TIME", or "(This comment previously read …)" are history, not claims about the current palette. **Do not update these.** If a historical paragraph would now read as describing the new palette, add one sentence dating it to the old one rather than editing its figures.

**Kind C — a browser-measured pixel value.** These quote what Chromium rendered — `rgb(245,247,249)`, "best pixel in the box", "worst pixel in the box" — and no arithmetic over tokens reproduces them, because they measure the translucent pane over real content. **You cannot recompute these and must not invent replacements.** Mark each one as measured against the pre-redesign palette and therefore expired, in the file's own voice. Say plainly that nothing has re-measured the new pane. That is the honest state and it matches how this stylesheet already treats figures it cannot stand behind.

**A comment that still says `15.57:1` next to a colour that now reads `16.53:1` fails the suite** — that is the mechanism working, not a bug to route around.

- [ ] **Step 7: Run the contrast suite**

Run: `.venv/bin/python -m pytest tests/test_glass_contrast.py -v`
Expected: all pass, including `test_the_ratios_the_stylesheet_writes_down_are_the_ratios_it_has` (15 parametrised cases).

- [ ] **Step 8: Run the full suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 9: Commit**

```bash
git add app/web/static/strata.css tests/test_glass_contrast.py
git commit -m "A palette with more than two hues, and fifteen ratios that still tell the truth"
```

---

## Task 4: The brand mark

`--accent` was `#0d5c6b` and the mark is painted the same hex in six places. Changing one without the others leaves a teal logo on an indigo page.

**Files:**
- Modify: `app/web/static/logo.svg`, `app/web/static/favicon.svg`, `deploy/site/logo.svg`, `deploy/site/favicon.svg`, `app/web/templates/base.html`, `deploy/site/index.html`, `scripts/film.py`

- [ ] **Step 1: Find every occurrence**

Run: `grep -rln "0d5c6b" --include="*.html" --include="*.svg" --include="*.css" --include="*.py" . | grep -v .venv`
Expected: the seven files above, plus `app/web/static/strata.css` (done in Task 3) and two docs (handled in Task 16).

- [ ] **Step 2: Set the new accent in `strata.css`**

```css
  --accent: #2f4bd8;   /* institutional indigo: anything you can act on */
  --alarm:  #a02c1d;   /* refusal keeps its oxblood weight */
```

- [ ] **Step 2b: The two teal remnants Task 3 found and deliberately left here**

Neither is pinned and no test covers them, so nothing will fail if you skip this — they will simply read faintly green against an indigo page, which is the kind of defect that survives review because no one can name it.

`--hatch` is the old ink at an alpha. Re-derive it from the new ink in each scheme:

```css
  --hatch: rgba(15, 23, 41, 0.16);      /* light: --ink #0f1729 at .16 */
```
```css
    --hatch: rgba(233, 237, 247, 0.18); /* dark: --ink #e9edf7 at .18 */
```

The high-contrast `--hatch` values are achromatic (`rgba(0,0,0,0.34)`, `rgba(255,255,255,...)`) and need no change.

`--glass-cast` is the shadow colour, and its own comment says shadows here are blue-black and never neutral. The light value `#04191f` is a teal-black. Replace with a blue-black in the new family:

```css
  --glass-cast: #060b1a;     /* shadows are blue-black, never neutral */
```

Dark stays `#000000` and the high-contrast value stays `#fff`; both are already hue-free.

- [ ] **Step 3: Replace the hex in all seven files**

```bash
grep -rl "0d5c6b" --include="*.html" --include="*.svg" --include="*.py" . \
  | grep -v .venv | grep -v docs/ \
  | xargs sed -i '' 's/#0d5c6b/#2f4bd8/g'
```

- [ ] **Step 4: Verify the two marks are still byte-identical**

```bash
diff <(grep -o '<svg class="logo".*</svg>' app/web/templates/base.html) \
     <(grep -o '<svg class="logo".*</svg>' deploy/site/index.html) \
  && echo "marks identical"
```

Expected: `marks identical`. This was true before the change and must stay true.

- [ ] **Step 5: Run the full suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "One mark, one hue, in all six places it is painted"
```

---

## Task 5: Type scale and component language

**Files:**
- Modify: `app/web/static/strata.css`

- [ ] **Step 1: Add display type tokens beside the existing scale**

```css
  /* Display sizes. These are the only places --face-display is used, and they
     are all headings. The interface scale above is untouched: --t-micro
     through --t-h1 were measured against the system stack and the 12px floor
     was set for a real reader on real hardware. */
  --t-display: 3rem;      /* 48px — the hero line, marketing only */
  --t-title: 2rem;        /* 32px — page and section titles */
  --t-lead: 1.25rem;      /* 20px — the sentence under a title */
  --track-display: -0.02em;  /* geometric faces need negative tracking large */
```

- [ ] **Step 2: Add the shape and depth tokens**

```css
  /* Shape. Larger radii than the record had, and deliberately not applied to
     the record: see the gradient rule. Chrome is soft; evidence is square. */
  --r-card: 14px;
  --r-control: 10px;
  --r-pill: 999px;

  --lift-1: 0 1px 2px color-mix(in srgb, var(--ink) 6%, transparent),
            0 2px 8px -4px color-mix(in srgb, var(--ink) 18%, transparent);
  --lift-2: 0 2px 4px color-mix(in srgb, var(--ink) 7%, transparent),
            0 16px 32px -18px color-mix(in srgb, var(--ink) 32%, transparent);
```

- [ ] **Step 3: Apply the display face to headings**

```css
h1, .t-display, .t-title {
  font-family: var(--face-display);
  letter-spacing: var(--track-display);
  font-weight: 750;
}

/* Every figure a reader compares down a column. A number that changes width
   between renders reads as a bug in the data, not in the type. */
.count, .metric, td.num, .coord { font-variant-numeric: tabular-nums; }
```

- [ ] **Step 4: Run the suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 5: Commit**

```bash
git add app/web/static/strata.css
git commit -m "A display scale for headings, and figures that hold their column"
```

---

## Task 6: The masthead

**Files:**
- Modify: `app/web/templates/base.html`, `app/web/static/strata.css`

**Interfaces:**
- Consumes: tokens from Tasks 2, 3, 5.
- Must preserve: `nav__link--off` with `{{ hint }}` as text, the `Admin` item gated on `admin_menu(request)`, `--focus-anchor`.

- [ ] **Step 1: Read the guard before editing**

Run: `.venv/bin/python -m pytest tests/test_a11y_guards.py -v`
Expected: all pass. These are the rules the edit must not break.

- [ ] **Step 2: Restyle the masthead in `strata.css`**

Keep the existing structure. Change only surface, depth and type: `--glass-veil` background, `--lift-1` on scroll, `--face-display` on the wordmark text, `--r-control` on the nav links.

- [ ] **Step 3: Confirm `--focus-anchor` still clears the bar**

```css
/* The sticky bar grew; the anchor grows with it, or a keyboard user tabs to a
   control that scrolls under the chrome and appears not to have focus. */
:root { --focus-anchor: 5rem; }
```

- [ ] **Step 4: Run the a11y guards and the full suite**

Run: `.venv/bin/python -m pytest tests/test_a11y_guards.py -v && make test`
Expected: guards pass; 2,340 passed, 1 xfailed.

- [ ] **Step 5: Keyboard-walk the masthead**

Start the app (`make run`), tab from the top of any page. Every nav item takes focus, the ring is visible on each, and the disabled item announces its reason. No focus stop hides behind the bar.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/base.html app/web/static/strata.css
git commit -m "A masthead with weight, and a focus ring that still clears it"
```

---

## Task 7: The gradient ban, enforced

**Files:**
- Modify: `tests/test_design_guards.py`
- Modify: `app/web/static/strata.css` if the guard finds anything

- [ ] **Step 1: Add the failing guard**

```python
# append to tests/test_design_guards.py
RECORD_SELECTORS = (
    ".diff",
    ".claim",
    ".source",
    ".citation",
    ".passage",
)


def test_no_record_surface_carries_a_gradient():
    """Evidence is shown on paper, not on a painted background.

    The whole argument of this product is that a claim traces to the words
    behind it. A gradient behind those words is decoration applied to the one
    surface that must not look decorated. Chrome may be soft; the record is
    flat.
    """
    css = (ROOT / "app/web/static/strata.css").read_text()
    offenders = []
    for block in css.split("}"):
        if "gradient" not in block:
            continue
        selector = block.split("{")[0]
        for name in RECORD_SELECTORS:
            if name in selector:
                offenders.append(selector.strip()[:60])
    assert not offenders, (
        f"gradient on a record surface: {offenders}. Chrome is soft, the "
        "record is flat -- see the visual redesign spec, section 5."
    )
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_design_guards.py -v`
Expected: PASS today. It is a ratchet against later tasks, not a bug report.

- [ ] **Step 3: Commit**

```bash
git add tests/test_design_guards.py
git commit -m "A rule about where gradients may go, enforced rather than remembered"
```

---

## Task 8: Component sheets retuned

**Files:**
- Modify: `app/web/static/clerk.css`, `app/web/static/tour.css`, `app/web/static/workflow.css`

- [ ] **Step 1: Confirm the constraints**

Run: `.venv/bin/python -m pytest tests/test_chat_surface.py tests/test_a11y_guards.py -v`
Expected: pass. `clerk.css` may write no hex and no `rgba()`; each sheet needs `:focus-visible` and `prefers-reduced-motion`.

- [ ] **Step 2: Retune each sheet to the new tokens**

Replace radii with `--r-card` / `--r-control`, shadows with `--lift-1` / `--lift-2`. **Write no colour literal in `clerk.css`** — consume tokens only.

- [ ] **Step 3: Verify no hex crept into `clerk.css`**

Run: `grep -nE "#[0-9a-fA-F]{3,6}|rgba?\(" app/web/static/clerk.css`
Expected: no output.

- [ ] **Step 4: Run the suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 5: Commit**

```bash
git add app/web/static/clerk.css app/web/static/tour.css app/web/static/workflow.css
git commit -m "Three component sheets speaking the new tokens and no colour of their own"
```

---

## Task 9: Application motion

**Files:**
- Modify: `app/web/static/strata.css`

- [ ] **Step 1: Add the motion tokens**

```css
  /* Motion. --dur-tap and --dur-lift already existed and are unchanged; the
     refusal is new and deliberately does NOT use --spring. A refusal that
     bounces playfully is telling the reader the wrong thing about what just
     happened. It is firm and it stops. */
  --dur-refuse: 200ms;
  --ease-refuse: cubic-bezier(0.2, 0, 0.38, 1);
```

- [ ] **Step 2: Style the refusal state**

```css
.claim--refused {
  animation: refuse var(--dur-refuse) var(--ease-refuse) both;
  border-left: 3px solid var(--alarm);
}

@keyframes refuse {
  from { opacity: 0; transform: translateY(-2px); }
  to   { opacity: 1; transform: none; }
}
```

- [ ] **Step 3: Answer reduced motion in the same file**

```css
@media (prefers-reduced-motion: reduce) {
  /* The end state, immediately. Never nothing: the refusal is the message. */
  .claim--refused { animation: none; }
}
```

- [ ] **Step 4: Run the a11y guards**

Run: `.venv/bin/python -m pytest tests/test_a11y_guards.py -v`
Expected: pass, including `test_a_sheet_that_moves_answers_the_reader_who_asked_it_not_to`.

- [ ] **Step 5: Commit**

```bash
git add app/web/static/strata.css
git commit -m "The refusal gets motion of its own, and it does not bounce"
```

---

## Tasks 10-15: The six demo-path screens

One task each, same shape. Do them in this order: `login.html`, `project_list.html`, `project_detail.html`, `review_centre.html`, `change.html`, `workflow_route.html`.

**For each screen:**

- [ ] **Step 1: Open the screen in the running app and note what is wrong**

Run `make run`, visit the route, and write down the three worst things. Layout first, then hierarchy, then detail.

- [ ] **Step 2: Restyle using existing tokens only**

No new colour literals. If a value is needed that no token provides, add the token to `strata.css` rather than the literal to the template.

- [ ] **Step 3: Record surfaces stay flat**

On `change.html` and `review_centre.html` especially: the diff, the claims and the quoted source get no gradient, no large radius, no lift. They are paper.

- [ ] **Step 4: Run the suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 5: Keyboard-walk the screen**

Every interactive element takes focus, the ring is visible, nothing hides behind the masthead.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/<screen>.html app/web/static/strata.css
git commit -m "<one sentence naming what the screen now says that it did not>"
```

---

## Task 16: The eighteen inherited screens

**Files:**
- Check: every template in `app/web/templates/` not touched by Tasks 10-15

- [ ] **Step 1: Walk each one in the running app**

`admin_index`, `actions`, `feedback_review`, `integrations`, `invite_accept`, `invites_admin`, `permissions`, `proceeding`, `proceedings`, `review`, `review_project`, `shared_claim`, `shares_admin`, `users_admin`, `workflow_edit`, `workflow_list`, plus the `_clerk` and `_tour` partials.

- [ ] **Step 2: Fix only breakage, not design**

Overflow, unreadable contrast, collapsed layout. **Do not redesign them** — that was scoped out deliberately.

- [ ] **Step 3: Run the suite and commit**

```bash
make test
git add -A && git commit -m "Eighteen screens that inherited the system, checked rather than assumed"
```

---

## Task 17: The marketing site

**Files:**
- Modify: `deploy/site/site.css`, `deploy/site/index.html`
- Create: `deploy/site/reveal.js`
- Modify: `deploy/site/{security,privacy,terms,subprocessors,login,404}.html`

- [ ] **Step 1: Restyle `site.css` against the new system**

Mirror the app's tokens by hand. The two stylesheets do not share a file and that is deliberate — the site may carry gradients the app may not.

- [ ] **Step 2: Rebuild the hero around the refusal**

Not a product screenshot. A claim that declines to assert itself because its citation did not verify. This is the hardest technical decision in the product, in the first screenful of the public site.

- [ ] **Step 3: Write `reveal.js`**

```javascript
/* Scroll reveals. No library, no build step -- the same constraint the rest of
 * this project works under (ADR-012).
 *
 * Reduced motion is answered by doing nothing at all: the CSS ships the end
 * state and this script only ever removes it. A reader who asked for less
 * motion gets the finished page, not an empty one. That order matters; the
 * opposite would leave them looking at nothing.
 */
(function () {
  var wants = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (wants.matches || !("IntersectionObserver" in window)) return;

  var targets = document.querySelectorAll("[data-reveal]");
  Array.prototype.forEach.call(targets, function (el) {
    el.classList.add("is-hidden");
  });

  var seen = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.remove("is-hidden");
        seen.unobserve(entry.target);
      });
    },
    { threshold: 0.15 }
  );
  Array.prototype.forEach.call(targets, function (el) {
    seen.observe(el);
  });
})();
```

- [ ] **Step 4: Add the reveal styles with their reduced-motion answer**

```css
[data-reveal] { transition: opacity 400ms var(--ease), transform 400ms var(--ease); }
[data-reveal].is-hidden { opacity: 0; transform: translateY(16px); }

@media (prefers-reduced-motion: reduce) {
  /* The script never runs, so .is-hidden is never applied. This is belt and
     braces for a reader who changes the setting with the page already open. */
  [data-reveal], [data-reveal].is-hidden {
    opacity: 1; transform: none; transition: none;
  }
}
```

- [ ] **Step 5: Carry the system to the six remaining pages**

- [ ] **Step 6: Publish and check every link resolves**

Run: `make publish-docs`
Expected: `Every internal link in the published copy resolves.`

- [ ] **Step 7: Run the suite**

Run: `make test`
Expected: 2,340 passed, 1 xfailed.

- [ ] **Step 8: Commit**

```bash
git add deploy/site
git commit -m "A landing page whose first screenful is the refusal"
```

---

## Task 18: The documents

The project's own rule: the ADR ships in the same change as the decision. Three decisions were taken here.

**Files:**
- Modify: `docs/web-design.html`, `docs/.ai/decisions.html`, `docs/.ai/gaps.html`

- [ ] **Step 1: Write the ADR revisiting ADR-012**

Web font, narrowly. No build step and no CDN both survive; only "no web font" falls. Name the file, its size, its licence, and the fact that an offline reviewer still gets the whole design.

- [ ] **Step 2: Write the ADR on the palette**

Must say why two hues was right for a 48-hour build and wrong for a finished product. **An ADR that only says "we wanted more colours" is worth nothing at a panel.**

- [ ] **Step 3: Write the ADR on gradient confinement**

Chrome is soft, the record is flat, and `tests/test_design_guards.py` enforces it.

- [ ] **Step 4: Add the new part to `docs/web-design.html`**

Superseding Parts two and three. Say plainly that the earlier parts describe a surface that no longer exists.

- [ ] **Step 5: Add the demo film to `docs/.ai/gaps.html`**

The film shows the old design the moment this lands. It was scoped out of this work on purpose; record it as a gap rather than leaving it unsaid. Add it to the P2 demonstration table.

- [ ] **Step 6: Re-run the suite and publish**

Run: `make test && make publish-docs`
Expected: 2,340 passed, 1 xfailed; every internal link resolves.

- [ ] **Step 7: Commit**

```bash
git add docs
git commit -m "Three decisions, written down in the change that took them"
```

---

## Task 19: Final verification

- [ ] **Step 1: Fresh clone check**

Run: `make fresh-check`
Expected: exit zero, `fresh clone: tests pass`.

- [ ] **Step 2: Walk all four colour schemes**

macOS: System Settings > Appearance for light and dark; Accessibility > Display > Increase contrast for the high-contrast pair. Check the six demo-path screens in each. **Test coverage is not the same as looking at it.**

- [ ] **Step 3: Confirm the offline promise**

Disable the network, hard-reload the app and the site. The display face still renders. If it falls back to the system stack, the font is being fetched rather than served.

- [ ] **Step 4: Re-count and update any published test figure**

If the suite count changed, update `docs/submission.html`, `docs/tech-questions-faq.html` and `docs/.ai/interview-bank.html` in the same change. This is gap P1-5 and it is cheap to avoid repeating.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "The surface a reviewer opens is the surface this project describes"
```

---

## Self-Review Notes

**Spec coverage:** §4 type → Tasks 2, 5. §5 colour → Tasks 3, 4, 7. §6 motion → Tasks 9, 17. §7 site → Task 17. §8 app → Tasks 5, 6, 8, 10-16. §9 docs → Task 18. §10 acceptance → Task 19. §3 constraints → Global Constraints, checked in every task's test step.

**Known gap, stated rather than hidden:** Tasks 10-15 and 16 cannot carry literal code, because the right edit depends on what the screen looks like once Tasks 3-9 land. They carry a fixed procedure and explicit rules instead. Every task that *can* be pinned to exact code is.

**Deferred deliberately:** re-recording the demo film (spec §12), the nine open items on `docs/.ai/gaps.html`.
