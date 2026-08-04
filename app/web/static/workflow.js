/* workflow.js — the approval route canvas.
 *
 * NO FRAMEWORK, NO BUILD STEP, NO NETWORK BUT ITS OWN. Vanilla JavaScript and
 * SVG, one <script> tag, nothing fetched from anywhere else. It works with the
 * network off except for the two things that are network by definition: saving
 * and activating, and both of those say so when they fail rather than looking
 * like they worked. The house style is app/web/static/citation.js: an IIFE,
 * `var`, plain functions, and no cleverness that a reader has to unpick.
 *
 * THE GRAPH IS ALREADY IN THE PAGE. It arrives in a <script type="application
 * /json"> block that the server rendered, so opening the editor never depends
 * on a request succeeding. Same argument citation.js makes about source
 * extracts: the thing the screen is about must not be one failed fetch away.
 *
 * NOTHING IS BUILT WITH innerHTML. Every node, every option and every message
 * is createElement and textContent. A route label is text an admin typed and a
 * validation message is text the server wrote; either could carry a bracket,
 * and a screen that renders one as markup is a screen that renders all of them
 * as markup.
 *
 * NODES ARE BUTTONS, WIRES ARE SVG. An SVG <g> is not reliably focusable and
 * needs role and tabindex bolted on to be announced as anything. A <button>
 * positioned over the SVG is reachable by Tab, gets the browser's focus ring,
 * and reads as a control with no argument. So the SVG layer draws paths and
 * nothing else, and every interactive thing on the canvas -- a step, its output
 * handle, an arrow's delete control -- is a real button.
 *
 * KEYBOARD, IN FULL, because a canvas that needs a mouse excludes people.
 *   Tab            reaches every step, every output handle, every arrow's cut
 *   Enter / Space  selects a step for editing
 *   Arrow keys     move the selected step; Shift moves it further
 *   c              starts a connection from this step; c on a second finishes
 *   Escape         cancels a connection in progress
 *   Delete         removes the step and its arrows
 * The one thing a keyboard cannot do here is a free drag with a pointer, and it
 * does not need to: arrow keys move by a fixed step, which is more precise.
 *
 * WHAT IS NOT TESTED. There is no browser in this repository's test suite and
 * no build step to add one, so this file is checked by eye and by the JSON it
 * exchanges, which tests/test_workflow_editor.py pins on both sides. Said
 * plainly rather than left to be discovered.
 */
(function () {
  "use strict";

  var graphNode = document.getElementById("wf-graph");
  var configNode = document.getElementById("wf-config");
  if (!graphNode || !configNode) {
    return;
  }

  var graph = JSON.parse(graphNode.textContent);
  var config = JSON.parse(configNode.textContent);

  var canvas = document.getElementById("wf-canvas");
  var wires = document.getElementById("wf-wires");
  var nodes = document.getElementById("wf-nodes");
  var smallList = document.getElementById("wf-small-list");
  var panel = document.getElementById("wf-panel");
  var panelEmpty = document.getElementById("wf-panel-empty");
  var status = document.getElementById("wf-status");
  var graphErrors = document.getElementById("wf-graph-errors");

  var fields = {
    id: document.getElementById("wf-panel-id"),
    errors: document.getElementById("wf-panel-errors"),
    label: document.getElementById("wf-label"),
    assignee: document.getElementById("wf-assignee"),
    hours: document.getElementById("wf-hours"),
    timeout: document.getElementById("wf-timeout"),
    timeoutHint: document.getElementById("wf-timeout-hint"),
    remindField: document.getElementById("wf-remind-field"),
    remind: document.getElementById("wf-remind"),
    escalateField: document.getElementById("wf-escalate-field"),
    escalate: document.getElementById("wf-escalate")
  };

  var SVG_NS = "http://www.w3.org/2000/svg";
  var NUDGE = 8;
  var BIG_NUDGE = 40;
  var PAD = 80;
  var DRAG_SLOP = 4;

  var selected = null; // step id
  var armed = null; // step id a connection is being drawn from
  var stepErrors = {}; // step id -> [message]
  var dirty = false;
  var suppressClick = false;

  /* --------------------------------------------------------------- model */

  function stepById(id) {
    for (var i = 0; i < graph.steps.length; i++) {
      if (graph.steps[i].id === id) {
        return graph.steps[i];
      }
    }
    return null;
  }

  /* STP-2 before STP-10. The server sorts the same way; sorting as text would
     put ten before two and the list would reorder itself the moment a tenth
     node was added, with nothing on screen to say why. */
  function numberOf(id) {
    var tail = String(id).split("-").pop();
    return /^\d+$/.test(tail) ? parseInt(tail, 10) : NaN;
  }

  function nextStepId() {
    var highest = 0;
    for (var i = 0; i < graph.steps.length; i++) {
      var n = numberOf(graph.steps[i].id);
      if (!isNaN(n) && n > highest) {
        highest = n;
      }
    }
    return "STP-" + (highest + 1);
  }

  function removeStep(id) {
    graph.steps = graph.steps.filter(function (step) {
      return step.id !== id;
    });
    // The arrows go with it. An edge naming a step that is gone is refused on
    // save, and would be refused by the database on PostgreSQL, so leaving one
    // behind would turn a delete into a save that fails for a reason the admin
    // did not cause.
    graph.edges = graph.edges.filter(function (edge) {
      return edge.from !== id && edge.to !== id;
    });
    if (selected === id) {
      selected = null;
    }
    if (armed === id) {
      armed = null;
    }
    delete stepErrors[id];
  }

  function hasEdge(from, to) {
    return graph.edges.some(function (edge) {
      return edge.from === from && edge.to === to;
    });
  }

  function connect(from, to) {
    if (!from || !to || from === to || hasEdge(from, to)) {
      return false;
    }
    graph.edges.push({ from: from, to: to });
    return true;
  }

  function touched(message) {
    dirty = true;
    say(message || "Unsaved changes.", false);
  }

  /* ------------------------------------------------------------- wording */

  function timeoutWord(step) {
    if (!step.on_timeout) {
      return "no timeout rule";
    }
    if (step.on_timeout === config.timeout_bypass) {
      return "skipped, no approval";
    }
    if (step.on_timeout === config.timeout_escalate) {
      return "escalates";
    }
    if (step.on_timeout === config.timeout_remind) {
      return "reminds";
    }
    return step.on_timeout;
  }

  function assigneeWord(rule) {
    if (!rule || rule === "unassigned") {
      return "nobody yet";
    }
    if (rule === "obligation_owner") {
      return "the obligation owner";
    }
    if (rule.indexOf(config.role_prefix) === 0) {
      return "role: " + rule.slice(config.role_prefix.length).replace(/_/g, " ");
    }
    if (rule.indexOf(config.user_prefix) === 0) {
      var id = rule.slice(config.user_prefix.length);
      for (var i = 0; i < config.users.length; i++) {
        if (config.users[i].id === id) {
          return config.users[i].name;
        }
      }
      // Announced, not smoothed over. A rule pointing at an account this
      // company does not have routes nowhere, and activation refuses it.
      return "an account not in this company (" + id + ")";
    }
    return rule;
  }

  function hoursWord(step) {
    if (!step.approval_hours) {
      return "no deadline";
    }
    return step.approval_hours === 1 ? "1 hour" : step.approval_hours + " hours";
  }

  function timeoutClass(step) {
    if (!step.on_timeout) {
      return "wf-node--undecided";
    }
    return "wf-node--" + step.on_timeout;
  }

  /* ------------------------------------------------------------ drawing */

  function clear(element) {
    while (element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  function nodeElement(id) {
    return nodes.querySelector('[data-step="' + cssEscape(id) + '"]');
  }

  /* Refusals as a list, one message per line. Run together into a paragraph
     they read as a wall -- four separate facts about a step, each of which the
     admin has to act on separately, in one block nobody finishes. */
  function messageList(messages) {
    var list = document.createElement("ul");
    list.className = "wf-node__errors";
    messages.forEach(function (message) {
      var item = document.createElement("li");
      item.textContent = message;
      list.appendChild(item);
    });
    return list;
  }

  /* Bring a node into the canvas's own scroll box, without moving the page.
     scrollIntoView would scroll the document as well, which throws the admin
     out of the toolbar they just used. */
  function reveal(id) {
    var element = nodeElement(id);
    if (!element) {
      return;
    }
    var left = element.offsetLeft;
    var top = element.offsetTop;
    var right = left + element.offsetWidth;
    var bottom = top + element.offsetHeight;
    if (left < canvas.scrollLeft) {
      canvas.scrollLeft = Math.max(0, left - 24);
    } else if (right > canvas.scrollLeft + canvas.clientWidth) {
      canvas.scrollLeft = right - canvas.clientWidth + 24;
    }
    if (top < canvas.scrollTop) {
      canvas.scrollTop = Math.max(0, top - 24);
    } else if (bottom > canvas.scrollTop + canvas.clientHeight) {
      canvas.scrollTop = bottom - canvas.clientHeight + 24;
    }
  }

  /* Attribute selectors take a quoted string, and a step id is a value the
     server chose -- but it is still not this file's business to assume it has
     no quote in it. */
  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function drawNodes() {
    var focusId = document.activeElement
      ? document.activeElement.getAttribute("data-focus")
      : null;
    clear(nodes);

    graph.steps.forEach(function (step, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "wf-node " + timeoutClass(step);
      if (step.id === selected) {
        button.className += " wf-node--selected";
      }
      if (stepErrors[step.id]) {
        button.className += " wf-node--bad";
      }
      button.setAttribute("data-step", step.id);
      button.setAttribute("data-focus", "node:" + step.id);
      button.setAttribute("aria-pressed", step.id === selected ? "true" : "false");
      button.style.left = (step.x || 0) + "px";
      button.style.top = (step.y || 0) + "px";

      var label = document.createElement("span");
      label.className =
        "wf-node__label" + (step.label ? "" : " wf-node__label--empty");
      label.textContent = step.label || "Unnamed step";
      button.appendChild(label);

      var who = document.createElement("span");
      who.className = "wf-node__who";
      who.textContent = assigneeWord(step.assignee_rule) + " · " + hoursWord(step);
      button.appendChild(who);

      var rule = document.createElement("span");
      rule.className = "wf-node__rule";
      rule.textContent = timeoutWord(step);
      button.appendChild(rule);

      button.setAttribute(
        "aria-label",
        "Step " +
          (index + 1) +
          ", " +
          (step.label || "unnamed") +
          ". Asked: " +
          assigneeWord(step.assignee_rule) +
          ". " +
          hoursWord(step) +
          ", then " +
          timeoutWord(step) +
          "."
      );

      if (stepErrors[step.id]) {
        button.appendChild(messageList(stepErrors[step.id]));
      }

      nodes.appendChild(button);

      // The output handle, placed after the node is in the document so its
      // measured height is real rather than assumed.
      var handle = document.createElement("button");
      handle.type = "button";
      handle.className = "wf-handle" + (armed === step.id ? " wf-handle--armed" : "");
      handle.setAttribute("data-handle", step.id);
      handle.setAttribute("data-focus", "handle:" + step.id);
      handle.textContent = "→";
      handle.title = "Connect this step to another";
      handle.setAttribute(
        "aria-label",
        "Connect " + (step.label || step.id) + " to another step"
      );
      handle.style.left = (step.x || 0) + button.offsetWidth - 9 + "px";
      handle.style.top =
        (step.y || 0) + Math.round(button.offsetHeight / 2) - 9 + "px";
      nodes.appendChild(handle);
    });

    if (focusId) {
      var back = nodes.querySelector('[data-focus="' + cssEscape(focusId) + '"]');
      if (back) {
        back.focus();
      }
    }
  }

  function centreRight(step) {
    var element = nodeElement(step.id);
    var width = element ? element.offsetWidth : 208;
    var height = element ? element.offsetHeight : 80;
    return { x: (step.x || 0) + width, y: (step.y || 0) + height / 2 };
  }

  function centreLeft(step) {
    var element = nodeElement(step.id);
    var height = element ? element.offsetHeight : 80;
    return { x: step.x || 0, y: (step.y || 0) + height / 2 };
  }

  function drawWires(livePoint) {
    clear(wires);
    // The cut buttons live in the node layer, because they are HTML and the
    // wires are SVG. They are drawn here, so they are cleared here -- this runs
    // on every frame of a drag, and leaving them would pile up one dead button
    // per pointer move.
    Array.prototype.slice
      .call(nodes.querySelectorAll(".wf-cut"))
      .forEach(function (old) {
        old.parentNode.removeChild(old);
      });

    var extent = { w: 0, h: 0 };
    graph.steps.forEach(function (step) {
      var element = nodeElement(step.id);
      extent.w = Math.max(extent.w, (step.x || 0) + (element ? element.offsetWidth : 208));
      extent.h = Math.max(extent.h, (step.y || 0) + (element ? element.offsetHeight : 80));
    });
    var width = extent.w + PAD;
    var height = extent.h + PAD;
    wires.style.width = width + "px";
    wires.style.height = height + "px";
    nodes.style.width = width + "px";
    nodes.style.height = height + "px";

    graph.edges.forEach(function (edge, index) {
      var from = stepById(edge.from);
      var to = stepById(edge.to);
      if (!from || !to) {
        return;
      }
      var start = centreRight(from);
      var end = centreLeft(to);
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("class", "wf-wire");
      path.setAttribute("d", curve(start, end));
      wires.appendChild(path);

      // The cut control, on the wire, reachable by Tab.
      var cut = document.createElement("button");
      cut.type = "button";
      cut.className = "wf-cut";
      cut.textContent = "×";
      cut.setAttribute("data-cut", index);
      cut.setAttribute("data-focus", "cut:" + edge.from + ">" + edge.to);
      cut.setAttribute(
        "aria-label",
        "Remove the arrow from " +
          (from.label || from.id) +
          " to " +
          (to.label || to.id)
      );
      cut.style.left = Math.round((start.x + end.x) / 2 - 10) + "px";
      cut.style.top = Math.round((start.y + end.y) / 2 - 10) + "px";
      nodes.appendChild(cut);
    });

    if (livePoint && armed) {
      var source = stepById(armed);
      if (source) {
        var live = document.createElementNS(SVG_NS, "path");
        live.setAttribute("class", "wf-wire wf-wire--live");
        live.setAttribute("d", curve(centreRight(source), livePoint));
        wires.appendChild(live);
      }
    }
  }

  function curve(start, end) {
    var bend = Math.max(40, Math.abs(end.x - start.x) / 2);
    return (
      "M " +
      start.x +
      " " +
      start.y +
      " C " +
      (start.x + bend) +
      " " +
      start.y +
      ", " +
      (end.x - bend) +
      " " +
      end.y +
      ", " +
      end.x +
      " " +
      end.y
    );
  }

  function drawSmall() {
    if (!smallList) {
      return;
    }
    clear(smallList);
    graph.steps.forEach(function (step, index) {
      var item = document.createElement("li");
      var button = document.createElement("button");
      button.type = "button";
      button.className = "wf-small__step " + timeoutClass(step);
      if (stepErrors[step.id]) {
        button.className += " wf-node--bad";
      }
      button.setAttribute("data-step", step.id);
      button.setAttribute("aria-pressed", step.id === selected ? "true" : "false");

      var head = document.createElement("span");
      head.className = "wf-node__label";
      head.textContent = index + 1 + ". " + (step.label || "Unnamed step");
      button.appendChild(head);

      var body = document.createElement("span");
      body.className = "wf-node__who";
      body.textContent =
        assigneeWord(step.assignee_rule) +
        " · " +
        hoursWord(step) +
        " · " +
        timeoutWord(step);
      button.appendChild(body);

      if (stepErrors[step.id]) {
        button.appendChild(messageList(stepErrors[step.id]));
      }

      item.appendChild(button);
      smallList.appendChild(item);
    });
  }

  /* -------------------------------------------------------------- panel */

  function option(value, text) {
    var element = document.createElement("option");
    element.value = value;
    element.textContent = text;
    return element;
  }

  function fillAssignee(select, rule, allowLiterals) {
    clear(select);
    if (allowLiterals) {
      select.appendChild(option("obligation_owner", "The obligation owner on the item"));
      select.appendChild(option("unassigned", "Nobody yet"));
    } else {
      select.appendChild(option("", "Nobody chosen"));
    }

    if (config.roles.length) {
      var roleGroup = document.createElement("optgroup");
      roleGroup.label = "A role";
      config.roles.forEach(function (name) {
        roleGroup.appendChild(
          option(config.role_prefix + name, name.replace(/_/g, " "))
        );
      });
      select.appendChild(roleGroup);
    }

    if (config.users.length) {
      var userGroup = document.createElement("optgroup");
      userGroup.label = "A named person";
      config.users.forEach(function (person) {
        userGroup.appendChild(option(config.user_prefix + person.id, person.name));
      });
      select.appendChild(userGroup);
    }

    // A stored rule the menu does not offer -- a role since deleted, an account
    // since removed -- is added as its own option and named as unresolved
    // rather than silently swapped for the first entry in the list. Quietly
    // changing a saved value on open is how a route stops matching what the
    // admin last agreed to.
    var value = rule || (allowLiterals ? "unassigned" : "");
    if (value && !select.querySelector('option[value="' + cssEscape(value) + '"]')) {
      select.appendChild(option(value, value + " — not in this company"));
    }
    select.value = value;
  }

  function fillTimeout(select, value) {
    clear(select);
    select.appendChild(option("", "Not decided yet"));
    config.timeout_actions.forEach(function (action) {
      var words = action;
      if (action === config.timeout_remind) {
        words = "Remind, and keep waiting";
      } else if (action === config.timeout_escalate) {
        words = "Escalate to somebody else";
      } else if (action === config.timeout_bypass) {
        words = "Skip the step — the item moves on WITHOUT approval";
      }
      select.appendChild(option(action, words));
    });
    select.value = value || "";
  }

  function timeoutHint(value) {
    if (value === config.timeout_bypass) {
      return {
        text:
          "An item reaching this step with nobody answering moves on with no " +
          "approval from this step. Everybody reading the route screen is told " +
          "that in those words.",
        warn: true
      };
    }
    if (value === config.timeout_escalate) {
      return { text: "The step is asked again of somebody else.", warn: false };
    }
    if (value === config.timeout_remind) {
      return {
        text: "Nobody else is asked and the item does not move on.",
        warn: false
      };
    }
    return {
      text: "Activation refuses a step that does not say. Nothing is assumed.",
      warn: false
    };
  }

  function showPanel() {
    var step = selected ? stepById(selected) : null;
    if (!step) {
      panel.hidden = true;
      panelEmpty.hidden = false;
      return;
    }
    panel.hidden = false;
    panelEmpty.hidden = true;

    fields.id.textContent = step.id;
    fields.label.value = step.label || "";
    fillAssignee(fields.assignee, step.assignee_rule, true);
    fields.hours.value = step.approval_hours === null ? "" : step.approval_hours;
    fillTimeout(fields.timeout, step.on_timeout);
    fillAssignee(fields.escalate, step.escalate_to, false);
    fields.remind.value =
      step.remind_every_hours === null ? "" : step.remind_every_hours;

    var hint = timeoutHint(step.on_timeout);
    fields.timeoutHint.textContent = hint.text;
    fields.timeoutHint.className = "wf-hint" + (hint.warn ? " wf-hint--warn" : "");

    // A field that cannot apply is hidden AND disabled. Hidden alone leaves it
    // in the tab order in some browsers, and disabled alone leaves a dead
    // control sitting on screen.
    applicable(fields.remindField, fields.remind, step.on_timeout === config.timeout_remind);
    applicable(
      fields.escalateField,
      fields.escalate,
      step.on_timeout === config.timeout_escalate
    );

    clear(fields.errors);
    if (stepErrors[step.id]) {
      fields.errors.hidden = false;
      fields.errors.appendChild(messageList(stepErrors[step.id]));
    } else {
      fields.errors.hidden = true;
    }

    var locked = config.editable !== true;
    [
      fields.label,
      fields.assignee,
      fields.hours,
      fields.timeout,
      fields.remind,
      fields.escalate
    ].forEach(function (control) {
      if (locked) {
        control.disabled = true;
      }
    });
    document.getElementById("wf-delete").disabled = locked;
  }

  function applicable(wrapper, control, yes) {
    wrapper.hidden = !yes;
    control.disabled = !yes || config.editable !== true;
  }

  function draw() {
    drawNodes();
    drawWires();
    drawSmall();
    showPanel();
  }

  /* ------------------------------------------------------------- status */

  function say(message, bad) {
    status.textContent = message;
    status.className = "wf-bar__status" + (bad ? " wf-bar__status--bad" : "");
  }

  function showErrors(errors) {
    stepErrors = {};
    var general = [];
    errors.forEach(function (error) {
      if (error.step_id) {
        if (!stepErrors[error.step_id]) {
          stepErrors[error.step_id] = [];
        }
        stepErrors[error.step_id].push(error.message);
      } else {
        general.push(error.message);
      }
    });

    clear(graphErrors);
    if (general.length) {
      graphErrors.hidden = false;
      var heading = document.createElement("p");
      heading.textContent =
        "This route cannot go live yet. These are about the graph as a whole:";
      graphErrors.appendChild(heading);
      var list = document.createElement("ul");
      general.forEach(function (message) {
        var item = document.createElement("li");
        item.textContent = message;
        list.appendChild(item);
      });
      graphErrors.appendChild(list);
    } else {
      graphErrors.hidden = true;
    }

    var named = Object.keys(stepErrors);
    if (named.length) {
      // Open the first offending step, so the message the admin has to act on
      // is on screen rather than one click away. It overrides whatever was
      // selected: after pressing Activate the thing to look at is the refusal,
      // not the node that happened to be open when it was pressed.
      selected = named.sort(function (a, b) {
        return numberOf(a) - numberOf(b);
      })[0];
    }
    draw();
    if (selected) {
      reveal(selected);
    }
  }

  function clearErrors() {
    stepErrors = {};
    clear(graphErrors);
    graphErrors.hidden = true;
  }

  /* ------------------------------------------------------------- saving */

  function post(url, body) {
    var options = {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(url, options).then(function (response) {
      return response.json().then(function (payload) {
        return { status: response.status, payload: payload };
      });
    });
  }

  function save() {
    say("Saving…", false);
    return post(config.graph_url, graph)
      .then(function (result) {
        if (result.payload && result.payload.ok) {
          clearErrors();
          dirty = false;
          draw();
          say("Saved.", false);
          return true;
        }
        showErrors((result.payload && result.payload.errors) || []);
        say("Not saved. The refusals are on the steps below.", true);
        return false;
      })
      .catch(function () {
        // Announced. A save that failed on the network must never look like a
        // save that worked: the admin would close the tab believing the route
        // was stored.
        say("Nothing was saved — the request did not reach the server.", true);
        return false;
      });
  }

  function activate() {
    say("Checking the route…", false);
    post(config.activate_url)
      .then(function (result) {
        if (result.payload && result.payload.ok) {
          say("This is now the live route. Reloading…", false);
          window.location.reload();
          return;
        }
        showErrors((result.payload && result.payload.errors) || []);
        say("Not activated. Every reason is on its step below.", true);
      })
      .catch(function () {
        say("Nothing was activated — the request did not reach the server.", true);
      });
  }

  /* ------------------------------------------------------------- events */

  function select(id) {
    selected = id;
    draw();
    reveal(id);
  }

  function perRow() {
    return Math.max(1, Math.floor((canvas.clientWidth - 60) / 240));
  }

  function addStep() {
    var step = {
      id: nextStepId(),
      label: "",
      assignee_rule: "unassigned",
      approval_hours: null,
      on_timeout: null,
      escalate_to: null,
      remind_every_hours: null,
      // Placed in a row, offset from the last one, so a new node never lands
      // on top of an existing one and looks like nothing happened -- and the
      // row length comes from the canvas's real width rather than a guess of
      // four, because a node dropped off the right edge of a narrow canvas is
      // a node the admin has to go looking for.
      x: 40 + (graph.steps.length % perRow()) * 240,
      y: 40 + Math.floor(graph.steps.length / perRow()) * 160
    };
    graph.steps.push(step);
    selected = step.id;
    touched("Step added. Not saved yet.");
    draw();
    reveal(step.id);
    fields.label.focus();
  }

  function armFrom(id) {
    if (armed === id) {
      armed = null;
      say("Connection cancelled.", false);
    } else if (armed) {
      if (connect(armed, id)) {
        touched("Connected. Not saved yet.");
      } else {
        say("Those two are already connected, or it is the same step.", true);
      }
      armed = null;
    } else {
      armed = id;
      say("Connecting from this step. Move to another and press c, or Escape.", false);
    }
    draw();
  }

  if (config.editable === true) {
    document.getElementById("wf-add").addEventListener("click", addStep);
    document.getElementById("wf-save").addEventListener("click", function () {
      save();
    });
    document.getElementById("wf-activate").addEventListener("click", function () {
      // Save first. Activating a route while the canvas holds unsaved changes
      // would validate the stored graph and report errors about nodes that are
      // not on screen -- messages the admin cannot act on.
      if (dirty) {
        save().then(function (ok) {
          if (ok) {
            activate();
          }
        });
      } else {
        activate();
      }
    });
    document.getElementById("wf-delete").addEventListener("click", function () {
      if (!selected) {
        return;
      }
      removeStep(selected);
      touched("Step deleted. Not saved yet.");
      draw();
    });
  }

  nodes.addEventListener("click", function (event) {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    var cut = event.target.closest(".wf-cut");
    if (cut && config.editable === true) {
      graph.edges.splice(parseInt(cut.getAttribute("data-cut"), 10), 1);
      touched("Arrow removed. Not saved yet.");
      draw();
      return;
    }
    var handle = event.target.closest(".wf-handle");
    if (handle && config.editable === true) {
      armFrom(handle.getAttribute("data-handle"));
      return;
    }
    var node = event.target.closest(".wf-node");
    if (node) {
      if (armed && armed !== node.getAttribute("data-step")) {
        armFrom(node.getAttribute("data-step"));
        return;
      }
      select(node.getAttribute("data-step"));
    }
  });

  if (smallList) {
    smallList.addEventListener("click", function (event) {
      var button = event.target.closest(".wf-small__step");
      if (button) {
        select(button.getAttribute("data-step"));
      }
    });
  }

  /* Dragging. Pointer events rather than mouse events, so a stylus and a
     trackpad behave the same; the canvas sets touch-action: none so a drag on a
     touch screen moves the node instead of scrolling the page.

     THE POINTER IS CAPTURED ON THE NODE, NOT ON THE CANVAS, and that is not a
     detail. A browser dispatches `click` to the nearest common ancestor of the
     pointerdown and pointerup targets, and pointer capture retargets both to
     whatever holds the capture. Capturing on the canvas therefore fired every
     click on the canvas itself, the selection listener on the node layer never
     ran, and clicking a step did nothing at all -- silently, with no error
     anywhere. Capturing on the node keeps the click on the node and still
     delivers pointermove after the cursor has left it, which is the only thing
     the capture was for. */
  var drag = null;

  nodes.addEventListener("pointerdown", function (event) {
    if (config.editable !== true) {
      return;
    }
    var handle = event.target.closest(".wf-handle");
    if (handle) {
      event.preventDefault();
      armed = handle.getAttribute("data-handle");
      drag = { kind: "link", moved: false, capture: handle };
      handle.setPointerCapture(event.pointerId);
      drawWires();
      return;
    }
    var node = event.target.closest(".wf-node");
    if (!node || event.target.closest(".wf-cut")) {
      return;
    }
    var step = stepById(node.getAttribute("data-step"));
    if (!step) {
      return;
    }
    var box = canvas.getBoundingClientRect();
    drag = {
      kind: "move",
      id: step.id,
      moved: false,
      capture: node,
      offsetX: event.clientX - box.left + canvas.scrollLeft - (step.x || 0),
      offsetY: event.clientY - box.top + canvas.scrollTop - (step.y || 0)
    };
    node.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", function (event) {
    if (!drag) {
      return;
    }
    var box = canvas.getBoundingClientRect();
    var x = event.clientX - box.left + canvas.scrollLeft;
    var y = event.clientY - box.top + canvas.scrollTop;

    if (drag.kind === "link") {
      drag.moved = true;
      drawWires({ x: x, y: y });
      return;
    }

    var step = stepById(drag.id);
    if (!step) {
      return;
    }
    var nextX = Math.max(0, Math.round(x - drag.offsetX));
    var nextY = Math.max(0, Math.round(y - drag.offsetY));
    if (!drag.moved) {
      var far =
        Math.abs(nextX - (step.x || 0)) > DRAG_SLOP ||
        Math.abs(nextY - (step.y || 0)) > DRAG_SLOP;
      if (!far) {
        return;
      }
      drag.moved = true;
    }
    step.x = nextX;
    step.y = nextY;
    var element = nodeElement(step.id);
    if (element) {
      element.style.left = step.x + "px";
      element.style.top = step.y + "px";
    }
    drawWires();
  });

  canvas.addEventListener("pointerup", function (event) {
    if (!drag) {
      return;
    }
    var finished = drag;
    drag = null;
    if (finished.capture && finished.capture.hasPointerCapture(event.pointerId)) {
      finished.capture.releasePointerCapture(event.pointerId);
    }

    if (finished.kind === "link") {
      var under = document.elementFromPoint(event.clientX, event.clientY);
      var target = under ? under.closest(".wf-node") : null;
      if (target && armed) {
        if (connect(armed, target.getAttribute("data-step"))) {
          touched("Connected. Not saved yet.");
        } else {
          say("Those two are already connected, or it is the same step.", true);
        }
        armed = null;
      } else if (finished.moved) {
        // Dropped on nothing. The armed state stays, so the next click on a
        // node finishes the connection -- and the status line says so rather
        // than leaving a half-drawn arrow with no explanation.
        say("Drop a connection on a step, or press Escape to cancel.", false);
      }
      suppressClick = finished.moved;
      draw();
      return;
    }

    if (finished.moved) {
      suppressClick = true;
      touched("Moved. Not saved yet.");
      draw();
    }
  });

  /* A cancelled pointer -- the browser took it back for a scroll or a gesture --
     ends the drag where it stands rather than leaving the canvas believing a
     drag is still running. Without this the next click would be swallowed. */
  canvas.addEventListener("pointercancel", function () {
    if (drag) {
      drag = null;
      draw();
    }
  });

  /* Keyboard on the canvas. Every pointer gesture above has an equivalent here
     except a free drag, which arrow keys replace with something more precise. */
  nodes.addEventListener("keydown", function (event) {
    var node = event.target.closest(".wf-node");
    if (!node) {
      return;
    }
    var id = node.getAttribute("data-step");
    var step = stepById(id);
    if (!step) {
      return;
    }

    if (event.key === "c" || event.key === "C") {
      if (config.editable === true) {
        event.preventDefault();
        armFrom(id);
      }
      return;
    }

    if (event.key === "Delete" || event.key === "Backspace") {
      if (config.editable === true) {
        event.preventDefault();
        removeStep(id);
        touched("Step deleted. Not saved yet.");
        draw();
      }
      return;
    }

    var moves = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1]
    };
    if (moves[event.key] && config.editable === true) {
      event.preventDefault();
      var by = event.shiftKey ? BIG_NUDGE : NUDGE;
      step.x = Math.max(0, (step.x || 0) + moves[event.key][0] * by);
      step.y = Math.max(0, (step.y || 0) + moves[event.key][1] * by);
      selected = id;
      touched("Moved. Not saved yet.");
      draw();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && armed) {
      armed = null;
      say("Connection cancelled.", false);
      draw();
    }
  });

  /* Panel edits go straight into the model. There is no apply button: a form
     that has to be submitted before the canvas agrees with it is two versions of
     one graph, and the one on screen would be the one nobody saved. */
  function bind(control, apply) {
    if (!control) {
      return;
    }
    control.addEventListener("change", function () {
      var step = selected ? stepById(selected) : null;
      if (!step) {
        return;
      }
      apply(step, control.value);
      touched("Changed. Not saved yet.");
      draw();
    });
  }

  function wholeHours(value) {
    var trimmed = String(value).trim();
    if (!trimmed) {
      return null;
    }
    var number = parseInt(trimmed, 10);
    // Not a number, or not above zero, is left as absence rather than stored as
    // a guess. The server refuses either way; keeping it out of the model means
    // the canvas never shows a deadline nobody set.
    return isNaN(number) || number < 1 ? null : number;
  }

  bind(fields.label, function (step, value) {
    step.label = value;
  });
  bind(fields.assignee, function (step, value) {
    step.assignee_rule = value || "unassigned";
  });
  bind(fields.hours, function (step, value) {
    step.approval_hours = wholeHours(value);
  });
  bind(fields.timeout, function (step, value) {
    step.on_timeout = value || null;
    // The fields that no longer apply are emptied, not just hidden. A reminder
    // interval left on a step that now escalates is a number in a column that
    // means nothing, and a reviewer reading the row would take it for a
    // decision somebody made.
    if (step.on_timeout !== config.timeout_remind) {
      step.remind_every_hours = null;
    }
    if (step.on_timeout !== config.timeout_escalate) {
      step.escalate_to = null;
    }
  });
  bind(fields.remind, function (step, value) {
    step.remind_every_hours = wholeHours(value);
  });
  bind(fields.escalate, function (step, value) {
    step.escalate_to = value || null;
  });

  /* The route's own name. It is not a step, so it is not in the step panel;
     it rides along in the same save, because the contract carries it in the
     same body and a second endpoint for one string would be a second way for
     the two to disagree. */
  var routeName = document.getElementById("wf-route-name");
  var heading = document.getElementById("wf-name");
  if (routeName) {
    routeName.addEventListener("input", function () {
      graph.name = routeName.value;
      heading.textContent = graph.name;
      dirty = true;
    });
    routeName.addEventListener("change", function () {
      touched("Renamed. Not saved yet.");
    });
  }

  // Typing in the label should show on the node as it is typed; the change
  // event alone waits for a blur, which makes the canvas look unresponsive.
  fields.label.addEventListener("input", function () {
    var step = selected ? stepById(selected) : null;
    if (!step) {
      return;
    }
    step.label = fields.label.value;
    dirty = true;
    var element = nodeElement(step.id);
    if (element) {
      var label = element.querySelector(".wf-node__label");
      label.textContent = step.label || "Unnamed step";
      label.className =
        "wf-node__label" + (step.label ? "" : " wf-node__label--empty");
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (dirty) {
      // The browser decides the wording; all a page can do is ask. Without it a
      // closed tab silently loses a route somebody spent ten minutes drawing.
      event.preventDefault();
      event.returnValue = "";
    }
  });

  draw();
  if (config.editable === true) {
    say("", false);
  } else {
    say("This route is frozen. Copy it into a new draft to change it.", false);
  }
})();
