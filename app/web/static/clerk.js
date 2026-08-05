/* The assistant dock. Opens a panel, sends a turn, renders what came back.
 *
 * NO BUILD STEP, NO FRAMEWORK, NO CDN, NO WEB FONT (ADR-012). One plain script
 * tag, no module graph, nothing fetched from another host. It works with the
 * machine off the internet, which is what a reviewer running `make run` on a
 * plane gets.
 *
 * THREE RULES THIS FILE EXISTS TO KEEP.
 *
 * 1. A WITHHELD ITEM NEVER RENDERS LIKE AN ANSWER. The count comes back on the
 *    wire and is drawn with .withheld, .withheld__label and .withheld__reason
 *    -- the same hatch, dashed rule and amber the review queue already teaches
 *    -- with a link to that queue. It is never folded into the reply sentence,
 *    and never omitted because it was zero-ish.
 *
 * 2. AN ABSENT COUNT IS NOT ZERO. The server refuses to send a turn without
 *    one. If some future build does anyway, this panel says the coverage of the
 *    answer is unknown rather than drawing a complete-looking reply. Absence is
 *    denial on the client too.
 *
 * 3. EVERY STRING FROM THE SERVER GOES IN AS TEXT. textContent, never
 *    innerHTML. A reply is prose that passed through a model, and a model that
 *    has read a filing has read whatever that filing contained.
 *
 * WHAT IT DOES NOT DO. It keeps no transcript of its own: the record is the
 * chat_messages table, and a page reload starts the panel empty rather than
 * replaying a copy the browser kept. It holds no session id -- the server knows
 * whose conversation this is from the cookie, and an id the browser could send
 * is an id the browser could send somebody else's.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-clerk]");
  if (!root) {
    return;
  }

  var TURN_URL = root.getAttribute("data-clerk-turn");
  var OPENING_URL = root.getAttribute("data-clerk-opening");
  var THUMB_URL = root.getAttribute("data-clerk-thumb");
  var BUG_URL = root.getAttribute("data-clerk-bug");
  var REVIEW_URL = root.getAttribute("data-clerk-review");

  /* Every node this file binds to, named once and checked once.
   *
   * Without this, a template edit that drops one element throws a TypeError at
   * the first line that touches it, the rest of the file never runs, and the
   * controls stay hidden with nothing anywhere saying why -- the dock would
   * simply not exist on that build. A missing element is a wiring defect, so it
   * is reported where wiring defects are read, and the panel refuses to half
   * work rather than presenting buttons that do nothing.
   */
  function need(selector) {
    var node = root.querySelector(selector);
    if (!node) {
      missing.push(selector);
    }
    return node;
  }

  var missing = [];
  var launch = need("[data-clerk-launch]");
  var dock = need(".clerk-dock");
  var bugSheet = need(".clerk-bug");
  var transcript = need("[data-clerk-transcript]");
  var pillRow = need("[data-clerk-pills]");
  var form = need("[data-clerk-form]");
  var input = need("[data-clerk-input]");
  var send = need("[data-clerk-send]");
  var opener = need("[data-clerk-open]");
  var closer = need("[data-clerk-close]");
  var bugOpen = need("[data-clerk-bug-open]");
  var bugClose = need("[data-clerk-bug-close]");
  var bugForm = need("[data-clerk-bug-form]");
  var bugSaid = need("[data-clerk-bug-said]");

  if (missing.length) {
    if (window.console && window.console.error) {
      window.console.error(
        "the assistant dock is missing " + missing.join(", ") +
          ": _clerk.html and clerk.js have gone out of step, and the panel is " +
          "switched off rather than half wired"
      );
    }
    return;
  }

  var opened = false;
  var busy = false;

  /* The screen the person is on, captured rather than asked for. The path is
     the one name that is always true and always available; the server cuts it
     to the width of the column. */
  var surface = (window.location.pathname || "/").slice(0, 64);

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function show(node, visible) {
    node.hidden = !visible;
  }

  /* An expired session is the failure this function is shaped around. The
     guard in app/web/deps.py answers a request with no session by redirecting
     to the login page, fetch follows redirects, and what comes back is a 200
     carrying HTML. Parsed as JSON that throws something about an unexpected
     token, and the person reads a syntax error where they should read "sign in
     again". So the content type is checked before the body is trusted. */
  function readJson(response) {
    var kind = response.headers.get("content-type") || "";
    if (kind.indexOf("json") === -1) {
      throw new Error("your session has ended -- sign in again");
    }
    return response.json();
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) {
        return Promise.resolve()
          .then(function () {
            return readJson(response);
          })
          .catch(function () {
            return {};
          })
          .then(function (payload) {
            var problem = new Error(payload.detail || "that did not go through");
            problem.status = response.status;
            throw problem;
          });
      }
      return readJson(response);
    });
  }

  /* ------------------------------------------------------------ the launch */

  /* base.html offers a slot in the masthead. When it does, the controls belong
     there, beside the company name, where a person looks for the tools of a
     screen. When it does not, they pin to the corner instead -- which works,
     and is not what was intended, so it says so once in the console rather than
     moving somewhere unexpected in silence. */
  function place() {
    var slot = document.querySelector("[data-clerk-slot]");
    if (slot) {
      slot.appendChild(launch);
    } else {
      launch.className += " clerk-launch--floating";
      if (window.console && window.console.warn) {
        window.console.warn(
          "no [data-clerk-slot] in the masthead: the assistant controls are " +
            "pinned to the corner of the viewport instead of sitting in it"
        );
      }
    }
    show(launch, true);
  }

  /* ----------------------------------------------------------- the opening */

  function opening() {
    return fetch(OPENING_URL, { credentials: "same-origin" }).then(function (
      response
    ) {
      if (!response.ok) {
        throw new Error("the opening could not be read");
      }
      /* Through the same reader as everything else: an expired session comes
         back as the login page with a 200 on it. */
      return readJson(response);
    });
  }

  function nameEverything(payload) {
    var slots = root.querySelectorAll("[data-clerk-name]");
    var i;
    for (i = 0; i < slots.length; i++) {
      slots[i].textContent = payload.name;
    }
    var tagline = root.querySelector("[data-clerk-tagline]");
    if (tagline) {
      tagline.textContent = payload.tagline;
    }
  }

  /* ------------------------------------------------------------- the turns */

  /* A model writes markdown whether or not you asked it to, and this panel was
     printing the asterisks. The rule at the head of this file still holds and is
     the reason the bug existed: every string from the server goes in as TEXT,
     because a reply that passed through a model can be talked into carrying
     markup. So this renders a small, fixed subset by BUILDING NODES -- textContent
     for every character that came from the server, innerHTML nowhere. A model
     that emits <script> gets a paragraph reading "<script>".

     The subset is deliberately short: bold, inline code, unordered lists,
     paragraphs. Not links -- a link whose href came from a model is a place to
     send somebody, and this product does not let a model choose that. Not
     headings, not tables, not images. Anything unrecognised stays as the plain
     text it arrived as, which is the safe direction to fail. */

  var STRONG = /\*\*([^*]+)\*\*/g;
  var CODE = /`([^`]+)`/g;

  /* One line of inline markup into text nodes and elements. */
  function inline(parent, text) {
    var rest = String(text);
    // Code first: a backtick span may legitimately contain asterisks.
    var parts = rest.split(CODE);
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        parent.appendChild(el("code", null, parts[i]));
        continue;
      }
      var bits = parts[i].split(STRONG);
      for (var j = 0; j < bits.length; j++) {
        if (!bits[j]) {
          continue;
        }
        if (j % 2 === 1) {
          parent.appendChild(el("strong", null, bits[j]));
        } else {
          parent.appendChild(document.createTextNode(bits[j]));
        }
      }
    }
  }

  function addTurn(kind, text) {
    var turn = el("div", "clerk-turn clerk-turn--" + kind);
    var body = el("div", "clerk-turn__body");
    var lines = String(text === undefined || text === null ? "" : text).split("\n");
    var list = null;

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].replace(/\s+$/, "");
      var bullet = /^\s*[-*]\s+(.*)$/.exec(line);

      if (bullet) {
        if (!list) {
          list = el("ul", "clerk-turn__list");
          body.appendChild(list);
        }
        var item = el("li");
        inline(item, bullet[1]);
        list.appendChild(item);
        continue;
      }

      list = null;
      if (!line.replace(/^\s+/, "")) {
        continue;
      }
      var para = el("p");
      inline(para, line);
      body.appendChild(para);
    }

    if (!body.childNodes.length) {
      body.appendChild(el("p", null, text));
    }

    turn.appendChild(body);
    transcript.appendChild(turn);
    transcript.scrollTop = transcript.scrollHeight;
    return turn;
  }

  /* One quiet line naming what the turn consulted. A compliance product owes a
     person the provenance of an answer; it does not owe them a headline. */
  function addUsed(turn, used) {
    if (!used || !used.length) {
      return;
    }
    var names = used.map(function (entry) {
      return entry.tool + " (" + entry.found + ")";
    });
    turn.appendChild(el("p", "clerk-used", "Consulted: " + names.join(", ")));
  }

  /* The one thing this panel must never get wrong. A withheld item is drawn in
     the treatment the app already uses for one, with the reason in the position
     a statement would have occupied, and a way to the queue that holds it. */
  function addWithheld(turn, label, reason) {
    var slot = el("div", "withheld");
    slot.appendChild(el("p", "withheld__label", label));
    var line = el("p", "withheld__reason", reason + " ");
    var link = el("a", null, "Open the review queue");
    link.href = REVIEW_URL;
    line.appendChild(link);
    slot.appendChild(line);
    turn.appendChild(slot);
  }

  function addCoverage(turn, withheld) {
    if (typeof withheld !== "number" || withheld < 0) {
      /* Rule 2. Nobody counted, so nobody may report full coverage. */
      addWithheld(
        turn,
        "Coverage unknown",
        "This turn did not say how much it left out, so the answer above may " +
          "not be the whole of it. Nothing here has been checked against the " +
          "record."
      );
      return;
    }
    if (withheld === 0) {
      return;
    }
    addWithheld(
      turn,
      withheld === 1 ? "1 claim withheld" : withheld + " claims withheld",
      "Their citations did not verify, so the statements are not shown here " +
        "and not summarised above. The reasons are on the queue."
    );
  }

  /* -------------------------------------------------------------- thumbs */

  /* A thumb, drawn rather than fetched. The word stays as the accessible name
     and as the tooltip: an icon alone is a guess, and "was this useful" is not
     a question worth making somebody guess at. Built with createElementNS
     because an SVG element created with createElement is an unknown HTML tag
     that renders as nothing -- a silent, invisible failure. */
  function thumb(direction, words) {
    var button = el("button", "clerk-mark clerk-mark--" + direction);
    button.type = "button";
    button.title = words;
    button.setAttribute("aria-label", words);

    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "15");
    svg.setAttribute("height", "15");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");

    var path = document.createElementNS(NS, "path");
    path.setAttribute(
      "d",
      "M6.1 14.5H4.2A1.2 1.2 0 0 1 3 13.3V8.2A1.2 1.2 0 0 1 4.2 7h1.9v7.5Z" +
        "M7.4 7 10 1.9a1.1 1.1 0 0 1 2.1.5v3h2.1a1.2 1.2 0 0 1 1.2 1.4l-.9 5" +
        "a1.2 1.2 0 0 1-1.2 1H7.4V7Z"
    );
    path.setAttribute("fill", "currentColor");
    svg.appendChild(path);

    // One glyph, rotated for the other. Two paths would drift apart.
    if (direction === "down") {
      svg.setAttribute("transform", "rotate(180)");
    }
    button.appendChild(svg);
    button.appendChild(el("span", "clerk-mark__word", words));
    return button;
  }

  function addMarks(turn, messageId) {
    var marks = el("div", "clerk-turn__marks");
    var up = thumb("up", "Useful");
    var down = thumb("down", "Not useful");
    var said = el("p", "clerk-said", "");
    said.setAttribute("role", "status");
    up.type = "button";
    down.type = "button";
    up.setAttribute("aria-pressed", "false");
    down.setAttribute("aria-pressed", "false");

    function settle(button, words) {
      button.setAttribute("aria-pressed", "true");
      up.disabled = true;
      down.disabled = true;
      said.textContent = words;
    }

    /* Up records silently: one request, no box, no thanks-for-your-feedback. */
    up.addEventListener("click", function () {
      post(THUMB_URL, { message_id: messageId, rating: "up" })
        .then(function () {
          settle(up, "Recorded.");
        })
        .catch(function (problem) {
          said.textContent = "Not recorded: " + problem.message;
        });
    });

    /* Down opens a box. The server freezes the exchange from its own rows, so
       nothing typed here decides what the reviewer will see it was about. */
    down.addEventListener("click", function () {
      if (turn.querySelector(".clerk-comment")) {
        return;
      }
      var box = el("div", "clerk-comment");
      var label = el(
        "label",
        "clerk-comment__label",
        "What was wrong with it? Optional, and it helps."
      );
      var field = el("textarea", "clerk-comment__field");
      field.rows = 3;
      label.htmlFor = field.id = "clerk-comment-" + messageId;
      var actions = el("div", "clerk-comment__actions");
      var confirm = el("button", "btn btn--primary", "Send");
      var cancel = el("button", "btn", "Cancel");
      confirm.type = "button";
      cancel.type = "button";

      confirm.addEventListener("click", function () {
        post(THUMB_URL, {
          message_id: messageId,
          rating: "down",
          comment: field.value
        })
          .then(function () {
            box.parentNode.removeChild(box);
            settle(down, "Recorded, with what you wrote.");
          })
          .catch(function (problem) {
            said.textContent = "Not recorded: " + problem.message;
          });
      });
      cancel.addEventListener("click", function () {
        box.parentNode.removeChild(box);
      });

      actions.appendChild(confirm);
      actions.appendChild(cancel);
      box.appendChild(label);
      box.appendChild(field);
      box.appendChild(actions);
      turn.appendChild(box);
      field.focus();
    });

    marks.appendChild(up);
    marks.appendChild(down);
    turn.appendChild(marks);
    turn.appendChild(said);
  }

  /* --------------------------------------------------------------- pills */

  function addPills(pills) {
    pillRow.textContent = "";
    if (!pills) {
      return;
    }
    pills.forEach(function (pill) {
      var button = el("button", "clerk-pill", pill.label);
      button.type = "button";
      button.addEventListener("click", function () {
        ask(pill.prompt);
      });
      pillRow.appendChild(button);
    });
  }

  /* ---------------------------------------------------------------- a turn */

  function render(payload) {
    var turn = addTurn("clerk", payload.reply);
    addUsed(turn, payload.used);
    addCoverage(turn, payload.withheld);
    if (payload.message_id) {
      addMarks(turn, payload.message_id);
    }
    addPills(payload.pills);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function ask(message) {
    var text = (message || "").replace(/^\s+|\s+$/g, "");
    if (!text || busy) {
      return;
    }
    busy = true;
    send.disabled = true;
    addTurn("person", text);
    addPills([]);
    input.value = "";

    post(TURN_URL, { message: text, surface: surface })
      .then(render)
      .catch(function (problem) {
        /* The turn did not happen. Say that, rather than an empty panel that
           looks like an answer nobody read. */
        addTurn(
          "note",
          "That did not reach the record: " +
            problem.message +
            ". Nothing was looked up, so nothing here is an answer."
        );
      })
      .then(function () {
        busy = false;
        send.disabled = false;
        input.focus();
      });
  }

  /* ------------------------------------------------------------ open, close */

  function openDock() {
    show(dock, true);
    opener.setAttribute("aria-expanded", "true");
    input.focus();
    if (opened) {
      return;
    }
    opened = true;
    opening()
      .then(function (payload) {
        nameEverything(payload);
        addTurn("clerk", payload.greeting);
        addPills(payload.pills);
      })
      .catch(function () {
        /* The fallback announces itself: no invented greeting, no silent
           empty panel. The input still works, and so does the bug report. */
        addTurn(
          "note",
          "I could not read my own opening from this build. The input below " +
            "still sends, and nothing here is an answer."
        );
      });
  }

  function closeDock() {
    show(dock, false);
    opener.setAttribute("aria-expanded", "false");
    opener.focus();
  }

  opener.addEventListener("click", function () {
    if (dock.hidden) {
      openDock();
    } else {
      closeDock();
    }
  });
  closer.addEventListener("click", closeDock);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ask(input.value);
  });

  /* Enter sends, shift-enter breaks a line. A textarea rather than an input
     because a question about a docket runs to two lines more often than not. */
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });

  /* --------------------------------------------------------- the bug report */

  var bugWhere = root.querySelector("[data-clerk-surface]");

  if (bugWhere) {
    bugWhere.textContent = surface;
  }

  /* form.elements, not form.title. A form's `title` property is the tooltip
     string every element carries, so the named-control shortcut silently hands
     back "" for exactly the field this form needs most. */
  var bugTitle = bugForm.elements.title;
  var bugDetail = bugForm.elements.detail;

  function openBug() {
    show(bugSheet, true);
    bugOpen.setAttribute("aria-expanded", "true");
    bugTitle.focus();
  }

  function closeBug() {
    show(bugSheet, false);
    bugOpen.setAttribute("aria-expanded", "false");
    bugOpen.focus();
  }

  bugOpen.addEventListener("click", function () {
    if (bugSheet.hidden) {
      openBug();
    } else {
      closeBug();
    }
  });
  bugClose.addEventListener("click", closeBug);

  bugForm.addEventListener("submit", function (event) {
    event.preventDefault();
    bugSaid.textContent = "";
    post(BUG_URL, {
      title: bugTitle.value,
      detail: bugDetail.value,
      surface: surface
    })
      .then(function () {
        bugForm.reset();
        bugSaid.textContent = "Reported. It is in the queue with your name on it.";
      })
      .catch(function (problem) {
        bugSaid.textContent = "Not reported: " + problem.message;
      });
  });

  /* Escape closes whatever is open, innermost first, and hands focus back to
     the control that opened it -- the same rule citation.js keeps. */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    if (!bugSheet.hidden) {
      closeBug();
    } else if (!dock.hidden) {
      closeDock();
    }
  });

  place();
})();
