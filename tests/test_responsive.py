"""The application on a phone, guarded by the things that actually break it.

WHY THIS FILE EXISTS. The stylesheet carried nine media queries, which reads as
"responsive" to anyone counting. Three of them were breakpoints and they held
four rules between them, none about tables -- while ten tables lived across six
templates with no scroll container anywhere. A table wider than the viewport
pushes the whole page sideways, the masthead scrolls away from the content, and
nothing lines up again. That is the symptom every reader recognises as not built
for a phone, and a media-query count hid it.

WHAT THESE TESTS CAN AND CANNOT DO. They read the stylesheet and the rendered
markup. They cannot lay out a page, so they cannot prove the app looks right on a
handset -- only a browser at a real width can do that, and nobody has done it.
What they can do is catch the specific regressions that caused this defect and
that a person reviewing a diff will not notice: a fixed pixel width, a table with
no way to scroll, a viewport tag deleted, a long digest with nothing letting it
break. Each test below guards one of those and claims nothing further.

Offline, no network, no browser.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.db import init_db, session_scope
from app.state.models import ROLE_ANALYST

CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "strata.css"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"

# Below this, a phone in portrait. The app's own narrow breakpoints are 34rem,
# 40rem and 62rem; anything guarding "a small screen" must fire at or above the
# widest of those, or it guards nothing a phone will meet.
PHONE_BREAKPOINT_REM = 62


def _blocks(css: str, pattern: str) -> list[str]:
    """The body of every at-rule whose header matches, braces balanced."""
    out = []
    for match in re.finditer(pattern, css):
        start, depth, i = match.end(), 1, match.end()
        while depth and i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        out.append(css[start : i - 1])
    return out


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text()


@pytest.fixture
def client() -> TestClient:
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
    c = TestClient(app, base_url="https://testserver")
    email = next(a.email for a in demo_account_list() if a.role == ROLE_ANALYST)
    assert c.post(
        "/login", data={"email": email, "password": DEMO_PASSWORD}, follow_redirects=False
    ).status_code == 303
    return c


# ------------------------------------------------------------------ the tables


def test_a_table_can_scroll_itself_on_a_narrow_screen(css):
    """The defect this file was written for.

    Ten tables, no scroll container, no responsive rule. Asserting the rule
    exists is weak on its own -- so this also checks the two declarations that
    make it work rather than merely look like it works. display:block without
    overflow-x scrolls nothing; overflow-x without display:block does nothing to
    a table, because a table box does not establish one.
    """
    narrow = "\n".join(
        _blocks(css, rf"@media\s*\(max-width:\s*(?:[0-9]|[1-5][0-9]|6[0-{PHONE_BREAKPOINT_REM % 10}])"
                     r"[0-9]*(?:\.[0-9]+)?rem\)\s*\{")
    )
    assert narrow, "no narrow-width media query survives in the stylesheet"

    rules = [body for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", narrow) if "table" in sel]
    assert rules, "no narrow-width rule targets `table`; a wide table will push the page sideways"

    joined = " ".join(rules)
    assert "overflow-x" in joined, "the table rule does not let the table scroll"
    assert "display" in joined and "block" in joined, (
        "overflow-x on a table box does nothing -- a table does not establish a "
        "block formatting context, so display:block is what makes it scroll"
    )


def test_no_fixed_pixel_width_wide_enough_to_break_a_phone(css):
    """A single `width: 900px` undoes every breakpoint above it.

    Small fixed widths are fine and common -- an icon, a chip, a rule. The
    dangerous ones are wider than a phone, so that is where the line sits.
    """
    offenders = [
        m.group(0).strip()
        for m in re.finditer(r"(?:^|[\s;{])(?:min-)?width:\s*([0-9]{3,})px", css)
        if int(m.group(1)) >= 400
    ]
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_a_long_unbroken_string_may_break(css):
    """A 64-character chain digest with nowhere to break sets the page width.

    This product renders digests, version ids and quoted fragments, so it has
    more of these than most.
    """
    assert re.search(r"overflow-wrap:\s*(anywhere|break-word)", css), (
        "nothing in the stylesheet lets a long token break"
    )


# ------------------------------------------------------------- every template


def test_every_rendered_screen_declares_a_viewport(client):
    """Without this tag a phone renders at desktop width and scales down.

    Every breakpoint in the stylesheet is dead if this is missing from base.html,
    and its absence looks like nothing in a diff.
    """
    for path in ("/", "/projects", "/proceedings", "/review", "/escalations"):
        body = client.get(path).text
        assert 'name="viewport"' in body, f"{path} renders without a viewport tag"
        assert "width=device-width" in body, f"{path} does not size to the device"


def test_the_login_page_carries_a_viewport_too(client):
    """It is served outside the session guard and inherits nothing by default."""
    body = TestClient(app, base_url="https://testserver").get("/login").text
    assert 'name="viewport"' in body and "width=device-width" in body


def test_no_template_hardcodes_a_width_the_stylesheet_cannot_override(css):
    """An inline style beats every rule in the stylesheet, breakpoints included."""
    offenders = []
    for template in sorted(TEMPLATES.glob("*.html")):
        for match in re.finditer(r'style="([^"]*)"', template.read_text()):
            for width in re.findall(r"(?:min-)?width:\s*([0-9]{3,})px", match.group(1)):
                if int(width) >= 400:
                    offenders.append(f"{template.name}: {match.group(1)[:60]}")
    assert not offenders, f"inline widths that beat the breakpoints: {offenders}"
