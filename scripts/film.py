#!/usr/bin/env python3
"""Record the product doing the work, then record who is allowed to change it.

TWO ACTS, AND THE ORDER IS THE ARGUMENT.

Act one is the analyst's day, and it still spends its longest beat on one thing:
a claim refusing to assert itself because its citation did not verify. That is
the product, and no amount of feature coverage replaces it.

Act two is the administrative half -- permissions, logins, sources, the approval
route, the share register. It comes second on purpose. A reviewer who meets the
admin screens first reads this as configuration software; a reviewer who meets
the refusal first reads the admin screens as the controls around a thing that
already refuses. Same screens, different product.

Everything on screen is the real application against a seeded database. No
mockups, no slides, no narration over stills.

THE FIRST-VISIT TOUR HAS TO BE HANDLED, and forgetting it cost a take. There is
nothing wrong with the tour -- orienting somebody on their first login is the
right thing to do, and it gets a beat of its own here because a reviewer should
see it exists. The problem is only that this recorder starts with a clean browser
profile every single time, so without handling it the tour opens over the first
screen and sits on top of the shot the whole film is built to reach. So: show it
once, then set the cookie it reads, rather than clicking through four steps on
camera.

Run:
    make seed                          # into a scratch database
    uvicorn app.main:app --port 8111   # against that database
    python3 scripts/film.py <admin-email> <password>

Sign in as an ADMINISTRATOR: act two needs the administrative screens, and an
admin can reach every analyst screen as well, so one session films both.

Needs playwright in the SYSTEM python, not the project virtualenv. The product
has no browser dependency and must not grow one for the sake of a video.
"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8111"
OUT = pathlib.Path("/private/tmp/claude-501/-Users-jayeshsahasi-github-strata/d55eeea2-8bd9-4f27-a26d-3161a77f7384/scratchpad/film")
W, H = 1440, 900

# The change carrying one claim that verifies and one that does not. That pairing
# is the entire point of the recording, so it is named here rather than found by
# clicking around and hoping.
CHANGE = "CHG-v1-v2-003"

# app/web/static/tour.js reads this and stays shut when it is set.
TOUR_COOKIE = "strata_tour"



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


def beat(page, ms: int, why: str) -> None:
    """Hold a shot. The comment is the storyboard."""
    print(f"  {ms/1000:>4.1f}s  {why}")
    page.wait_for_timeout(ms)


def glide(page, to: int, steps: int = 30) -> None:
    """Scroll in small steps. One jump reads as a cut and loses the reader."""
    for i in range(steps):
        page.mouse.wheel(0, to / steps)
        page.wait_for_timeout(16)


def main() -> None:
    email, password = sys.argv[1], sys.argv[2]
    OUT.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(OUT),
            record_video_size={"width": W, "height": H},
            device_scale_factor=2,          # retina, so text is not mush
            reduced_motion="no-preference",
        )
        page = ctx.new_page()

        # Sign in off-camera. The login screen is not what anyone needs to see.
        page.goto(f"{BASE}/login", wait_until="networkidle")
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("networkidle")

        # =================================================== ACT ONE: the work

        # 0. The tour gets one beat, because it is a real feature and somebody's
        #    first login should not drop them in cold. Then it is dismissed by
        #    setting the cookie it reads, rather than clicking four Next buttons
        #    on camera. See the module docstring for why this is here at all.
        page.goto(f"{BASE}/projects", wait_until="networkidle")
        page.wait_for_timeout(1000)
        if page.locator("[data-tour]").count():
            caption(page, "A first-time reader is walked through it once")
            beat(page, 2600, "the first-visit tour")
        ctx.add_cookies([{"name": TOUR_COOKIE, "value": "seen", "url": BASE}])
        caption(page, "")
        page.goto(f"{BASE}/projects", wait_until="networkidle")
        page.wait_for_timeout(300)

        # 1. The board. What a regulatory affairs analyst has in hand.
        caption(page, "Every line of work, and what is still open on it")
        beat(page, 3200, "the project board")
        glide(page, 320)
        beat(page, 1500, "the cards, with counts and next re-run")

        # 2. Two corpora: an invented docket carrying deliberate traps, and a
        #    real Kentucky filing pair the commission published itself.
        page.goto(f"{BASE}/proceedings", wait_until="networkidle")
        caption(page, "Two corpora. One invented with traps in it, one real and public.")
        beat(page, 3400, "the proceedings list")

        # 3. The change. The diff between two versions.
        page.goto(f"{BASE}/changes/{CHANGE}", wait_until="networkidle")
        caption(page, "What moved between them, down to the character")
        beat(page, 3000, "what changed between v1 and v2")
        glide(page, 260)
        caption(page, "Each claim shows the words it rests on")
        beat(page, 3200, "a claim, and the citation behind it")

        # 4. THE SHOT. Every other beat exists to reach this one.
        card = page.locator(".claim--withheld").first
        card.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'smooth'})")
        page.wait_for_timeout(700)
        caption(page, "This one does not. Source says <b>20 MW</b>. The citation said <b>10</b>.")
        beat(page, 3600, "THE SHOT part one: the mismatch")
        caption(page, "So it is <b>not asserted</b>. The reason takes its place.")
        beat(page, 3600, "THE SHOT part two: the refusal")

        # 5. Everything refused, in one queue.
        page.goto(f"{BASE}/escalations", wait_until="networkidle")
        caption(page, "Everything held back, in one queue, with the reason")
        beat(page, 3400, "the queue of refusals")

        # 6. The approval route: who signs, how long, and what the clock does.
        page.goto(f"{BASE}/workflow", wait_until="networkidle")
        caption(page, "Who has to approve it, and how long they have")
        beat(page, 3400, "the approval route")
        glide(page, 300)
        caption(page, "Run out of time and it escalates — or is <b>skipped, not approved</b>")
        beat(page, 3600, "the timeout behaviours")

        # 7. Steering: a person redirecting the system, on the record.
        page.goto(f"{BASE}/review", wait_until="networkidle")
        caption(page, "Everything a project rests on, and what it withheld")
        beat(page, 3000, "the review screen")
        steer = page.locator("textarea[name='instruction'], input[name='instruction']").first
        if steer.count():
            steer.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            caption(page, "Disagree with it? <b>Steer it</b> — the instruction goes on the record")
            steer.click()
            steer.type("Treat load-forecast changes as material this quarter.", delay=30)
            beat(page, 2400, "typing a steer directive")

        # 8. Clarke. A real model call against the real record.
        page.goto(f"{BASE}/projects", wait_until="networkidle")
        caption(page, "Or just ask")
        page.click("[data-clerk-open]")
        page.locator("[data-clerk-input]").first.wait_for(state="visible", timeout=10000)
        page.wait_for_timeout(800)
        beat(page, 1200, "the assistant opens")
        box = page.locator("[data-clerk-input]").first
        box.click()
        box.type("What is withheld and why?", delay=32)
        beat(page, 800, "the question")
        caption(page, "It answers from the record, and counts what it withheld")
        page.click("[data-clerk-send]")
        try:
            page.wait_for_function(
                "() => document.querySelector('[data-clerk-transcript]')"
                "  && document.querySelector('[data-clerk-transcript]').innerText.length > 260",
                timeout=25000,
            )
        except Exception:
            pass
        beat(page, 5000, "Clarke answers")
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # ============================================ ACT TWO: who may change it

        page.goto(f"{BASE}/admin", wait_until="networkidle")
        caption(page, "Now the other half: <b>who is allowed to change any of it</b>")
        beat(page, 3400, "the administration index")

        # 9. Permissions. The screen the whole role argument rests on.
        page.goto(f"{BASE}/permissions", wait_until="networkidle")
        caption(page, "Any permission to any person. A role is a shortcut, not a cage.")
        beat(page, 3600, "the permission grid")
        glide(page, 320)
        caption(page, "A clash of duties is <b>named, not forbidden</b>. Small teams are real.")
        beat(page, 3600, "the conflict register")

        # 10. Accounts and invitations.
        page.goto(f"{BASE}/users", wait_until="networkidle")
        caption(page, "Accounts, and when each one last signed in")
        beat(page, 3200, "the logins screen")
        page.goto(f"{BASE}/admin/invites", wait_until="networkidle")
        caption(page, "An invitation lasts 24 hours. A resend supersedes the old one.")
        beat(page, 3200, "the invitation register")

        # 11. Where the filings come from.
        page.goto(f"{BASE}/admin/sources", wait_until="networkidle")
        caption(page, "Where the filings come from, and which kinds can actually fetch")
        beat(page, 3600, "the source registry")

        # 12. The route editor. Drawing the thing act one only showed.
        page.goto(f"{BASE}/admin/workflows", wait_until="networkidle")
        caption(page, "The route is drawn here, not hard-coded")
        beat(page, 3600, "the approval route editor")

        # 13. The share register. Every link, and every time it was opened.
        page.goto(f"{BASE}/admin/shares", wait_until="networkidle")
        caption(page, "Every citation shared outside, and every time it was opened")
        beat(page, 3400, "the share register")

        # 14. Rest on the board.
        page.goto(f"{BASE}/projects", wait_until="networkidle")
        caption(page, "You act on what it proves. It says nothing else.")
        beat(page, 2800, "close")

        ctx.close()
        browser.close()

    made = sorted(OUT.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    print("\nrecorded:", made[-1] if made else "NOTHING")


if __name__ == "__main__":
    main()
