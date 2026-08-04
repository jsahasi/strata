/* The first-visit walkthrough. It points at things, once.
 *
 * NO BUILD STEP, NO FRAMEWORK, NO CDN (ADR-012). One plain script tag, nothing
 * fetched from another host, nothing fetched at all. It works with the machine
 * off the internet, which is what a reviewer running `make run` on a plane gets.
 *
 * IT HOLDS NO WORDS. Every stop -- its target, its title, its sentence -- is in
 * app/web/templates/_tour.html, beside the markup it describes. This file walks
 * that table and does not know what any of it says.
 *
 * FOUR RULES IT EXISTS TO KEEP.
 *
 * 1. A STOP WITH NO TARGET IS SKIPPED, SILENTLY. Not anchored to the corner of
 *    the viewport, which is what every library that does this falls back to. A
 *    person with no projects, or without the permission that draws a control,
 *    gets the stops their screen actually has and nothing else. If no stop
 *    resolves, nothing is drawn AND nothing is remembered -- burning the tour on
 *    a screen that could not show it is the same defect one step later.
 *
 * 2. IN THE MARKUP IS NOT ON THE SCREEN. A hidden element is a real element:
 *    the assistant's own buttons ship hidden and are revealed by clerk.js, and a
 *    permission can hide a control without removing it. So every candidate is
 *    asked for its client rects, and one with none is not a target.
 *
 * 3. THE PAGE IS LEFT AS IT WAS FOUND. The ring is drawn over the target, never
 *    on it. Nothing here sets a style on, adds a class to, or moves an element
 *    of the screen it is describing, so there is no restore step to get wrong.
 *
 * 4. IT NEVER COMES BACK. Dismissed or finished, it writes the preference and
 *    is done. A walkthrough that reappears is worse than none. The way back is
 *    deliberate and is printed on the tip itself: ?tour=1 on any address.
 *
 * WHAT IT DOES NOT DO. It does not trap focus. The tip is a dialog that does not
 * block the page (aria-modal="false") -- the reader can leave it by tabbing, and
 * a trap here would be a cage around a thing nobody asked for. Focus does move
 * to each step, which is the part a keyboard reader needs, Tab reaches the
 * buttons, Enter works them, and Escape ends the run from any step. Focus then
 * goes back where it came from; today that is the document itself, because the
 * tour opens on page load and nothing in the page had focus to lend it.
 */
(function () {
  "use strict";

  /* THE COOKIE IS A PREFERENCE, NOT A CREDENTIAL, and it must not be mistaken
     for one. app/auth/sessions.py issues a bearer token: HttpOnly, so script
     cannot read it, and one cross-site scripting bug does not hand over a
     session. This holds the answer to "have you seen this yet". Script has to
     read it, so HttpOnly is impossible here -- which is exactly why the two may
     not share a name, a lifetime, or a reader. Nothing in this file names the
     session cookie.

     SameSite=Lax and Path=/ for the same reasons the session cookie has them.
     Secure is conditional, and that is deliberate: on plain http a browser
     drops a Secure cookie without a word, the preference would never be stored,
     and the tour would open on every single page load forever. app/web/deps.py
     takes Secure off for plain http on this machine for the same reason and
     says so on the login page; this says so in the console. */
  var COOKIE = "strata_tour";
  var DONE = "done";
  var LIFE_SECONDS = 31536000; /* 365 days. Long enough not to be a nuisance. */
  var REPLAY = "tour=1";

  /* Distance from the target to the tip, and how far the ring stands off the
     thing it is drawn around. Pixels, because both are optical, not typographic. */
  var GAP = 12;
  var RING_PAD = 6;

  function trim(words) {
    return (words || "").replace(/^\s+|\s+$/g, "");
  }

  var root = document.querySelector("[data-tour]");
  if (!root) {
    return;
  }

  /* Every node this file binds to, named once and checked once -- the same
     shape clerk.js uses. A template edit that drops one element would otherwise
     throw at the first line that touches it, and the walkthrough would simply
     not exist on that build with nothing anywhere saying why. */
  var missing = [];

  function need(selector) {
    var node = root.querySelector(selector);
    if (!node) {
      missing.push(selector);
    }
    return node;
  }

  var ring = need("[data-tour-ring]");
  var tip = need("[data-tour-tip]");
  var stepLine = need("[data-tour-step]");
  var titleLine = need("[data-tour-title]");
  var bodyLine = need("[data-tour-body]");
  var back = need("[data-tour-back]");
  var next = need("[data-tour-next]");
  var dismiss = need("[data-tour-end]");

  if (missing.length) {
    if (window.console && window.console.error) {
      window.console.error(
        "the walkthrough is missing " + missing.join(", ") +
          ": _tour.html and tour.js have gone out of step, and it is switched " +
          "off rather than half drawn"
      );
    }
    return;
  }

  /* --------------------------------------------------------- the preference */

  /* An exact name. A prefix test would read strata_tour_anything as this one,
     and a substring test would read half the cookie jar. */
  function preference() {
    var jar = (document.cookie || "").split(";");
    for (var i = 0; i < jar.length; i++) {
      var cut = jar[i].indexOf("=");
      if (cut === -1) {
        continue;
      }
      if (trim(jar[i].slice(0, cut)) === COOKIE) {
        return trim(jar[i].slice(cut + 1));
      }
    }
    return "";
  }

  function remember() {
    var parts = [
      COOKIE + "=" + DONE,
      "Path=/",
      "Max-Age=" + LIFE_SECONDS,
      "SameSite=Lax"
    ];
    if (window.location.protocol === "https:") {
      parts.push("Secure");
    } else if (window.console && window.console.warn) {
      /* The fallback announces itself rather than degrading in silence. */
      window.console.warn(
        "the walkthrough preference is being stored without Secure because " +
          "this page is not on https; a Secure cookie would be dropped here " +
          "and the walkthrough would open on every visit"
      );
    }
    document.cookie = parts.join("; ");
  }

  function asked() {
    var query = (window.location.search || "").replace(/^\?/, "").split("&");
    for (var i = 0; i < query.length; i++) {
      if (query[i] === REPLAY) {
        return true;
      }
    }
    return false;
  }

  if (!asked() && preference() === DONE) {
    return;
  }

  /* ------------------------------------------------------------- the stops */

  var table = document.querySelector("[data-tour-stops]");
  var stops = null;
  var why = "";
  if (!table) {
    why = "there is no [data-tour-stops] block on the page";
  } else {
    try {
      stops = JSON.parse(table.textContent);
    } catch (problem) {
      why = "the stop table did not parse as JSON: " + problem.message;
    }
  }
  if (!stops || !stops.length) {
    if (window.console && window.console.error) {
      window.console.error(
        "the walkthrough has no stops and is switched off: " +
          (why || "the stop table is empty") +
          ". They are carried by _tour.html."
      );
    }
    return;
  }

  /* Rule 2. In the markup is not on the screen: display:none, the hidden
     attribute, a zero-sized box and a collapsed ancestor all answer here. */
  function onScreen(node) {
    return !!node && node.getClientRects().length > 0;
  }

  function textOf(node) {
    return trim((node.textContent || "").replace(/\s+/g, " "));
  }

  function target(variant) {
    for (var i = 0; i < variant.find.length; i++) {
      var found;
      try {
        found = document.querySelectorAll(variant.find[i]);
      } catch (problem) {
        /* A selector this browser cannot read is a defect in the stop table,
           and it says so rather than quietly behaving like a missing target. */
        if (window.console && window.console.error) {
          window.console.error(
            "the walkthrough cannot read the selector " + variant.find[i]
          );
        }
        continue;
      }
      for (var j = 0; j < found.length; j++) {
        var node = found[j];
        /* The tour may not point at itself. */
        if (root.contains(node) || !onScreen(node)) {
          continue;
        }
        if (variant.text && textOf(node).indexOf(variant.text) !== 0) {
          continue;
        }
        return node;
      }
    }
    return null;
  }

  /* Rule 1. The run is built before anything is drawn, so the counter can say
     "2 of 3" and mean it. A stop whose variants all miss is not in the run at
     all -- it is not shown empty, and it does not take a number. */
  var run = [];
  var s;
  var v;
  for (s = 0; s < stops.length; s++) {
    for (v = 0; v < stops[s].variants.length; v++) {
      var spot = target(stops[s].variants[v]);
      if (spot) {
        run.push({ node: spot, variant: stops[s].variants[v] });
        break;
      }
    }
  }

  /* Nothing on this screen to point at. Draw nothing, and remember nothing:
     the next screen the reader opens gets its turn. */
  if (!run.length) {
    return;
  }

  /* ---------------------------------------------------------- the drawing */

  var at = 0;
  var came = document.activeElement;

  function place(node) {
    var box = node.getBoundingClientRect();

    ring.style.top = box.top - RING_PAD + "px";
    ring.style.left = box.left - RING_PAD + "px";
    ring.style.width = box.width + RING_PAD * 2 + "px";
    ring.style.height = box.height + RING_PAD * 2 + "px";

    /* Measured after the tip is on the screen, because a hidden box has no
       size and a tip placed from a size of zero lands in the corner. */
    var wide = tip.offsetWidth;
    var tall = tip.offsetHeight;
    var room = document.documentElement.clientHeight;
    var across = document.documentElement.clientWidth;
    var top;
    var left;

    /* Four places, tried in order, and the order is about not covering the
       thing being pointed at. Below and above are the readable ones. Beside is
       what a tall target needs -- a withheld claim can be most of the column,
       and a tip clamped into the viewport over it hides the very sentence the
       step is about. */
    if (box.bottom + GAP + tall + GAP <= room) {
      top = box.bottom + GAP;
      left = box.left;
    } else if (box.top - GAP - tall >= GAP) {
      top = box.top - GAP - tall;
      left = box.left;
    } else if (across - box.right - GAP - wide >= GAP) {
      top = box.top;
      left = box.right + GAP;
    } else if (box.left - GAP - wide >= GAP) {
      top = box.top;
      left = box.left - GAP - wide;
    } else {
      /* A target as big as the screen. Sit below it and accept the overlap: a
         tip over part of a tall target still reads, and a tip off the bottom of
         the screen does not. The clamp below is what makes that true. */
      top = box.bottom + GAP;
      left = box.left;
    }

    tip.style.top = Math.max(GAP, Math.min(top, room - tall - GAP)) + "px";
    tip.style.left = Math.max(GAP, Math.min(left, across - wide - GAP)) + "px";
  }

  function show(index) {
    at = index;
    var item = run[index];

    stepLine.textContent = index + 1 + " of " + run.length;
    titleLine.textContent = item.variant.title;
    bodyLine.textContent = item.variant.body;
    back.hidden = index === 0;
    next.textContent = index === run.length - 1 ? "Done" : "Next";

    root.hidden = false;
    ring.hidden = false;
    tip.hidden = false;

    /* Instant, never smooth. Movement is what a reader who asked for none is
       asking against, and a measurement taken during a smooth scroll is a
       measurement of where the target used to be. */
    item.node.scrollIntoView({
      block: "center",
      inline: "nearest",
      behavior: "instant"
    });
    place(item.node);
    tip.focus();
  }

  function end() {
    remember();
    tip.hidden = true;
    ring.hidden = true;
    root.hidden = true;
    window.removeEventListener("resize", again);
    window.removeEventListener("scroll", again);
    window.removeEventListener("load", again);

    /* Focus goes back where it was, if there was anywhere. Today there is not:
       this runs on page load, so the thing focused when it opened is the body,
       and hiding a focused element hands focus back to the document -- which is
       where the reader was. The check is here for the day something in the page
       starts the tour, when handing focus back to that control is the whole
       difference between a walkthrough and a trap. */
    if (came && came !== document.body && document.contains(came) && onScreen(came)) {
      came.focus();
    }
  }

  var pending = false;

  /* The tip and the ring are placed in viewport coordinates, so anything that
     moves the viewport under them has to move them too. Once per frame, because
     scroll fires far more often than a browser paints. */
  function again() {
    if (pending || tip.hidden) {
      return;
    }
    pending = true;
    var frame = window.requestAnimationFrame;
    var redraw = function () {
      pending = false;
      if (!tip.hidden) {
        place(run[at].node);
      }
    };
    if (frame) {
      frame(redraw);
    } else {
      redraw();
    }
  }

  back.addEventListener("click", function () {
    if (at > 0) {
      show(at - 1);
    }
  });

  next.addEventListener("click", function () {
    if (at < run.length - 1) {
      show(at + 1);
    } else {
      end();
    }
  });

  dismiss.addEventListener("click", end);

  /* Escape ends it, from any step, and it counts as having seen it -- the same
     rule citation.js and clerk.js keep for the panels they open. */
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !tip.hidden) {
      end();
    }
  });

  window.addEventListener("resize", again);
  window.addEventListener("scroll", again);

  /* And once more when the page has finished loading. A deferred script runs
     before the last image has arrived, and a target that moves after it was
     measured leaves the ring drawn around where it used to be. Measuring again
     at load costs one frame and closes the whole class. */
  window.addEventListener("load", again);

  show(0);
})();
