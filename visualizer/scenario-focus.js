/* SCENARIO FOCUS LENS v1.1 — presentation only.
   - Blur eases in slowly after a short hover-intent delay (no sudden blur).
   - Row phrases are handed to the global gold cursor (cursor.js) through data-cursor.
   Writes only CSS vars on the rows; one rAF loop, idle when nothing moves. */
(function () {
  "use strict";
  var fine = window.matchMedia && matchMedia("(hover:hover) and (pointer:fine)").matches;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var INTENT = 140;      /* ms the pointer must rest inside before other rows start to blur */
  var RAMP_IN = 0.045;   /* blur ramp per frame (lower = slower, softer) */
  var RAMP_OUT = 0.09;   /* release */
  var FOLLOW = 0.16;     /* row focus smoothing */
  var LABELS = { "btn-demo-happy": "Run happy path", "btn-demo-fail": "Trigger decline", "btn-demo-race": "Fire 5 calls",
                 "btn-demo-crash": "Kill the process", "btn-toggle-custom": "Set payload", "btn-reset-seats": "Restock seats" };
  var box, rows, st, px = 0, py = 0;
  var inside = false, e = 0, enterAt = 0, raf = 0, last = 0;

  function init() {
    box = document.querySelector(".scenario-buttons");
    if (!box || !fine || reduce) return;
    rows = [].slice.call(box.querySelectorAll(".btn-scenario"));
    st = rows.map(function () { return { v: 1, b: 0, mx: 0, r: null }; });
    rows.forEach(function (el) {
      el.setAttribute("data-cursor", LABELS[el.id] || "Run");
      var i = el.querySelector(".scenario-info-btn"); if (i) i.setAttribute("data-cursor", "Details");
    });
    box.addEventListener("pointerenter", function (ev) {
      inside = true; enterAt = performance.now(); px = ev.clientX; py = ev.clientY;
      measure(); document.documentElement.classList.add("sf-in"); kick();
    }, { passive: true });
    box.addEventListener("pointermove", function (ev) { px = ev.clientX; py = ev.clientY; kick(); }, { passive: true });
    box.addEventListener("pointerleave", function () {
      inside = false; document.documentElement.classList.remove("sf-in"); kick();
    }, { passive: true });
    addEventListener("scroll", function () { if (inside) { measure(); kick(); } }, { passive: true });
  }
  function measure() { rows.forEach(function (el, i) { st[i].r = el.getBoundingClientRect(); }); }
  function kick() { if (!raf) { last = performance.now(); raf = requestAnimationFrame(tick); } }

  function tick(now) {
    var dt = Math.min(50, now - last), f60 = dt / 16.67; last = now;
    var ease = function (a) { return 1 - Math.pow(1 - a, f60); }, busy = false;

    /* engagement: waits for hover intent, then ramps up slowly; releases a bit faster */
    var et = inside && now - enterAt > INTENT ? 1 : 0;
    var ne = e + (et - e) * ease(et ? RAMP_IN : RAMP_OUT);
    if (Math.abs(ne - et) < 0.002) ne = et; else busy = true;
    if (inside && !et) busy = true;
    e = ne;

    var k = ease(FOLLOW);
    for (var i = 0; i < rows.length; i++) {
      var s = st[i], r = s.r || (s.r = rows[i].getBoundingClientRect()), f = 1, tb = 0;
      if (inside) {
        var inRow = py >= r.top && py <= r.bottom, d = Math.abs(py - (r.top + r.height / 2));
        var t = inRow ? 1 : Math.max(0, 1 - d / (r.height * 2.1));
        f = 0.12 + 0.88 * (t * t * (3 - 2 * t));
        if (inRow) { s.mx = px - r.left; tb = 1; }
      }
      var tv = 1 - e * (1 - f);                          /* blur only as strong as engagement allows */
      var nv = s.v + (tv - s.v) * k;
      if (Math.abs(nv - tv) > 0.002) busy = true; else nv = tv;
      s.v = nv;
      var nb = s.b + (tb - s.b) * ease(0.2);            /* bump/glow reacts at once, independent of blur intent */
      if (Math.abs(nb - tb) > 0.002) busy = true; else nb = tb;
      s.b = nb;
      var st_ = rows[i].style;
      st_.setProperty("--v", nv.toFixed(3));
      st_.setProperty("--b", nb.toFixed(3));
      st_.setProperty("--mx", s.mx.toFixed(0) + "px");
    }
    raf = busy ? requestAnimationFrame(tick) : 0;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
