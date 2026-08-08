/* Scroll reveals. No library, no build step -- the same constraint the rest of
 * this project works under (ADR-012).
 *
 * Reduced motion is answered by doing nothing at all: the CSS ships the end
 * state and this script only ever removes it. A reader who asked for less
 * motion gets the finished page, not an empty one. That order matters; the
 * opposite would leave them looking at nothing.
 *
 * The same argument covers two other readers the obvious version loses. A
 * browser with no IntersectionObserver returns below, and a browser with
 * JavaScript switched off never reaches this file at all. Both get the whole
 * page, because the whole page is what the markup and the stylesheet already
 * say. Nothing here is a content dependency.
 *
 * WHAT DOES NOT REVEAL, AND WHY IT IS NOT AN OVERSIGHT. The hero carries no
 * data-reveal. It holds the refusal -- a claim declining to assert itself --
 * and that surface is given no effects anywhere on this site. It is also the
 * first screenful, so fading it in would mean the first thing a reader sees is
 * an empty page settling. index.html says this again at the markup.
 */
(function () {
  var wants = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (wants.matches || !("IntersectionObserver" in window)) return;

  var targets = document.querySelectorAll("[data-reveal]");
  Array.prototype.forEach.call(targets, function (el) {
    el.classList.add("is-hidden");
  });

  var seen = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.remove("is-hidden");
        seen.unobserve(entry.target);
      });
    },
    { threshold: 0.15 }
  );
  Array.prototype.forEach.call(targets, function (el) {
    seen.observe(el);
  });
})();
