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

# No `^` anchor: strata.css puts one token per line, but a compact block (as
# in the test fixture) can hold several on one line. Every `--token: value;`
# inside a :root body is a declaration; nothing else in that body looks like
# one, so anchoring to line start would only lose real matches for no gain.
TOKEN = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")

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
