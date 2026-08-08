#!/usr/bin/env python3
"""Record ten movements. Each one shows a capability and ends with one line.

WHAT WAS WRONG WITH THE OLD CUT, said plainly because it is the whole reason
this file changed. It ran 93 seconds over 25 shots and every shot was true, and
a viewer reaching the end could not say what any single screen had been FOR. The
captions described what was on the glass -- "the review queue", "the coverage
strip" -- and a description is not a reason. Screens followed screens because
they were next in the file.

THE RULE THIS CUT IS BUILT ON: ONE TAKEAWAY PER CAPABILITY.

  * A movement shows one capability and ends on ONE short line saying what it
    means for the person watching. Not what the screen is. What it is for.
  * That line is drawn in a colour the ordinary captions never use, so within
    two movements the viewer has learned that this colour means "here is the
    point". See TAKEAWAY_INK below for the colour and why it is that one.
  * Takeaways are short. Under twelve words wherever the sentence survives it.
  * A shot with no takeaway is cut. If nobody can say what it was for, it did
    not earn its seconds.

THE ORDER, AND THE ONE LINE EACH MOVEMENT EXISTS TO DELIVER.

   1. The refusal.       The quote is not in the source, so the claim is not made.
   2. The same rule.     Same check, opposite answer. Nobody chose which.
   3. Real filings.      The corpus was not built to pass. A filer published it.
   4. Routing.           A name appears here only when somebody put it there.
   5. The queue.         What it withheld is counted beside what it kept.
   6. Logins.            Your people, your roles, and a record of who granted what.
   7. Permissions.       You decide who may approve. A conflict is named, never hidden.
   8. Approval routes.   The approval route is yours to draw, not ours.
   9. Security, access.  We measured it. Where it falls short, we say so.
  10. Close.             You act on what it proves. It says nothing else.

A TAKEAWAY IS READ AGAINST THE PICTURE BEHIND IT, which decided two of those
ten. Movement four ends on the change where routing DID name an owner, so its
line cannot be "it will not name an owner it cannot stand behind" -- true of the
movement, false of the frame. Movement five ends on the coverage strip, so the
sentence about settling a refusal is said back on the queue where it belongs and
the line over the strip is about the strip. A caption that argues with the
screen under it is the same defect as a claim that argues with its citation.

LENGTH. The holds add up to 104 seconds across 40 shots, and the file lands
longer than that: navigation, scrolling and the keyboard walk are real time no
beat() counts. Every hold outside movement one was cut to the bone to pay for
movements six to nine; movement one was not, because the refusal is the only
thing here that nothing else in the market does and it is worth the discomfort.

WHY MOVEMENTS SIX TO EIGHT ARE NEW, and they are the largest change here. The
old cut filmed nothing an administrator touches, on the argument that a feature
tour is a fail signal. That argument was right about tours and wrong about
these three screens. An enterprise buyer watching this is holding three
questions and will not ask them out loud: can I add my people, can I control
what they may do, can I shape the approval route to my own organisation. Every
one of those is answered by a screen that exists today, and each is a single
beat plus a line. They are not a tour because none of them is filmed for its own
sake: each answers a question the buyer already has.

WHAT IS STILL DELIBERATELY NOT FILMED. The sign-in door, because a password must
not be on screen and must not be spoken. The first-visit tour, the project
board, the assistant, the invitation queue, the share register, the feedback
screen and the source registry -- the last four are named on the /admin index in
movement six, which is honest about their existing without spending four seconds
each proving it. That index lists only what the account signed in may open, so
the row is evidence rather than a brochure. The hash chain has NO SCREEN in this
product (verify_chain is
reached from scripts/backup.py and app/state/replay.py and from nothing a
browser can open), so nothing here is captioned as the chain.

EVERY CLAIM ON SCREEN IS TRUE, AND THE SECURITY MOVEMENT WAS MEASURED RATHER
THAN REMEMBERED. Before this cut was written:

    curl -sS -o /dev/null -D - https://strata.sudama.ai/login
      -> content-security-policy: frame-ancestors 'none'; base-uri 'self';
         object-src 'none'; form-action 'self'
      -> x-frame-options: DENY, x-content-type-options: nosniff,
         referrer-policy: no-referrer
    curl on /users /permissions /admin /admin/workflows /workflow /escalations
    /review /changes/CHG-v1-v2-003 while signed out
      -> 303 to /login, and x-frame-options: DENY on every one of the redirects

So "security headers ship on every response, redirects included" and "nothing
here can be framed" are checked facts about the instance being filmed, not a
reading of app/web/headers.py.

THE ACCESSIBILITY LINE IS THE ONE THAT COULD HAVE DISCREDITED THE FILM, and it
says less than it could. WCAG 2.2 was swept (tests/test_a11y_guards.py walks
every template and every stylesheet, so a screen added next week inherits the
rule), the focus ring is defined once and no sheet may take it from
:focus-visible, and the contrast was measured on RENDERED PIXELS -- strata.css
:164-176 records the sample: /proceedings/MPUC-2026-0142 at 1440x900 dpr 2, the
masthead text set transparent, every pixel in the .nav__link--off box read, best
pixel 4.49:1 and worst 3.85:1 for --ink-3. That measurement says --ink-3 does
NOT clear AA on the masthead. So the film says exactly that, and no caption
anywhere claims conformance. A product whose argument is that it refuses to
assert what it cannot prove cannot claim an AA it does not hold; the honest line
is also the better line, which is lucky but not why it is there.

A CAPTION DOES NOT PLAY UNLESS THE SCREEN HOLDS WHAT IT SAYS, and the check
happens OFF CAMERA wherever it can. This rule was bought. A take was recorded in
which three captions and 10.6 seconds of confident narration ran over FastAPI's
default 404 body -- one line reading {"detail":"no such proceeding"} on a white
page -- because the docket the captions named is not in every database and the
navigation to it was unconditional. So the guard sits on the MOVEMENT, not on a
beat inside it, and it is decided before the camera points anywhere. Movements
three, six, seven and eight are each under one guard read off camera.

THE DOOR IS OPENED OFF CAMERA, and that needs two browser contexts rather than
one. Playwright records a context, not a page, so signing in on the recorded
context puts the login form -- and the keystrokes of a password -- in the file.
So: context one signs in and is never recorded; its cookies are handed to
context two, which is the one with a camera on it.

Context one also WARMS every screen the film will visit. That is not politeness.
The first view of an unjudged change is a GET that writes: it asks the model
whether the change matters, stores the verdict and appends to the audit chain.
Warming it means the model call, and its five-to-twenty seconds of dead air,
happen off camera -- and the recorded pass is then a pure read from first frame
to last.

WHICH ACCOUNT TO SIGN IN AS. Movements six to eight need `user.manage` (logins,
permissions) and `workflow.manage` (approval routes), and app/state/identity.py
puts both on the admin role and nowhere else. app/seed.py gives that role to
whoever the corpus calls VP -- on the shipped corpus that is Sarah Lindqvist,
VP Regulatory Affairs, at sarah.lindqvist@mep.example. An analyst account still
films: the three movements are skipped, loudly, off camera. Nothing here guesses
at a role; each screen is opened before the camera starts and the answer is read
off the response.

Run. STRATA_DATABASE_URL DECIDES WHICH DATABASE IS TOUCHED and nothing else
does: `make seed` takes no database argument, and app/state/db.py falls back to
sqlite:///strata.db -- the repo's own -- when the variable is unset. Export it
before the seed, not after, or the seed lands on the committed database.

    mkdir -p /tmp/film
    export STRATA_DATABASE_URL=sqlite:////tmp/film/strata.db
    make seed                          # into that database, not the repo's
    uvicorn app.main:app --port 8111   # against the same one
    python3 scripts/film.py <email> <password>

Or, to film the deployed instance, which is what the released cut records:

    STRATA_FILM_BASE=https://strata.sudama.ai python3 scripts/film.py <email> <password>

The seed matters for a second reason. `make seed` runs scripts/ingest_real.py,
which is the only thing that puts the Kentucky docket in a database; without it
movement three has nothing to film and cuts itself.

Needs playwright in the SYSTEM python, not the project virtualenv. The product
has no browser dependency and must not grow one for the sake of a video.
"""

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

# Where to film. Local by default; STRATA_FILM_BASE points it at the deployed
# instance instead.
#
# WHAT FILMING PRODUCTION WRITES, now that the recorded pass is read-only. One
# login session row, and -- on the warm-up pass only -- a materiality verdict per
# change the model has not judged yet, plus an ACCESS_DENIED row for each admin
# screen the account may not open, each appended to the append-only,
# hash-chained audit log. Nothing the camera sees writes anything at all: no
# form on any administrative screen is submitted, and the three that could
# create a person, a grant or a route are filmed and left alone.
BASE = os.environ.get("STRATA_FILM_BASE", "http://127.0.0.1:8111").rstrip("/")

# Overridable, and created with its parents.
OUT = pathlib.Path(os.environ.get("STRATA_FILM_OUT", "/tmp/strata-film"))

W, H = 1440, 900

# THE ONE CHANGE THE FILM IS BUILT ON. It carries two claims about the same
# passage at the same offsets: CLM-CHG-2 says the threshold is 20 megawatts and
# the source agrees, CLM-MISQUOTE says 10 and it does not. One page, one shape,
# opposite answers. Named here rather than found by clicking and hoping.
HERO_CHANGE = "CHG-v1-v2-003"

# Only used to build the anchor claim.html renders, so the cold open lands on
# the card instead of scrolling down to it on camera. A wrong id here costs
# nothing: the fragment is ignored and the scroll below corrects it.
HERO_WITHHELD = "CLM-MISQUOTE"

# The change where a person confirmed the mapping, from scripts/seed_demo_gaps.py.
# Its Obligations section names who and when; the hero change's does not.
MAPPED_CHANGE = "CHG-v1-v2-004"

# app/web/views/changes.py:268. change.html:429 prints it in front of the name
# and the date on a confirmed mapping, and it appears nowhere else on the page --
# which is what lets the confirmed row be told from the candidate rows below it,
# since both are drawn as p.claim__route.
LABEL_CONFIRMED = "Confirmed by"

# The real pair, from scripts/ingest_real.py. 1,024,409 characters against
# 1,024,536: a 127-character difference across a million, which is that file's
# own arithmetic and the number movement three says out loud.
REAL_DOCKET = "KY-PSC-2025-00113"

# The administrative screens, spelled here once. Each is owned by a view that
# names its own URL constant; these are those values and there is a test that
# every router in app/web/views is mounted.
ADMIN_INDEX = "/admin"              # app/web/templating.py::ADMIN_URL
USERS = "/users"                    # app/web/views/users_admin.py::USERS_URL
PERMISSIONS = "/permissions"        # app/web/views/permissions.py::PERMISSIONS_URL
WORKFLOWS = "/admin/workflows"      # app/web/views/admin.py::WORKFLOWS_URL
LIVE_ROUTE = "/workflow"            # app/web/views/workflow.py::ROUTE_URL

# app/web/static/tour.js reads this and stays shut when it is set.
#
# The VALUE matters as much as the name. tour.js checks `preference() === DONE`
# where DONE is the literal "done", so any other value leaves the tour opening on
# every screen that has one -- and several screens have their own.
TOUR_COOKIE = "strata_tour"
TOUR_DONE = "done"


# What routing said, in plain words, keyed by the code the screen printed.
#
# READ OFF THE PAGE RATHER THAN ASSUMED. The routing outcome for a change
# depends on which mappings exist and whether their duties have live owners, and
# the seed can move. A caption asserting a refusal over a screen that named
# somebody would be this film committing the exact fault the film is about, so
# the code is lifted from the DOM and the sentence is chosen from it.
ROUTING_LINE = {
    "ROUTE_MAPPING_UNCONFIRMED":
        "Nobody has confirmed which duty this touches, so it names <b>no owner</b>.",
    "ROUTE_NO_OBLIGATION":
        "No duty on this company's record reaches these words. It names <b>nobody</b>.",
    "ROUTE_OBLIGATION_UNOWNED":
        "The duty it touches has no owner, so it names <b>nobody</b>.",
    "ROUTE_OWNER_UNKNOWN":
        "The owner on file is not an account here. It names <b>nobody</b>.",
    "ROUTE_OWNER_INACTIVE":
        "The owner's account is shut, so it names <b>nobody</b>.",
    "ROUTE_OWNERS_DISAGREE":
        "Two owners are named and they do not agree. A person has to choose.",
    "ROUTE_OK":
        "Here it does name somebody, because somebody confirmed the mapping.",
    "ROUTE_PENDING_ACCEPTANCE":
        "It names the person, and says they have not accepted yet.",
}
ROUTING_FALLBACK = "It will not name an owner it cannot stand behind."


# THE TAKEAWAY COLOUR, AND WHY IT IS THIS ONE.
#
# The brand accent is #2f4bd8, and on the caption plate -- rgba(9,13,20,.94),
# very nearly #090d14 -- it measures 2.56:1. Unreadable, so the accent as
# written is not an option. #6ccddc is not an invented brightening of it: it is
# what strata.css:508 already sets --accent to in the dark scheme, which is the
# product's own answer to "this accent, on a dark surface". On the plate it
# measures 10.57:1.
#
# It is also unmistakably NOT the other two inks in this film: captions are
# white (19.46:1) and the emphasis inside an ordinary caption is #ffd479
# (13.85:1), a warm amber. Cyan against white and amber cannot be confused by
# anybody, including a viewer who reads the film with the sound off, which is
# most of them.
#
# NO EMPHASIS INSIDE A TAKEAWAY. A takeaway is one colour, whole, because the
# colour IS the emphasis; amber inside it would put two signals in one line and
# undo the thing the colour was for.
TAKEAWAY_INK = "#6ccddc"

CAPTION_CSS = f"""
/* THE BACKGROUND HUGS THE WORDS, it is not a bar across the frame.
   A lower-thirds gradient darkens a third of the picture whether or not there
   is text there, and on these screens that third is where the withheld claim
   sits -- the one shot the whole film exists to reach. So the panel itself is
   transparent and the contrast rides on the text: an inline span with padding
   and box-decoration-break:clone, which makes every wrapped line carry its own
   rounded background instead of one box drawn round the whole block. */
#strata-cap{{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;
  padding:0 56px 46px;pointer-events:none;text-align:center;
  opacity:0;transition:opacity .28s ease}}
#strata-cap.on{{opacity:1}}
#strata-cap .cap{{
  display:inline;
  -webkit-box-decoration-break:clone;box-decoration-break:clone;
  background:rgba(9,13,20,.94);
  color:#fff;
  font:600 38px/1.62 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  letter-spacing:.004em;
  padding:.16em .34em;
  border-radius:5px;
  /* A hairline of the same dark under the glyphs, so a light screen behind a
     descender cannot thin the edge out. */
  box-shadow:0 0 0 1px rgba(9,13,20,.94);
}}
#strata-cap b{{color:#ffd479;font-weight:700}}
/* The point of the movement. One colour, whole line, slightly heavier -- and
   any stray emphasis inside it is folded back into the same colour rather than
   allowed to compete. */
#strata-cap .cap--point{{color:{TAKEAWAY_INK};font-weight:700}}
#strata-cap .cap--point b{{color:inherit}}
"""

#: The two classes a caption can carry. Named so a typo is a NameError here
#: rather than a takeaway that silently rendered as an ordinary line.
CLASS_PLAIN = "cap"
CLASS_POINT = "cap cap--point"


def caption(page, text: str, klass: str = CLASS_PLAIN) -> None:
    """Put a line at the foot of the frame. Empty string clears it."""
    page.evaluate(
        """([text, css, klass]) => {
            let el = document.getElementById('strata-cap');
            if (!el) {
                const s = document.createElement('style');
                s.textContent = css; document.head.appendChild(s);
                el = document.createElement('div');
                el.id = 'strata-cap'; document.body.appendChild(el);
            }
            if (!text) { el.classList.remove('on'); return; }
            // One inline span so the background follows the words and wraps
            // with them. A block-level background would be the bar this design
            // is getting rid of.
            el.innerHTML = '<span class="' + klass + '">' + text + '</span>';
            el.classList.add('on');
        }""",
        [text, CAPTION_CSS, klass],
    )


def point(page, text: str, ms: int, why: str = "TAKEAWAY") -> None:
    """The line a movement exists to deliver, in the takeaway colour, held.

    One function rather than a flag on caption(), so a movement that forgot its
    takeaway is visible as a missing call while reading this file top to bottom.
    """
    caption(page, text, CLASS_POINT)
    beat(page, ms, f"* {why}: {text}")


#: Every hold, so the run can print what it spent. A cut that has drifted over
#: the ceiling should say so here rather than be discovered in a player.
_HELD: list[int] = []

#: What the recorded file should come in under, in seconds. The holds below add
#: up to less than this: navigation, scrolling and the keyboard walk are real
#: time that no beat() counts.
CEILING_S = 108


def beat(page, ms: int, why: str) -> None:
    """Hold a shot. The comment is the storyboard."""
    _HELD.append(ms)
    print(f"  {ms/1000:>4.1f}s  {why}")
    page.wait_for_timeout(ms)


def glide(page, to: int, steps: int = 30) -> None:
    """Scroll in small steps. One jump reads as a cut and loses the reader."""
    for i in range(steps):
        page.mouse.wheel(0, to / steps)
        page.wait_for_timeout(16)


def here(page, selector: str) -> bool:
    """Is this on the page at all. Used to skip a beat rather than film a blank."""
    return page.locator(selector).count() > 0


def count(page, selector: str) -> int:
    """How many. Read off camera to choose between two true sentences."""
    return page.locator(selector).count()


def text_of(page, selector: str) -> str:
    """The first match's text, squeezed, or "". Never raises on a missing node."""
    node = page.locator(selector).first
    if not node.count():
        return ""
    return " ".join((node.inner_text() or "").split())


def centre(page, selector: str, nudge: int = 0, smooth: bool = False) -> bool:
    """Put an element in the middle of the frame. False when it is not there.

    strata.css sets `scroll-behavior: smooth` on the document, so an anchor jump
    animates and a beat timed off it starts before the picture has settled.
    behavior:'instant' overrides it for this call and leaves the stylesheet
    alone. Pass smooth=True where the move itself carries meaning -- travelling
    from the refused claim up to the one that verified is the argument, and a
    hard cut between two cards that look alike loses it.

    `nudge` pushes the element up by that many pixels. The caption sits at the
    foot of the frame, and a card centred exactly has its last two lines under
    the words. Positive nudge lifts the card clear.
    """
    target = page.locator(selector).first
    if not target.count():
        return False
    target.evaluate(
        "(el, smooth) => el.scrollIntoView("
        "  {block: 'center', behavior: smooth ? 'smooth' : 'instant'})",
        smooth,
    )
    page.wait_for_timeout(900 if smooth else 0)
    if nudge:
        page.evaluate("n => window.scrollBy(0, n)", nudge)
    page.wait_for_timeout(220)
    return True


def centre_confirmed(page, nudge: int = 0) -> bool:
    """Put the confirmed mapping in the middle. False when there is none.

    NOT centre("#bears-on", nudge=150), which is what this used to be. That was
    a pixel offset tuned against one page: the drop from the heading to the
    confirmed row is however tall the routing card above it happens to be, so
    the number frames the right sentence on this change and something else on
    the next. Centring the ROW puts the caption's subject in the middle whatever
    is stacked over it.
    """
    found = page.evaluate(
        """(label) => {
            for (const el of document.querySelectorAll('p.claim__route')) {
                if ((el.textContent || '').indexOf(label) < 0) continue;
                (el.closest('li') || el).scrollIntoView(
                    {block: 'center', behavior: 'instant'});
                return true;
            }
            return false;
        }""",
        LABEL_CONFIRMED,
    )
    if not found:
        return False
    if nudge:
        page.evaluate("n => window.scrollBy(0, n)", nudge)
    page.wait_for_timeout(220)
    return True


def cut(page) -> None:
    """Clear the caption and let it fade before the picture changes.

    Without the wait the words are still on screen at the frame the navigation
    lands, which reads as a caption belonging to the next shot.
    """
    caption(page, "")
    page.wait_for_timeout(320)


def walk_the_focus(page, presses: int = 8, gap: int = 230) -> None:
    """Tab through the page on camera, so the ring is seen rather than claimed.

    THE ONLY BEAT IN THE FILM THAT DEMONSTRATES RATHER THAN DESCRIBES A
    NON-VISUAL PROPERTY. Everything else in the security movement is a fact
    about responses a camera cannot see; this one a viewer watches happen.

    Eight presses from a freshly loaded page walk the skip link and the masthead,
    which is where the ring is largest and most legible, and it lands somewhere
    definite whatever the page below is made of. It does NOT hunt for a
    particular control: a walk that had to find one would either take thirty
    presses on a long page or fail on a short one, and the caption over it --
    that the ring is never taken away -- is true at every stop.

    The caption element is a div with no tabindex and pointer-events:none, so it
    is not in the tab order and cannot catch the focus this is showing.
    """
    for _ in range(presses):
        page.keyboard.press("Tab")
        page.wait_for_timeout(gap)


def routing_code(page) -> str:
    """The routing reason code the Obligations section printed, or "".

    change.html puts it in the first .coord inside the first .card of the
    section headed by #bears-on. Read positionally rather than by a class of its
    own, because it has none -- and reading the DOM is still better than the
    alternative, which is a caption asserting a refusal nobody checked.
    """
    return page.evaluate(
        """() => {
            const h = document.getElementById('bears-on');
            const section = h && h.closest('section');
            const coord = section && section.querySelector('.card .coord');
            return coord ? coord.textContent.trim() : '';
        }"""
    )


def real_change_url(page) -> str:
    """The first change in the open pair that carries a claim, or "".

    proceeding.html draws two tables. The version timeline has four columns and
    the change list has five, so the width test tells them apart without either
    growing a class for this script's benefit. The claims cell reads "none" when
    there is none, which is the only row shape this has to reject.
    """
    return page.evaluate(
        """() => {
            const rows = Array.from(document.querySelectorAll('table.table tbody tr'));
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 5) continue;
                const claims = (cells[4].textContent || '').trim().toLowerCase();
                if (!claims || claims === 'none') continue;
                const link = cells[0].querySelector('a');
                if (link) return link.getAttribute('href');
            }
            return '';
        }"""
    )


def first_route(page) -> tuple[str, str]:
    """The first drawn route's URL and the word on its status badge, or two "".

    workflow_list.html draws one row per route: the name is a link in the first
    cell and the status is a badge in the second. Read off camera, because the
    caption that follows says "this one is live" and may only say it over a
    route the page called active.
    """
    return tuple(
        page.evaluate(
            """() => {
                const row = document.querySelector('table.table tbody tr');
                if (!row) return ['', ''];
                const link = row.querySelector('td a');
                const badge = row.querySelector('td .badge');
                return [
                    link ? link.getAttribute('href') : '',
                    badge ? (badge.textContent || '').trim().toLowerCase() : '',
                ];
            }"""
        )
    )


def opens(page, path: str, holds: str) -> bool:
    """Did this screen open for this account, with the thing the film needs on it.

    TWO QUESTIONS AND BOTH MATTER. Every administrative screen answers 403 with
    a rendered page rather than a stack trace -- one template, two answers, as
    users_admin.py and permissions.py both say in their own words -- so a 403
    body is a real page with a heading on it and a caption over it would read as
    a narration of a refusal nobody meant to film. And a 200 that did not draw
    the section the captions describe is no better. So: the status, and then the
    one selector the movement is built on.

    Called only from the off-camera context. An answer read after the camera is
    pointed at a page is an answer read too late.
    """
    answer = page.goto(f"{BASE}{path}", wait_until="networkidle")
    if not (answer and answer.ok):
        code = answer.status if answer else "no answer"
        print(f"  {path}: {code} -- this account cannot open it")
        return False
    if not here(page, holds):
        print(f"  {path}: opened, but {holds} is not on it")
        return False
    print(f"  {path}: open")
    return True


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: film.py <email> <password>")
    email, password = sys.argv[1], sys.argv[2]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"filming {BASE} into {OUT}")

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ------------------------------------------------- off camera: the door
        #
        # No record_video_dir on this context, so nothing here reaches the file:
        # not the login form, not the password, not the warm-up pass and not the
        # writes it triggers.
        door = browser.new_context(
            viewport={"width": W, "height": H}, device_scale_factor=2
        )
        opener = door.new_page()
        opener.goto(f"{BASE}/login", wait_until="networkidle")
        opener.fill('input[name="email"]', email)
        opener.fill('input[name="password"]', password)
        # Scoped to the form that has a password field. login.html also draws a
        # one-click demo door whose buttons are submit buttons too, and a bare
        # `button[type=submit]` can pick one of those instead -- signing in as
        # somebody else and, worse, doing it silently.
        opener.locator('form:has(input[name="password"])').locator(
            'button[type="submit"], input[type="submit"]'
        ).first.click()
        opener.wait_for_load_state("networkidle")

        if "/login" in opener.url:
            raise SystemExit("sign-in failed; nothing was recorded")

        # Warm every analyst screen the take visits, so the first view of an
        # unjudged change -- the one GET in this product that writes and may call
        # a model -- happens here rather than as dead air mid-shot.
        for path in (
            f"/changes/{HERO_CHANGE}",
            f"/changes/{MAPPED_CHANGE}",
            "/escalations",
            "/review",
        ):
            opener.goto(f"{BASE}{path}", wait_until="networkidle")
            print(f"  warmed {path}")

        # THE REAL DOCKET IS VISITED ON ITS OWN, because this request does two
        # jobs. It warms the page, and it decides whether movement three is
        # filmed at all. scripts/ingest_real.py is the only thing that puts this
        # docket in a database, so a database seeded without it answers this GET
        # 404 and FastAPI renders its default JSON body -- there is no HTML error
        # page in this app to catch it.
        docket = opener.goto(
            f"{BASE}/proceedings/{REAL_DOCKET}", wait_until="networkidle"
        )
        real_docket_ok = bool(docket and docket.ok) and here(opener, "table.table")
        print(
            "  real docket: "
            + ("present" if real_docket_ok else "ABSENT -- movement three is cut")
        )

        # And find its one claimed change, READ WHILE THAT PAGE IS STILL OPEN.
        # real_change_url() scans the table on whatever page the opener happens
        # to be showing, and the admin probes below leave it on a different one
        # with a table of its own -- so a call moved after them returns a link to
        # a login row or a permission row and the camera follows it.
        real_change = real_change_url(opener) if real_docket_ok else ""
        print(f"  real change: {real_change or 'none -- that beat is cut'}")
        if real_change:
            opener.goto(f"{BASE}{real_change}", wait_until="networkidle")

        # --------------------------------------- off camera: what this account is
        #
        # Nothing below guesses at a role. app/state/identity.py puts user.manage
        # and workflow.manage on the admin role and nowhere else, so an analyst
        # signing in loses movements six to eight -- and loses them HERE, in a
        # printed line, rather than as three captions over three refusal pages.
        can_users = opens(opener, USERS, "#add-title")
        can_permissions = opens(opener, PERMISSIONS, "#matrix")
        can_routes = opens(opener, WORKFLOWS, ".wf-new")

        # Two sentences are true of the permissions matrix and only one of them
        # is true of any given workspace, so the count decides which is said.
        # A caption naming a held conflict over a register that found none would
        # be this film inventing a finding, which is the one thing it may not do.
        conflicts = 0
        direct_grants = 0
        if can_permissions:
            opener.goto(f"{BASE}{PERMISSIONS}", wait_until="networkidle")
            # THE SCREEN'S OWN NUMBER, not a count of rows. permissions.html
            # prints `{{ conflicts | length }}` in the .coord beside the heading,
            # and .reg__line is drawn for a held pair AND for the sentence that
            # says there are none -- so counting that class answers 1 for an
            # empty register.
            said = text_of(opener, "#conflicts-title .coord")
            conflicts = int(said) if said.isdigit() else 0
            direct_grants = count(opener, ".pcell--direct") + count(
                opener, ".pcell--both"
            )
            print(f"  permissions: {conflicts} held conflict(s), "
                  f"{direct_grants} direct grant cell(s)")

        # The route the editor movement opens, and whether the list called it
        # live. Discovered rather than named: WF-0001 is what scripts/seed_route.py
        # writes today and a film that hard-coded it would caption an empty page
        # on any workspace that drew its own.
        route_url, route_status, route_nodes = "", "", 0
        if can_routes:
            opener.goto(f"{BASE}{WORKFLOWS}", wait_until="networkidle")
            route_url, route_status = first_route(opener)
            if route_url:
                opener.goto(f"{BASE}{route_url}", wait_until="networkidle")
                # The canvas is drawn by app/web/static/workflow.js from a JSON
                # block in the page, so the nodes exist a moment after load
                # rather than in the HTML. Counted here; an editor that drew
                # none is an editor this film does not open.
                opener.wait_for_timeout(700)
                route_nodes = count(opener, ".wf-node")
            print(f"  route: {route_url or 'none drawn'} "
                  f"[{route_status or '?'}] {route_nodes} step node(s)")

        signed_in = door.storage_state()
        door.close()

        # ------------------------------------------------ on camera: the take
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(OUT),
            record_video_size={"width": W, "height": H},
            device_scale_factor=2,          # retina, so text is not mush
            reduced_motion="no-preference",
            storage_state=signed_in,
        )
        # Before the first navigation, so no first-visit tour ever opens over a
        # shot. Several screens carry their own.
        ctx.add_cookies([{"name": TOUR_COOKIE, "value": TOUR_DONE, "url": BASE}])
        page = ctx.new_page()

        # ============================================== 1. THE REFUSAL
        #
        # The one movement that gets more room than feels comfortable, because
        # it is the only thing here nothing else does. No login, no dashboard,
        # no title card: the first frame is a claim that will not be made.
        page.goto(
            f"{BASE}/changes/{HERO_CHANGE}#claim-{HERO_WITHHELD}",
            wait_until="networkidle",
        )
        if not centre(page, ".claim--withheld", nudge=70):
            raise SystemExit(
                f"no withheld claim on /changes/{HERO_CHANGE}; the corpus is not "
                "seeded as the film expects and there is no shot to record"
            )

        beat(page, 1200, "COLD OPEN: the card, before a word is said about it")

        caption(page, "A claim was written about this passage. It is not on this page.")
        beat(page, 3000, "the refusal, stated")

        caption(page, "Its citation quoted the filing as <b>10 megawatts</b>.")
        beat(page, 3000, "what the claim said")

        caption(page, "The filing says <b>20</b>. Strata read the source again to check.")
        beat(page, 4000, "what the source says -- HOLD THIS ONE")

        caption(page, "Nothing it would have said is printed here, in any field.")
        beat(page, 2800, "and there is no field to print it in")

        point(page, "The quote is not in the source, so the claim is not made.", 4200)

        # ============================================== 2. THE SAME RULE
        #
        # Directly above, in the same column, in the same shape. change.html
        # keeps them adjacent on purpose and refuses to split them under two
        # headings; this movement is why.
        cut(page)
        if not centre(page, ".claim--verified", nudge=40, smooth=True):
            raise SystemExit(
                f"no verified claim on /changes/{HERO_CHANGE}; the pairing the "
                "film is built on is not in this database"
            )
        caption(page, "The claim above it went through the same check.")
        beat(page, 2200, "the verified claim")

        chip = page.locator(".claim--verified .citation-chip").first
        if chip.count():
            chip.click()
            page.wait_for_timeout(500)
            centre(page, ".claim--verified", nudge=40)
            caption(page, "Its words are in the source, at the characters it named.")
            beat(page, 2600, "the source panel, with the cited span marked")

        point(page, "Same check, opposite answer. Nobody chose which.", 3000)

        # ============================================== 3. REAL FILINGS
        #
        # The synthetic corpus carries traps built on purpose, which is exactly
        # the ground on which a reviewer discounts it. This pair was published by
        # a filer to a state commission.
        #
        # THE WHOLE MOVEMENT IS UNDER ONE GUARD, decided off camera. Two captions
        # name a Kentucky docket by number and describe its two versions; neither
        # can be said over a page that is not that docket.
        if real_docket_ok:
            cut(page)
            page.goto(f"{BASE}/proceedings/{REAL_DOCKET}", wait_until="networkidle")
            caption(page, "The same rule on filings nobody here wrote.")
            beat(page, 2200, "the real docket")
            caption(
                page,
                "Kentucky PSC 2025-00113, filed then corrected. A million "
                "characters each; they differ by <b>127</b>.",
            )
            beat(page, 3000, "the size of the haystack")

            # The real claim is hand-written and its citation is re-checked like
            # any other, so it can be refused -- scripts/ingest_real.py says so
            # and prints the verdict it got. If it is refused today this beat
            # does not run: a caption saying the edit was found and cited, over a
            # withheld card, would be a false sentence about a true refusal.
            if real_change:
                page.goto(f"{BASE}{real_change}", wait_until="networkidle")
                if centre(page, ".claim--verified", nudge=40):
                    caption(page, "It found the edit and cited it to the character.")
                    beat(page, 2400, "the real change, and the claim on it")

            point(page, "The corpus was not built to pass. A filer published it.", 3000)
        else:
            print("  SKIPPED movement 3: no real docket in this database")

        # ============================================== 4. ROUTING
        #
        # A claim that cannot prove itself is not asserted. A duty nobody
        # confirmed does not get an owner. Same shape, different table.
        cut(page)
        page.goto(f"{BASE}/changes/{HERO_CHANGE}#bears-on", wait_until="networkidle")
        centre(page, "#bears-on", nudge=150)
        caption(page, "One layer out: who has to act on this.")
        beat(page, 2200, "the Obligations section")
        code = routing_code(page)
        print(f"  routing code on screen: {code or 'none read'}")
        caption(page, ROUTING_LINE.get(code, ROUTING_FALLBACK))
        beat(page, 2600, "routing declines to name a person")

        page.goto(f"{BASE}/changes/{MAPPED_CHANGE}#bears-on", wait_until="networkidle")
        # No confirmed row, no beat -- rather than a caption about a name that is
        # not on screen. The search and the framing are the same act here, so
        # the guard IS the scroll: it returns False when there is nothing to put
        # in the middle.
        if centre_confirmed(page, nudge=60):
            caption(page, "Here somebody confirmed it, and the record says who and when.")
            beat(page, 2200, "the mapping a person put their name to")
        else:
            print(f"  SKIPPED: no confirmed mapping on /changes/{MAPPED_CHANGE}")

        # THE LINE COVERS BOTH HALVES, and it has to: this takeaway plays over
        # the change where routing DID name somebody. "It will not name an owner
        # it cannot stand behind" is true of the movement and false of the frame
        # it would be read against, and a viewer reading a caption against the
        # picture in front of them is the only reader there is.
        point(page, "A name appears here only when somebody put it there.", 3000)

        # ============================================== 5. THE QUEUE
        cut(page)
        page.goto(f"{BASE}/escalations", wait_until="networkidle")
        # READ THE ROWS BEFORE SAYING ANYTHING ABOUT THEM. review.html draws
        # "Nothing is held for review." over an empty queue, and the caption
        # below would then be read off a screen saying the opposite.
        codes = page.evaluate(
            "() => Array.from(document.querySelectorAll('.queue__code'))"
            "  .map(el => el.textContent.trim())"
        )
        print(f"  escalation codes on screen: {codes or 'NONE -- the queue beats are cut'}")
        if codes:
            caption(page, "Every refusal lands in one queue, with its reason and its source.")
            beat(page, 2200, "the review queue")
            # The second row is the check nobody expects: the quoted words ARE
            # in the document, three times over, and the citation did not say
            # which one it meant. Only said when both codes are really on screen.
            if {"CITATION_QUOTE_MISMATCH", "CITATION_AMBIGUOUS_OCCURRENCE"} <= set(codes):
                glide(page, 300)
                caption(
                    page,
                    "One quoted words that are not there. One quoted words that "
                    "appear <b>three times</b> and did not say which.",
                )
                beat(page, 2800, "the two shapes of refusal")
            # Said HERE rather than as the takeaway, because it is about the
            # queue and the takeaway plays two screens later. What a reviewer
            # can do to a row is settle it; what nobody can do from this screen
            # is turn a refused claim into a printed one.
            caption(page, "Settling one does not publish the claim. Nothing here can.")
            beat(page, 2400, "what a reviewer can and cannot do")

        page.goto(f"{BASE}/review#coverage", wait_until="networkidle")
        # No lift. MEASURED at 1440x900 this strip sits at 366..533 with the
        # caption band at 794..851 -- 260px of clear air, and a fixed lift is the
        # wrong shape for a strip whose height moves with how many findings the
        # database holds.
        if centre(page, ".coverage"):
            point(page, "What it withheld is counted beside what it kept.", 3000)
        else:
            print("  SKIPPED: no coverage strip, so movement 5 lost its takeaway")

        # ============================================== 6. LOGINS
        #
        # The first of the three questions an enterprise buyer is holding: can I
        # add my people. Under one guard read off camera -- an analyst account
        # gets a rendered 403 here, and three captions over it would be this film
        # narrating a refusal it caused.
        if can_users:
            cut(page)
            # THE ROLE CHANGES HERE AND THE FILM SAYS SO. Everything before this
            # was an analyst. What follows needs user.manage and workflow.manage,
            # which an analyst does not hold. Sliding into it silently would tell
            # a viewer that one account does all of this -- the opposite of what
            # the permission model is for, and the reading an enterprise buyer
            # would most want corrected.
            page.goto(f"{BASE}{ADMIN_INDEX}", wait_until="networkidle")
            caption(page, "From here on: a different person, signed in as an administrator.")
            beat(page, 2600, "the role switch, said out loud")
            centre(page, "table.table", nudge=40)
            caption(page, "The administrative screens, each behind the permission that opens it.")
            beat(page, 2400, "the admin index: every screen names its own code")

            page.goto(f"{BASE}{USERS}", wait_until="networkidle")
            caption(page, "Every account in this workspace, and when each was last used.")
            beat(page, 2200, "the logins screen, at the top")

            # THERE IS NO CEILING HERE AND THE FILM MUST NOT IMPLY ONE.
            # app/state/invites.py:35-42 says it in its own words: the handoff
            # path grants MORE than its inviter holds, because admin carries
            # user.invite while carrying neither action.approve nor
            # action.reject. grant_role has no ceiling either -- its actor is a
            # display string, so there is nothing to compute one against
            # (app/state/identity.py:55-61). An earlier cut of this film claimed
            # "nobody hands on authority they lack" over this very screen. It is
            # false, the repository documents it as false, and asserting it in a
            # film about refusing to overstate would have been the worst
            # sentence in the product.
            if centre(page, "#role", nudge=40):
                caption(page, "Add a colleague and choose their role. Every grant is written to the chain.")
                beat(page, 2200, "the add-a-login form and the ceiling note")

            point(page, "Your people, your roles, and a record of who granted what.", 3000)
        else:
            print("  SKIPPED movement 6: this account cannot open /users")

        # ============================================== 7. PERMISSIONS
        #
        # The second question: can I control what they may do. The register is
        # the argument -- a conflict is named rather than blocked, because a
        # four-person regulatory team may have nobody else to hold both sides and
        # a product that refused would be a product they cannot use.
        if can_permissions:
            cut(page)
            page.goto(f"{BASE}{PERMISSIONS}#matrix", wait_until="networkidle")
            if centre(page, ".perm-matrix", nudge=40):
                caption(page, "Who holds what, and how they came to hold it.")
                beat(page, 2200, "the matrix")
                # Two true sentences; the workspace decides which. A film saying
                # "or granted directly to one person" over a grid with no direct
                # grant in it would be describing a cell that is not there.
                caption(
                    page,
                    "Through a role, or granted directly to one person -- the cell says which."
                    if direct_grants
                    else "Every mark says which role carries it.",
                )
                beat(page, 2200, "what one cell means")

            if centre(page, "#conflicts .reg", nudge=40):
                caption(
                    page,
                    "One person holding both sides of a separation is named here."
                    if conflicts
                    else "Nobody here holds both sides of a declared pair. It looked, and says so.",
                )
                beat(page, 2200, "the conflict register")

            point(page, "You decide who may approve. A conflict is named, never hidden.", 3000)
        else:
            print("  SKIPPED movement 7: this account cannot open /permissions")

        # ============================================== 8. APPROVAL ROUTES
        #
        # The third question: can I shape the approval route to my own
        # organisation. Three screens, and the middle one is only opened when the
        # off-camera pass found step nodes on it -- the canvas is drawn by
        # workflow.js from a JSON block, so an editor that drew nothing is a grey
        # rectangle with a confident caption under it.
        if can_routes:
            cut(page)
            page.goto(f"{BASE}{WORKFLOWS}", wait_until="networkidle")
            caption(
                page,
                "Approval routes are drawn here. This one is live."
                if route_status == "active"
                else "Approval routes are drawn here.",
            )
            beat(page, 2200, "the route list, and the box that opens a new draft")

            if route_url and route_nodes:
                page.goto(f"{BASE}{route_url}", wait_until="networkidle")
                page.wait_for_timeout(700)          # the canvas draws itself
                centre(page, ".wf-canvas", nudge=30)
                caption(
                    page,
                    "Each step: who is asked, how long they have, and what "
                    "happens when that runs out.",
                )
                beat(page, 2600, "the route on the canvas")

            page.goto(f"{BASE}{LIVE_ROUTE}", wait_until="networkidle")
            if centre(page, ".wf-plan .wf-step", nudge=40):
                caption(page, "Everybody signed in can read the live route in plain words.")
                beat(page, 2200, "the read-only route: the people who do not draw it")

            point(page, "The approval route is yours to draw, not ours.", 3000)
        else:
            print("  SKIPPED movement 8: this account cannot open /admin/workflows")

        # ============================================== 9. SECURITY AND ACCESS
        #
        # Filmed on the change page, which is where the close is, so the film
        # comes home rather than making one more journey. Nothing said here is
        # asserted OF this screen except the two things that are true of every
        # screen, and both were measured with curl against the deployed instance
        # before this movement was written -- see the head of this file.
        #
        # THE ACCESSIBILITY LINE CONCEDES BEFORE IT IS ASKED. --ink-3 measures
        # 4.49:1 at its best rendered pixel on the masthead and 3.85:1 at its
        # worst, both under the 4.5:1 normal text needs, and strata.css says so
        # in the comment that carries the sample. No caption here claims AA.
        cut(page)
        page.goto(
            f"{BASE}/changes/{HERO_CHANGE}#claim-{HERO_WITHHELD}",
            wait_until="networkidle",
        )
        centre(page, ".claim--withheld", nudge=70)
        caption(
            page,
            "Every response carries the same headers, redirects included. "
            "Nothing here can be framed.",
        )
        beat(page, 3000, "measured with curl against this instance, not read off the code")

        caption(page, "Tab through it. The focus ring is never taken away.")
        walk_the_focus(page)
        beat(page, 1400, "the ring, walked rather than claimed")

        caption(page, "WCAG 2.2 was swept. Contrast was measured on rendered pixels.")
        beat(page, 2400, "swept, measured, and guarded by a test over every template")

        # THE CONCESSION IS ON SCREEN, not left for the takeaway to imply. Two
        # captions saying the work was done, followed by a line about telling
        # the truth, reads to a viewer as "it all passes" -- and it does not.
        # The film costs itself two seconds here to say which way it fails, and
        # the takeaway after it is then a promise the picture has already kept.
        caption(page, "Some grey text is still short of AA, and the stylesheet says so.")
        beat(page, 2400, "the shortfall, named before anybody asks")

        point(page, "We measured it. Where it falls short, we say so.", 3200)

        # ============================================== 10. CLOSE
        #
        # The ring goes before the last shot. It was the evidence in movement
        # nine and it is a distraction here -- a focused nav link at the top of
        # the frame pulls the eye off the card the film ends on.
        cut(page)
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        centre(page, ".claim--withheld", nudge=70)
        beat(page, 800, "back where it started")
        caption(page, "Edit the cited words and reload. The claim goes. Nothing re-runs.")
        beat(page, 2600, "the thing to remember")
        point(page, "You act on what it proves. It says nothing else.", 3600, "CLOSE")

        ctx.close()
        browser.close()

    made = sorted(OUT.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    spent = sum(_HELD) / 1000
    print(f"\nheld for {spent:.0f}s across {len(_HELD)} shots")
    if spent > CEILING_S:
        print(
            f"OVER THE CEILING: {spent:.0f}s of holds against {CEILING_S}s, and "
            "navigation is on top of that. Take it out of the holds, not the "
            "takeaways."
        )
    print("recorded:", made[-1] if made else "NOTHING")


if __name__ == "__main__":
    main()
