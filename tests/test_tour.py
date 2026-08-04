"""The first-visit walkthrough: a few tooltips, a cookie, and no tooltip that
points at nothing.

WHAT THIS FILE IS DEFENDING. A product tour is the one feature that is trivial
to build and impossible to notice breaking. Nobody sees it twice, so nobody
sees it fail. Four failures are specific to it, and none of them look like
anything in a diff.

1. A TOOLTIP POINTING AT NOTHING. Every library that does this anchors the tip
   to the top-left corner when the target is missing, and a first-time reader on
   an empty workspace meets a box in the corner explaining a thing that is not
   on their screen. The stops below are checked against real rendered pages:
   every selector the tour looks for is proved to exist on a screen this
   application actually serves, and the empty workspace is rendered on purpose
   to prove the other stops resolve to nothing rather than to a corner.

2. THE ARGUMENT NEVER LANDS. The tour exists for one purpose -- to get a first
   reader to a claim refusing to assert itself. A tour that shows the project
   board and stops has cost the reader time and taught them nothing. So the
   withheld stop is required to resolve on *every* screen: on the change screen
   it points at the claim itself, and everywhere else at the count of what was
   refused.

3. THE COOKIE LOOKS LIKE A CREDENTIAL. app/auth/sessions.py holds a bearer
   token; this holds a preference. They must not share a name, a lifetime or a
   reader, and script must be able to read this one -- which is exactly what
   makes it dangerous to confuse the two.

4. THE WORDS DRIFT. app/chat/persona.py is the one home for what the assistant
   is called. The tour points at its button and must not spell its name.

WHAT THESE TESTS CANNOT DO. There is no browser and no JavaScript engine here,
and there will not be one on a reviewer's machine either. So nothing below lays
out a page: no test here can prove the tip sits beside its target, that focus
moves, or that Escape ends the run. What they prove is what can be proved
without a browser -- that the stop table points at markup this application
really renders, that the copy carries the argument, that the cookie is what it
says it is, and that the code carries the paths the rest depends on.

WHAT WAS OBSERVED ELSEWHERE, AND IS NOT RE-RUN HERE. The behaviour above was
driven in headless Chromium while it was being built, against these exact
screens rendered by this application, at 1200x800 and at 390x700: the tour
opening once and not again, the ring landing on the withheld claim itself and
not on a corner, the tip staying inside the viewport and off the claim it
describes, three stops skipping on an empty workspace, the assistant stop
skipping when its button is in the markup and hidden, Tab reaching the buttons
and Enter advancing, Escape ending it, the preference being written and
honoured, ?tour=1 running it again, and no console error on any of them. That
harness is not in this suite: it needs a browser and a server the repository
does not carry, and a test that skips on every machine is worse than an honest
sentence saying it was checked by hand on this date. Anyone changing tour.js
should expect to check those again the same way.

Offline: no network, no browser, no model.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.chat import persona
from app.seed import load
from app.state.db import get_engine, session_scope
from app.state.models import Base
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.deps import SESSION_COOKIE
from app.web.views import changes as changes_view
from app.web.views import proceedings as proceedings_view
from app.web.views import projects as projects_view
from app.web.views import review as review_view

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app" / "web" / "templates" / "_tour.html"
SCRIPT = ROOT / "app" / "web" / "static" / "tour.js"
STYLES = ROOT / "app" / "web" / "static" / "tour.css"
BASE = ROOT / "app" / "web" / "templates" / "base.html"

# The preference cookie. Named here so the collision test compares two constants
# rather than two spellings of the same string.
COOKIE = "strata_tour"

# The query that runs the tour again on purpose.
REPLAY = "tour=1"

DOCKET = "MPUC-2026-0142"
PAIRED = "CHG-v1-v2-003"
PROJECT = "PRJ-MPUC-2026-0142"

# The stops, in the order the reader meets them. The argument is the order.
STOP_IDS = ("board", "docket", "citation", "withheld", "assistant")

# Below this a phone in portrait; the app's own widest narrow breakpoint.
PHONE_REM = 62


# --------------------------------------------------------------------- rigging


@pytest.fixture
def client() -> TestClient:
    """The screens, assembled here rather than imported from app/main.py.

    Same reason tests/test_screens.py does it: a component has to be testable
    before anything wires it in. The static mount is real, because the template
    links both files as fixed absolute paths and a 404 there is the whole
    feature missing.
    """
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(projects_view.router)
    app.include_router(proceedings_view.router)
    app.include_router(changes_view.router)
    app.include_router(review_view.router)
    return TestClient(app)


@pytest.fixture
def empty_schema():
    """Tables, no rows. A person on their first day, before anything is loaded."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def seeded(empty_schema):
    with session_scope() as session:
        return load(session)


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def styles() -> str:
    return STYLES.read_text()


SEEDED_SCREENS = (
    "/",
    "/proceedings",
    f"/proceedings/{DOCKET}",
    f"/changes/{PAIRED}",
    f"/projects/{PROJECT}",
    "/escalations",
)


# ------------------------------------------------------------ a small selector
#
# There is no HTML parser in this environment and adding one for a tour would be
# a dependency bought with somebody else's build. So the matcher below reads the
# tiny subset of CSS the stop table is allowed to use -- a tag, class tokens, one
# attribute test -- and refuses anything else loudly rather than quietly
# returning False, which would turn "this selector is wrong" into "this selector
# found nothing" and hide the defect it exists to catch.

_TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:\s[^<>]*)?)>", re.S)
_ATTR = re.compile(r'([\w:.-]+)(?:\s*=\s*"([^"]*)")?')
_SEL = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*)?(?P<classes>(?:\.[\w-]+)*)(?P<attr>\[[^\]]*\])?$"
)
_ATTR_TEST = re.compile(r'^\[([\w:-]+)(?:(\^?=)"([^"]*)")?\]$')


def _elements(html: str):
    """Every open tag: its name, its attributes, and where its content starts."""
    for match in _TAG.finditer(html):
        attrs = {}
        for attr in _ATTR.finditer(match.group(2) or ""):
            attrs[attr.group(1).lower()] = attr.group(2) or ""
        yield match.group(1).lower(), attrs, match.end()


def _selects(name: str, attrs: dict, selector: str) -> bool:
    parsed = _SEL.match(selector)
    assert parsed, f"the tour uses a selector this matcher cannot read: {selector}"
    if parsed["tag"] and parsed["tag"].lower() != name:
        return False
    classes = attrs.get("class", "").split()
    for token in re.findall(r"\.([\w-]+)", parsed["classes"] or ""):
        if token not in classes:
            return False
    raw = parsed["attr"]
    if raw:
        test = _ATTR_TEST.match(raw)
        assert test, f"the tour uses an attribute test this matcher cannot read: {raw}"
        key, op, value = test.group(1).lower(), test.group(2), test.group(3)
        if key not in attrs:
            return False
        if op == "=" and attrs[key] != value:
            return False
        if op == "^=" and not attrs[key].startswith(value):
            return False
    return True


def _text_from(html: str, start: int, name: str) -> str:
    """The text inside one element, tags stripped and whitespace collapsed."""
    end = html.find(f"</{name}", start)
    inner = html[start : end if end != -1 else len(html)]
    return " ".join(re.sub(r"<[^>]*>", " ", inner).split())


def _present(html: str, selector: str, text: str | None = None) -> bool:
    for name, attrs, at in _elements(html):
        if not _selects(name, attrs, selector):
            continue
        if text is None:
            return True
        if _text_from(html, at, name).startswith(text):
            return True
    return False


def _resolves(html: str, variant: dict) -> bool:
    """A variant resolves when any one of its selectors is on the page.

    This mirrors tour.js. It cannot mirror the visibility check the script also
    makes -- an element can be in the markup and hidden, and only a browser
    knows -- so a variant that resolves here may still be skipped there. That
    direction is safe: the script skips more than this test does, never less.
    """
    return any(_present(html, find, variant.get("text")) for find in variant["find"])


def _first_variant(html: str, stop: dict) -> dict | None:
    for variant in stop["variants"]:
        if _resolves(html, variant):
            return variant
    return None


def stops() -> list[dict]:
    """The stop table, read out of the template that carries it."""
    body = TEMPLATE.read_text()
    block = re.search(r"<script[^>]*data-tour-stops[^>]*>(.*?)</script>", body, re.S)
    assert block, "_tour.html carries no [data-tour-stops] block"
    return json.loads(block.group(1))


def _strip_comments(body: str) -> str:
    """Prose in a comment is not code. Every ban below reads the code only."""
    body = re.sub(r"\{#.*?#\}", " ", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    body = re.sub(r"(?m)^\s*//.*$", " ", body)
    return body


# ------------------------------------------------------------------- the files


def test_both_files_are_served_where_the_template_asks_for_them(client, template):
    """The failure this repository keeps having, in its cheapest form: a script
    tag pointing at a path nothing serves."""
    for path in ("/static/tour.js", "/static/tour.css"):
        assert path in template, f"_tour.html never asks for {path}"
        assert client.get(path).status_code == 200, f"nothing serves {path}"


def test_the_walkthrough_ships_hidden_and_is_revealed_by_script(template, script):
    """Same rule the assistant dock keeps and the citation viewer inverts. Every
    control here needs script to do anything, so a control rendered without one
    is a control that lies."""
    root = re.search(r"<div[^>]*data-tour\b[^>]*>", template, re.S)
    assert root, "_tour.html has no [data-tour] root"
    assert "hidden" in root.group(0), "the tour root must ship hidden"
    assert "hidden" in script, "tour.js never reveals anything"


# -------------------------------------------------------------------- the stops


def test_the_stops_are_five_and_end_on_the_thing_that_matters():
    table = stops()
    assert [stop["id"] for stop in table] == list(STOP_IDS)
    for stop in table:
        assert stop["variants"], stop["id"]
        for variant in stop["variants"]:
            assert variant["find"], stop["id"]
            assert variant["title"].strip(), stop["id"]
            assert len(variant["body"].split()) >= 20, (
                f"{stop['id']} says too little to carry the argument on its own"
            )


def test_the_withheld_stop_says_what_the_product_is_for():
    """Somebody who reads only the tour has to come away with the argument. The
    words are checked because this is the one stop that carries it."""
    stop = next(s for s in stops() if s["id"] == "withheld")
    for variant in stop["variants"]:
        words = variant["body"].lower()
        assert "reason" in words, variant["title"]
        assert "no statement" in words or "refused" in words, variant["title"]


def test_every_selector_the_tour_looks_for_exists_on_a_real_screen(client, seeded):
    """The whole point. A stop pointing at markup nothing renders is a tooltip
    in the corner of somebody's first visit."""
    pages = {url: client.get(url).text for url in SEEDED_SCREENS}
    for url, body in pages.items():
        assert body, url

    for stop in stops():
        for variant in stop["variants"]:
            for find in variant["find"]:
                found = [
                    url
                    for url, body in pages.items()
                    if _present(body, find, variant.get("text"))
                ]
                assert found, (
                    f"{stop['id']} looks for {find!r} and no screen renders it"
                )


def test_the_argument_reaches_every_screen(client, seeded):
    """The withheld stop is the reason the tour exists, so it may never be the
    one that gets skipped."""
    for url in SEEDED_SCREENS:
        body = client.get(url).text
        stop = next(s for s in stops() if s["id"] == "withheld")
        assert _first_variant(body, stop) is not None, url


def test_on_the_change_screen_it_points_at_the_claim_itself(client, seeded):
    """Where the real thing is on the page, the tour must point at the real
    thing and not at a link to a queue that counts it."""
    body = client.get(f"/changes/{PAIRED}").text
    stop = next(s for s in stops() if s["id"] == "withheld")
    variant = _first_variant(body, stop)
    assert variant is not None
    assert ".claim--withheld" in variant["find"], variant["title"]


def test_an_empty_workspace_skips_what_it_cannot_find(client, empty_schema):
    """A person on their first day, before anything is loaded. Three stops have
    nothing to point at and must resolve to nothing -- not to the corner."""
    body = client.get("/").text
    resolved = {stop["id"]: _first_variant(body, stop) for stop in stops()}

    for missing in ("board", "docket", "citation"):
        assert resolved[missing] is None, (
            f"{missing} claims a target on a workspace with no rows"
        )
    assert resolved["withheld"] is not None, "the argument was lost on an empty screen"
    assert resolved["assistant"] is not None


def test_the_step_counter_counts_only_what_will_show(template, script):
    """'1 of 5' on a screen that shows two stops is the same lie as a tooltip
    pointing at nothing."""
    assert "of 5" not in template and "of 5" not in script
    assert "data-tour-step" in template


# ------------------------------------------------------------------ the cookie


def test_the_preference_is_not_the_session(script):
    """A cookie script can read sitting next to a cookie script must never
    read. They may not share a name, and this file may not name the other one."""
    assert COOKIE != SESSION_COOKIE
    assert not COOKIE.startswith(SESSION_COOKIE)
    assert COOKIE in script, "tour.js does not use the cookie this test guards"
    assert SESSION_COOKIE not in script, "tour.js names the session cookie"


def test_the_cookie_is_set_the_way_the_session_cookie_is(script):
    code = _strip_comments(script)
    assert "SameSite=Lax" in code
    assert "path=/" in code.lower().replace(" ", "") or "Path=/" in code
    assert "Max-Age=" in code, "no max-age: a session cookie by accident"
    lives = [int(n) for n in re.findall(r"\b(\d{6,})\b", code)]
    assert lives, "no lifetime long enough to be one"
    assert max(lives) >= 30 * 24 * 3600, "the walkthrough would come back within a month"


def test_secure_comes_off_only_where_the_session_cookie_drops_it_too(script):
    """app/web/deps.py takes Secure off for plain http on this machine, because
    Safari drops a Secure cookie there and the product looks broken. The same
    reasoning, the same trigger, and it says so rather than failing in silence."""
    code = _strip_comments(script)
    assert "Secure" in code
    assert re.search(r'protocol\s*===?\s*"https:"', code), (
        "Secure is unconditional, so the tour repeats on every visit over plain http"
    )
    assert "console" in code, "the downgrade is silent"


def test_the_code_says_this_is_a_preference_and_not_a_credential(script):
    said = script.lower()
    assert "preference" in said
    assert "credential" in said


# ------------------------------------------------------- offline, and one voice


def test_the_tour_reaches_no_host_but_this_one(template, script, styles):
    """No CDN, no build step, no web font. It works with the network off."""
    for name, body in (("_tour.html", template), ("tour.js", script), ("tour.css", styles)):
        code = _strip_comments(body)
        assert "http://" not in code, name
        assert "https://" not in code.replace('"https:"', ""), name
        assert "@import" not in code, name
        assert not re.search(r"""["'(]//[a-z0-9]""", code), name
        assert "fetch(" not in code, name
        assert "XMLHttpRequest" not in code, name


def test_the_tour_borrows_the_apps_palette_rather_than_inventing_one(styles):
    body = _strip_comments(styles)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body)
    assert not re.search(r"\brgba?\(\s*\d", body)
    assert "var(--" in body


def test_it_never_spells_the_assistants_name(template, script):
    """persona.py is the one home for that. The tour points at the button."""
    for name, body in (("_tour.html", template), ("tour.js", script)):
        assert persona.DISPLAY_NAME not in body, name
        assert persona.TAGLINE not in body, name
        assert persona.GREETING not in body, name


# ------------------------------------------------- motion, corners and the page


def test_motion_happens_only_where_the_reader_has_not_asked_against_it(styles):
    """Gated on no-preference rather than switched off under reduce, so a
    browser that says nothing gets stillness rather than movement."""
    assert "prefers-reduced-motion: no-preference" in styles
    outside = re.sub(
        r"@media[^{]*prefers-reduced-motion:\s*no-preference[^{]*\{.*?\n\}",
        " ",
        styles,
        flags=re.S,
    )
    for banned in ("transition:", "animation:", "@keyframes"):
        assert banned not in _strip_comments(outside), (
            f"{banned} outside the motion gate"
        )


def test_the_tip_has_no_resting_place_in_the_corner(styles):
    """The failure mode of every library that does this. The tip carries no
    top or left of its own: the script sets them from a target it has already
    found, so a tip with no target has nowhere to be drawn."""
    for block in re.findall(r"\.tour-(?:tip|ring)\s*\{([^}]*)\}", styles):
        for edge in ("top:", "left:", "right:", "bottom:"):
            assert edge not in block, f"{edge} on the tip gives it a corner to fall into"


def test_a_target_in_the_markup_is_not_the_same_as_a_target_on_the_screen(script):
    """A permission can hide a control without removing it, and the assistant's
    own buttons are hidden until its script reveals them."""
    assert "getClientRects" in script


def test_escape_ends_it_and_focus_goes_back(script):
    code = _strip_comments(script)
    assert '"Escape"' in code
    assert "activeElement" in code, "nothing remembers where focus was"
    assert re.search(r"\.focus\(\)", code)


def test_it_says_how_to_run_it_again(template, script):
    """Told inside the product, not only in a comment nobody reads."""
    markup = re.sub(r"\{#.*?#\}", " ", template, flags=re.S)
    assert REPLAY in markup, "the tour never says how to replay it"
    assert REPLAY in script, "tour.js does not honour the replay it advertises"


def test_no_fixed_width_wider_than_a_phone(styles):
    offenders = [
        match.group(0).strip()
        for match in re.finditer(r"(?:^|[\s;{])(?:min-)?width:\s*([0-9]{3,})px", styles)
        if int(match.group(1)) >= 400
    ]
    assert not offenders, offenders


# ------------------------------------------------------------------ the wiring


@pytest.mark.xfail(
    strict=True,
    reason=(
        "NOT WIRED. base.html does not include _tour.html, so the walkthrough "
        "is built and reaches nobody. Add, after the _clerk.html include and "
        "before </body>:  {% include \"_tour.html\" %}  -- then DELETE this "
        "xfail marker, which is what turns this test red on you."
    ),
)
def test_base_html_includes_the_tour():
    body = BASE.read_text()
    clerk = body.find('include "_clerk.html"')
    tour = body.find('include "_tour.html"')
    assert tour != -1, "base.html never includes _tour.html"
    assert clerk != -1 and tour > clerk, (
        "the tour must be included after the assistant dock: both scripts are "
        "deferred, they run in document order, and the tour points at a button "
        "clerk.js has not revealed yet if it runs first"
    )
