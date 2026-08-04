/* The citation viewer. It toggles panels and nothing else.
 *
 * Every source extract is already in the page, server-rendered, with the cited
 * characters wrapped in a mark. There is no fetch here and no template. That is
 * the point: the evidence behind a claim must not depend on a request
 * succeeding, so the only thing a script can break is the tidiness.
 *
 * The page ships with the panels OPEN and their chips marked expanded. This
 * file closes them on load. Done the other way round -- hidden in the HTML,
 * revealed by script -- a reader with scripts blocked would see the claims and
 * none of their proof, which is the one failure this product cannot have.
 */
(function () {
  "use strict";

  var chips = Array.prototype.slice.call(
    document.querySelectorAll(".citation-chip[aria-controls]")
  );
  if (!chips.length) {
    return;
  }

  function panelFor(chip) {
    return document.getElementById(chip.getAttribute("aria-controls"));
  }

  function close(chip) {
    var panel = panelFor(chip);
    if (panel) {
      panel.hidden = true;
    }
    chip.setAttribute("aria-expanded", "false");
  }

  /* Scroll the container, never the page. The analyst opened a citation to read
     a sentence, not to lose their place in the claim column, so this moves the
     panel's own scroll box and leaves the document where it was. */
  function reveal(panel) {
    var body = panel.querySelector(".source-panel__body");
    var mark = panel.querySelector(".source-mark");
    if (!body || !mark) {
      return;
    }
    var box = body.getBoundingClientRect();
    var span = mark.getBoundingClientRect();
    body.scrollTop += span.top - box.top - (body.clientHeight - span.height) / 2;
  }

  function open(chip) {
    var panel = panelFor(chip);
    if (!panel) {
      return;
    }
    chips.forEach(close);
    panel.hidden = false;
    chip.setAttribute("aria-expanded", "true");
    reveal(panel);
    panel.focus();
  }

  function openChip() {
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].getAttribute("aria-expanded") === "true") {
        return chips[i];
      }
    }
    return null;
  }

  chips.forEach(function (chip) {
    close(chip);

    chip.addEventListener("click", function () {
      if (chip.getAttribute("aria-expanded") === "true") {
        close(chip);
      } else {
        open(chip);
      }
    });

    var panel = panelFor(chip);
    var button = panel && panel.querySelector(".source-panel__close");
    if (button) {
      button.addEventListener("click", function () {
        close(chip);
        chip.focus();
      });
    }
  });

  /* Escape closes whatever is open and hands focus back to the chip that opened
     it. Without that last step the reader is left at the top of the document
     with no way back to where they were reading. */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var chip = openChip();
    if (!chip) {
      return;
    }
    close(chip);
    chip.focus();
  });
})();
