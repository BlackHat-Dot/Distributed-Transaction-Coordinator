/* ==========================================================================
   DTC MISSION CONTROL — PREMIUM MOTION LAYER
   Loaded AFTER app.js / booking.js. This file only ever ADDS classes/DOM to
   elements that already exist — it never reassigns window.* functions, never
   removes listeners, and never mutates STATE. If anything in here throws,
   the dashboard underneath keeps working exactly as it did before.
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

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  /* ------------------------------------------------------------------
     1. Count-up tween for every .stat-num, whenever its real value
        changes (guarded against self-triggered mutations and against
        polling refreshes that re-set the same value).
     ------------------------------------------------------------------ */
  function watchStatNumbers() {
    var nodes = document.querySelectorAll(".stat-num");
    nodes.forEach(function (node) {
      var lastKnown = node.textContent;
      var internal = false;

      var mo = new MutationObserver(function () {
        if (internal) return;
        var now = node.textContent;
        if (now === lastKnown) return;
        var from = lastKnown;
        lastKnown = now;
        if (reduceMotion) return;
        animate(from, now);
      });
      mo.observe(node, { characterData: true, childList: true, subtree: true });

      function animate(fromText, toText) {
        var toMatch = String(toText).match(/^(-?[\d,]*\.?\d+)(.*)$/);
        var fromMatch = String(fromText).match(/^(-?[\d,]*\.?\d+)(.*)$/);
        if (!toMatch) return;

        var toNum = parseFloat(toMatch[1].replace(/,/g, ""));
        var fromNum = fromMatch ? parseFloat(fromMatch[1].replace(/,/g, "")) : toNum;
        var suffix = toMatch[2] || "";
        var decimals = (toMatch[1].split(".")[1] || "").length;
        if (isNaN(toNum) || isNaN(fromNum)) return;

        var duration = 550;
        var start = performance.now();

        function frame(t) {
          var p = Math.min(1, (t - start) / duration);
          var val = fromNum + (toNum - fromNum) * easeOutCubic(p);
          var text = val.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + suffix;

          internal = true;
          node.textContent = text;
          internal = false;
          lastKnown = node.textContent;

          if (p < 1) {
            requestAnimationFrame(frame);
          } else {
            internal = true;
            node.textContent = toText;
            internal = false;
            lastKnown = toText;
            node.classList.add("stat-pop");
            setTimeout(function () { node.classList.remove("stat-pop"); }, 550);
          }
        }
        requestAnimationFrame(frame);
      }
    });
  }

  /* ------------------------------------------------------------------
     2. One orchestrated boot sequence: health nodes and hero stats
        settle into place, staggered, right as the entry overlay clears.
     ------------------------------------------------------------------ */
  function initBootSequence() {
    if (reduceMotion) return;

    function runBoot() {
      var groups = [
        document.querySelectorAll(".health-node"),
        document.querySelectorAll(".stat-card, .stat-pill")
      ];
      var i = 0;
      groups.forEach(function (group, groupIdx) {
        group.forEach(function (el) {
          el.style.setProperty("--pm-delay", (groupIdx * 140 + i * 70) + "ms");
          el.classList.add("pm-boot-in");
          i++;
        });
      });
    }

    var overlay = document.getElementById("dtc-entry-screen");
    if (!overlay) { runBoot(); return; }

    var done = false;
    var mo = new MutationObserver(function () {
      if (done) return;
      if (overlay.classList.contains("fade-out")) {
        done = true;
        mo.disconnect();
        runBoot();
      }
    });
    mo.observe(overlay, { attributes: true, attributeFilter: ["class"] });

    // Safety net: if the overlay never reports fade-out (e.g. skipped
    // entirely, or this page has no entry screen wiring), boot anyway.
    setTimeout(function () {
      if (!done) { done = true; mo.disconnect(); runBoot(); }
    }, 6000);
  }

  /* ------------------------------------------------------------------
     3. Seat tiles: flash a ring exactly when a seat's real status
        flips (your hold landing, or another buyer taking a seat live).
        Hooks tile.dataset.status, which booking.js only ever writes
        when the value actually changes — so this never fires on noise.
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
        // Force reflow so the animation can restart on rapid re-flips.
        void tile.offsetWidth;
        tile.classList.add("pm-flip");
      });
    });
    mo.observe(matrix, { attributes: true, attributeFilter: ["data-status"], subtree: true });
  }

  /* ------------------------------------------------------------------
     4. Hold countdown urgency: last 60 seconds of an ephemeral hold
        get a quiet red glow on the timer card — a real deadline, not
        a decorative one.
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
     5. Button ripple — feedback that answers the click itself.
     ------------------------------------------------------------------ */
  function initButtonRipples() {
    if (reduceMotion) return;

    document.addEventListener("pointerdown", function (e) {
      var btn = e.target.closest(".btn, .health-node");
      if (!btn) return;
      var rect = btn.getBoundingClientRect();
      var size = Math.max(rect.width, rect.height) * 1.6;
      var span = document.createElement("span");
      span.className = "pm-ripple";
      span.style.width = span.style.height = size + "px";
      span.style.left = (e.clientX - rect.left - size / 2) + "px";
      span.style.top = (e.clientY - rect.top - size / 2) + "px";
      btn.appendChild(span);
      span.addEventListener("animationend", function () { span.remove(); });
      setTimeout(function () { if (span.parentNode) span.remove(); }, 700);
    });
  }

  /* ------------------------------------------------------------------
     6. A single restrained cursor sheen on each page's hero surface.
     ------------------------------------------------------------------ */
  function initHeroSheen() {
    if (reduceMotion) return;
    var hero = document.querySelector(".proof-panel, .checkout-card");
    if (!hero) return;

    hero.addEventListener("pointermove", function (e) {
      var rect = hero.getBoundingClientRect();
      var x = ((e.clientX - rect.left) / rect.width) * 100;
      var y = ((e.clientY - rect.top) / rect.height) * 100;
      hero.style.setProperty("--pm-x", x + "%");
      hero.style.setProperty("--pm-y", y + "%");
    });
  }

  onReady(function () {
    try { watchStatNumbers(); } catch (e) {}
    try { initBootSequence(); } catch (e) {}
    try { watchSeatFlips(); } catch (e) {}
    try { watchHoldUrgency(); } catch (e) {}
    try { initButtonRipples(); } catch (e) {}
    try { initHeroSheen(); } catch (e) {}
  });
})();
