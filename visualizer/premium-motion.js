/* ==========================================================================
   DTC OBSERVABILITY CONSOLE — MOTION & INTERACTION LAYER
   Loaded AFTER app.js / booking.js. Non-invasive: never mutates state,
   never alters button dimensions or injects intrusive DOM elements.
   ========================================================================== */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function onReady(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  /* ------------------------------------------------------------------
     1. Seat tiles: flash a clean highlight ring when seat status flips.
     ------------------------------------------------------------------ */
  function watchSeatFlips() {
    var matrix = document.getElementById("seat-matrix");
    if (!matrix || reduceMotion) return;

    var mo = new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        if (m.type !== "attributes" || m.attributeName !== "data-status") return;
        var tile = m.target;
        if (!tile.classList || !tile.classList.contains("seat-tile")) return;
        tile.classList.remove("pm-flip");
        void tile.offsetWidth;
        tile.classList.add("pm-flip");
      });
    });
    mo.observe(matrix, { attributes: true, attributeFilter: ["data-status"], subtree: true });
  }

  /* ------------------------------------------------------------------
     2. Hold countdown urgency: last 60 seconds get an alert state.
     ------------------------------------------------------------------ */
  function watchHoldUrgency() {
    var clock = document.getElementById("hold-clock");
    var card = document.getElementById("hold-timer-card");
    if (!clock || !card) return;

    var mo = new MutationObserver(function () {
      var parts = clock.textContent.split(":").map(Number);
      if (parts.length !== 2 || parts.some(isNaN)) return;
      var totalSeconds = parts[0] * 60 + parts[1];
      card.classList.toggle("pm-urgent", totalSeconds > 0 && totalSeconds <= 60);
    });
    mo.observe(clock, { characterData: true, childList: true, subtree: true });
  }

  /* ------------------------------------------------------------------
     3. Subtle cursor sheen on hero surfaces (rAF throttled)
     ------------------------------------------------------------------ */
  function initHeroSheen() {
    if (reduceMotion) return;
    var hero = document.querySelector(".proof-panel, .checkout-card");
    if (!hero) return;

    var rafId = null;
    hero.addEventListener("pointermove", function (e) {
      if (rafId) return;
      rafId = requestAnimationFrame(function () {
        var rect = hero.getBoundingClientRect();
        var x = ((e.clientX - rect.left) / rect.width) * 100;
        var y = ((e.clientY - rect.top) / rect.height) * 100;
        hero.style.setProperty("--pm-x", x.toFixed(2) + "%");
        hero.style.setProperty("--pm-y", y.toFixed(2) + "%");
        rafId = null;
      });
    });
  }

  onReady(function () {
    try { watchSeatFlips(); } catch (e) {}
    try { watchHoldUrgency(); } catch (e) {}
    try { initHeroSheen(); } catch (e) {}
  });
})();
