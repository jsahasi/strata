#!/usr/bin/env python3
"""Record one idea: this product refuses to assert what it cannot prove.

ONE IDEA, AND EVERYTHING ELSE IS EVIDENCE FOR IT.

The old cut ran two acts and eighteen screens -- the board, the tour, the
assistant, permissions, logins, invitations, sources, the route editor, the
share register. Every one of those is real and none of them is the argument. A
reviewer watching a tour of features decides inside eight seconds that this is
another wrapper, and after that the good screens are read as chrome on a
wrapper. So the tour is gone. What is left is one claim refusing itself, and
then four widening circles that exist only to show the same refusal is not a
trick played once.

THE ORDER, AND WHY EACH BEAT IS WHERE IT IS.

  1. Cold open on the refusal. No login, no dashboard, no title card. The first
     frame is a claim that will not be made, because its citation quoted "10
     megawatts" and the filing says 20. Held longer than is comfortable. This
     is the only shot that matters; everything after it is corroboration.
  2. The same machinery passing. The claim directly above it on the same page,
     in the same shape, whose words ARE in the source. Same rule, opposite
     answer, nobody chose which. Adjacency is the demonstration -- change.html
     keeps both in one column for exactly this reason and its own comment says
     so.
  3. Filings nobody here wrote. Kentucky PSC 2025-00113, filed then corrected,
     a million characters each and 127 apart. The trap corpus could be accused
     of being built to pass; this one was published by a filer.
  4. The same refusal one layer out. Routing will not name who has to act when
     nobody confirmed the mapping, then the change where somebody did and the
     record says who.
  5. The record. Everything refused in one queue, and the count of what was
     withheld set in the same ink as the count of what was included.
  6. Close on the thing a person remembers: edit the cited words, reload, and
     the claim goes -- with no job in between.

WHAT IS DELIBERATELY NOT FILMED, so nobody re-adds it by accident. The sign-in
door, because a password must not be on screen. The first-visit tour, the
project board, the assistant, permissions, users, invitations, the source
registry, the route editor and the share register -- all real, all a feature
tour, all cut. The hash chain has NO SCREEN in this product (verify_chain is
reached from scripts/backup.py and app/state/replay.py and from nothing a
browser can open), so what stands in for "the audit chain" here is the record
that IS on screen: who confirmed a mapping and when, and who resolved a refusal
and when. Do not caption those as the chain. They are not it.

A CAPTION DOES NOT PLAY UNLESS THE SCREEN HOLDS WHAT IT SAYS, and the check
happens OFF CAMERA wherever it can. This rule was bought. A take was recorded in
which three captions and 10.6 seconds of confident narration ran over FastAPI's
default 404 body -- one line reading {"detail":"no such proceeding"} on a white
page -- because the docket the captions named is not in every database and the
navigation to it was unconditional. The script already knew: its own warm-up
request had come back 404 and it printed that the beat would be skipped, then
filmed the page anyway. Only the two beats after the navigation were guarded.

So the guard sits on the MOVEMENT, not on the beat inside it, and it is decided
before the camera points anywhere. The off-camera pass reports what it found and
the movements read that. A film about refusing to assert what it cannot prove
cannot itself narrate over a page that contradicts it; that is not a rough edge
in the demo, it is the demo arguing against itself.

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
to last. It also discovers the real docket's change id off camera, so no beat on
camera depends on a table scan finding a row.

Run. STRATA_DATABASE_URL DECIDES WHICH DATABASE IS TOUCHED and nothing else
does: `make seed` takes no database argument, and app/state/db.py falls back to
sqlite:///strata.db -- the repo's own -- when the variable is unset. Export it
before the seed, not after, or the seed lands on the committed database.

    mkdir -p /tmp/film
    export STRATA_DATABASE_URL=sqlite:////tmp/film/strata.db
    make seed                          # into that database, not the repo's
    uvicorn app.main:app --port 8111   # against the same one
    python3 scripts/film.py <email> <password>

The seed matters for a second reason. `make seed` runs scripts/ingest_real.py,
which is the only thing that puts the Kentucky docket in a database; without it
movement three has nothing to film and cuts itself. See the off-camera block.

Any signed-in account reaches every screen filmed here; no administrative screen
survives in this cut, so an analyst account is enough.

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
# change the model has not judged yet, each appended to the append-only,
# hash-chained audit log. The old cut also sent a real message to the assistant;
# that beat is gone, so that write is gone with it. Nothing the camera sees
# writes anything at all.
BASE = os.environ.get("STRATA_FILM_BASE", "http://127.0.0.1:8111").rstrip("/")

# Overridable, and created with its parents. The previous default was a
# session-scoped scratch directory that no longer exists, and mkdir without
# parents=True fails on it rather than falling back.
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

# The real pair, from scripts/ingest_real.py.
REAL_DOCKET = "KY-PSC-2025-00113"

# app/web/static/tour.js reads this and stays shut when it is set.
#
# The VALUE matters as much as the name. tour.js checks `preference() === DONE`
# where DONE is the literal "done", so any other value leaves the tour opening on
# every screen that has one -- and several screens have their own. A take was
# recorded with "seen" here and the Review screen's tour sat over the shot.
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


CAPTION_CSS = """
/* THE BACKGROUND HUGS THE WORDS, it is not a bar across the frame.
   A lower-thirds gradient darkens a third of the picture whether or not there
   is text there, and on these screens that third is where the withheld claim
   sits -- the one shot the whole film exists to reach. So the panel itself is
   transparent and the contrast rides on the text: an inline span with padding
   and box-decoration-break:clone, which makes every wrapped line carry its own
   rounded background instead of one box drawn round the whole block. */
#strata-cap{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;
  padding:0 56px 46px;pointer-events:none;text-align:center;
  opacity:0;transition:opacity .28s ease}
#strata-cap.on{opacity:1}
#strata-cap .cap{
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
}
#strata-cap b{color:#ffd479;font-weight:700}
"""


def caption(page, text: str) -> None:
    """Put a line at the foot of the frame. Empty string clears it."""
    page.evaluate(
        """([text, css]) => {
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
            el.innerHTML = '<span class="cap">' + text + '</span>';
            el.classList.add('on');
        }""",
        [text, CAPTION_CSS],
    )


#: Every hold, so the run can print what it spent. Two minutes is the ceiling
#: and a cut that has drifted over it should say so rather than be discovered in
#: a player.
_HELD: list[int] = []


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

    MEASURED, on CHG-v1-v2-004 at 1440x900. Off the heading, the confirmed line
    landed 79px below centre and the caption cut through the middle of a quoted
    passage. Centring the row with a 60px lift puts the line at 403..422 -- near
    centre, well clear of the caption band at 794..851 -- and leaves the caption
    sitting over label chips rather than through a sentence.
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

    Called off camera. A scan that finds nothing costs a skipped beat here and
    would cost a dead shot if it ran during the take.
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

        # Warm every screen the take visits, so the first view of an unjudged
        # change -- the one GET in this product that writes and may call a model
        # -- happens here rather than as dead air mid-shot.
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
        # filmed at all.
        #
        # scripts/ingest_real.py is the only thing that puts this docket in a
        # database, so a database seeded without it answers this GET 404 and
        # FastAPI renders its default JSON body -- there is no HTML error page in
        # this app to catch it. The status is the whole test and it costs nothing
        # here. Asking the recorded pass to work it out instead means asking it
        # after the camera is already pointed at the page, which is how ten
        # seconds of narration got recorded over that JSON body.
        #
        # The table check is the second half: a 200 that drew no version timeline
        # is not a docket page worth three captions either.
        docket = opener.goto(
            f"{BASE}/proceedings/{REAL_DOCKET}", wait_until="networkidle"
        )
        real_docket_ok = bool(docket and docket.ok) and here(opener, "table.table")
        print(
            "  real docket: "
            + ("present" if real_docket_ok else "ABSENT -- movement three is cut")
        )

        # And find its one claimed change, still off camera. A scan that finds
        # nothing costs a skipped beat here; on camera it would cost a dead shot.
        real_change = real_change_url(opener) if real_docket_ok else ""
        print(f"  real change: {real_change or 'none -- those two beats are cut'}")
        if real_change:
            opener.goto(f"{BASE}{real_change}", wait_until="networkidle")

        signed_in = door.storage_state()
        door.close()

        # ------------------------------------------------------ on camera: the take
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

        # ================================================= 1. THE COLD OPEN
        #
        # The first frame is the refusal. The anchor puts the card on screen
        # during load rather than after a scroll the camera would see; centre()
        # then corrects it whatever the anchor did.
        page.goto(
            f"{BASE}/changes/{HERO_CHANGE}#claim-{HERO_WITHHELD}",
            wait_until="networkidle",
        )
        if not centre(page, ".claim--withheld", nudge=70):
            raise SystemExit(
                f"no withheld claim on /changes/{HERO_CHANGE}; the corpus is not "
                "seeded as the film expects and there is no shot to record"
            )

        beat(page, 1600, "COLD OPEN: the card, before a word is said about it")

        caption(page, "A claim was written about this passage. It is not on this page.")
        beat(page, 3800, "the refusal, stated")

        caption(page, "Its citation quoted the filing as <b>10 megawatts</b>.")
        beat(page, 3600, "what the claim said")

        caption(page, "The filing says <b>20</b>. Strata read the source again to check.")
        beat(page, 4400, "what the source says -- HOLD THIS ONE")

        caption(page, "So the claim is not made. The reason takes its place.")
        beat(page, 4200, "the refusal itself")

        caption(page, "Nothing it would have said is printed here, in any field.")
        beat(page, 3800, "and there is no field to print it in")

        # ============================================ 2. THE SAME RULE, PASSING
        #
        # Directly above, in the same column, in the same shape. change.html
        # keeps them adjacent on purpose and refuses to split them under two
        # headings; this beat is why.
        cut(page)
        if not centre(page, ".claim--verified", nudge=40, smooth=True):
            raise SystemExit(
                f"no verified claim on /changes/{HERO_CHANGE}; the pairing the "
                "film is built on is not in this database"
            )
        caption(page, "The claim above it went through the same check.")
        beat(page, 3400, "the verified claim")

        chip = page.locator(".claim--verified .citation-chip").first
        if chip.count():
            chip.click()
            page.wait_for_timeout(500)
            centre(page, ".claim--verified", nudge=40)
            caption(page, "Its words are in the source, at the characters it named.")
            beat(page, 4000, "the source panel, with the cited span marked")
            caption(page, "Read on this request. Not looked up from a stored verdict.")
            beat(page, 4400, "why that matters")

        caption(page, "One rule, both times. It passed one claim and refused the other.")
        beat(page, 4000, "the pairing, said out loud")

        # ================================== 3. FILINGS NOBODY HERE WROTE
        #
        # The synthetic corpus carries traps built on purpose, which is exactly
        # the ground on which a reviewer discounts it. This pair was published by
        # a filer to a state commission.
        #
        # THE WHOLE MOVEMENT IS UNDER ONE GUARD, decided off camera. Three
        # captions name a Kentucky docket by number and describe its two
        # versions; none of them can be said over a page that is not that
        # docket. Guarding only the beats after the navigation -- which is what
        # this did -- leaves the navigation itself and the three captions on it
        # running whatever came back.
        if real_docket_ok:
            cut(page)
            page.goto(f"{BASE}/proceedings/{REAL_DOCKET}", wait_until="networkidle")
            caption(page, "The same rule on filings nobody here wrote.")
            beat(page, 3200, "the real docket")
            caption(page, "Kentucky PSC 2025-00113. Testimony filed, then corrected.")
            beat(page, 3600, "the version timeline")
            caption(page, "About a million characters each. The two differ by <b>127</b>.")
            beat(page, 3800, "the size of the haystack")

            # The real claim is hand-written and its citation is re-checked like
            # any other, so it can be refused -- scripts/ingest_real.py says so
            # and prints the verdict it got. If it is refused today, these two
            # beats do not run: a caption saying "Strata found the edit and cited
            # it" over a withheld card would be a false sentence about a true
            # refusal.
            if real_change:
                page.goto(f"{BASE}{real_change}", wait_until="networkidle")
                if centre(page, ".claim--verified", nudge=40):
                    caption(page, "Strata found the edit and cited it to the character.")
                    beat(page, 3800, "the real change, and the claim on it")
                    if here(page, ".claim--verified .claim__filing a"):
                        centre(page, ".claim--verified .claim__filing", nudge=30)
                        caption(page, "The source link goes to the Commission's copy, not ours.")
                        beat(page, 3800, "the way out to the filing itself")
        else:
            print("  SKIPPED movement 3: no real docket in this database")

        # ================================ 4. THE SAME REFUSAL, ONE LAYER OUT
        #
        # A claim that cannot prove itself is not asserted. A duty nobody
        # confirmed does not get an owner. Same shape, different table.
        cut(page)
        page.goto(f"{BASE}/changes/{HERO_CHANGE}#bears-on", wait_until="networkidle")
        # Enough of a lift to put the routing card in the middle rather than the
        # heading above it.
        centre(page, "#bears-on", nudge=150)
        caption(page, "The same refusal one layer out: who has to act on this.")
        beat(page, 3400, "the Obligations section")
        code = routing_code(page)
        print(f"  routing code on screen: {code or 'none read'}")
        caption(page, ROUTING_LINE.get(code, ROUTING_FALLBACK))
        beat(page, 4200, "routing declines to name a person")

        page.goto(f"{BASE}/changes/{MAPPED_CHANGE}#bears-on", wait_until="networkidle")
        # No confirmed row, no beat -- rather than a caption about a name that is
        # not on screen. The search and the framing are the same act here, so
        # the guard IS the scroll: it returns False when there is nothing to put
        # in the middle.
        if centre_confirmed(page, nudge=60):
            caption(page, "Here somebody did confirm it, and the record says who and when.")
            beat(page, 4000, "the mapping a person put their name to")
        else:
            print(f"  SKIPPED: no confirmed mapping on /changes/{MAPPED_CHANGE}")

        # =========================================== 5. EVERYTHING IT WITHHELD
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
            beat(page, 3400, "the review queue")
            # The second row is the check nobody expects: the quoted words ARE
            # in the document, three times over, and the citation did not say
            # which one it meant. Only said when both codes are really on
            # screen -- a caption describing rows that are not there is the
            # exact fault this film is about.
            both = {"CITATION_QUOTE_MISMATCH", "CITATION_AMBIGUOUS_OCCURRENCE"} <= set(codes)
            if both:
                glide(page, 300)
                caption(
                    page,
                    "One quoted words that are not there. One quoted words that "
                    "appear <b>three times</b> and did not say which.",
                )
                beat(page, 4400, "the two shapes of refusal")
            # "Either" presumes the two shapes above it. Over a queue holding one
            # refusal, or two of the same kind, that word is a small false thing
            # said about the screen -- so the sentence moves with the screen
            # rather than the guard being left off it. The point the sentence
            # carries does not depend on the count and is made either way.
            caption(
                page,
                "Settling either does not publish the claim. Nothing here can."
                if both
                else "Settling it does not publish the claim. Nothing here can.",
            )
            beat(page, 3600, "what a reviewer can and cannot do")

        page.goto(f"{BASE}/review#coverage", wait_until="networkidle")
        # No lift. `nudge` exists to get a card out from under the caption, and
        # MEASURED at 1440x900 this strip sits at 366..533 with the caption band
        # at 794..851 -- 260px of clear air, so there is nothing to lift it out
        # of. The 40px that used to be here only pushed it off centre, and a
        # fixed lift is the wrong shape for a strip whose height moves with how
        # many findings the database holds. Centring holds whatever it grows to.
        if centre(page, ".coverage"):
            caption(page, "And it counts what it withheld, in the same ink as what it kept.")
            beat(page, 4400, "the coverage strip: two numbers, equal weight")

        # ============================================================ 6. CLOSE
        cut(page)
        page.goto(
            f"{BASE}/changes/{HERO_CHANGE}#claim-{HERO_WITHHELD}",
            wait_until="networkidle",
        )
        centre(page, ".claim--withheld", nudge=70)
        beat(page, 900, "back where it started")
        caption(page, "Edit the cited words and reload. The claim goes. Nothing re-runs.")
        beat(page, 4600, "the thing to remember")
        caption(page, "You act on what it proves. It says nothing else.")
        beat(page, 4400, "close")

        ctx.close()
        browser.close()

    made = sorted(OUT.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    print(f"\nheld for {sum(_HELD)/1000:.0f}s across {len(_HELD)} shots")
    print("recorded:", made[-1] if made else "NOTHING")


if __name__ == "__main__":
    main()
