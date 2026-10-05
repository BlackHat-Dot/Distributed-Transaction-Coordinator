/* SCENARIO FOCUS LENS — presentation only. Writes CSS vars (--v focus, --b bump, --mx glow x)
   on the scenario rows. One rAF loop, runs only while moving/settling, lerp-smoothed. */
(function () {
  "use strict";
  var box, rows, st = [], px = 0, py = 0, inside = false, raf = 0, last = 0;
  var fine = window.matchMedia && matchMedia("(hover:hover) and (pointer:fine)").matches;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function init() {
    box = document.querySelector(".scenario-buttons");
    if (!box || !fine || reduce) return;
    rows = [].slice.call(box.querySelectorAll(".btn-scenario"));
    st = rows.map(function () { return { v: 1, b: 0, mx: 0, tv: 1, r: null }; });
    box.addEventListener("pointerenter", function (e) { inside = true; measure(); move(e); }, { passive: true });
    box.addEventListener("pointermove", move, { passive: true });
    box.addEventListener("pointerleave", function () { inside = false; kick(); }, { passive: true });
    addEventListener("scroll", function () { if (inside) { measure(); kick(); } }, { passive: true });
  }
  function measure() { rows.forEach(function (el, i) { st[i].r = el.getBoundingClientRect(); }); }
  function move(e) { px = e.clientX; py = e.clientY; kick(); }
  function kick() { if (!raf) { last = performance.now(); raf = requestAnimationFrame(tick); } }
  function tick(now) {
    var dt = Math.min(50, now - last); last = now;
    var k = 1 - Math.pow(1 - 0.16, dt / 16.67), busy = false;
    for (var i = 0; i < rows.length; i++) {
      var s = st[i], r = s.r || (s.r = rows[i].getBoundingClientRect());
      if (inside) {
        var cy = r.top + r.height / 2, d = Math.abs(py - cy);
        var inRow = py >= r.top && py <= r.bottom;
        s.tv = inRow ? 1 : Math.max(0, 1 - d / (r.height * 2.1));
        s.tv = s.tv * s.tv * (3 - 2 * s.tv);             /* smoothstep falloff */
        s.tv = 0.12 + 0.88 * s.tv;                        /* never fully gone */
        if (inRow) s.mx = px - r.left;
      } else s.tv = 1;
      var nv = s.v + (s.tv - s.v) * k;
      var nb = Math.pow(nv, 9) * (inside || nv < 0.999 ? 1 : 0);
      if (Math.abs(nv - s.tv) > 0.002) busy = true; else nv = s.tv;
      s.v = nv; s.b = nb;
      var el = rows[i].style;
      el.setProperty("--v", nv.toFixed(3));
      el.setProperty("--b", (inside || nv < 0.999 ? nb : 0).toFixed(3));
      el.setProperty("--mx", s.mx.toFixed(0) + "px");
    }
    raf = (busy || inside) ? requestAnimationFrame(tick) : 0;
    if (inside && !busy) raf = 0;                         /* idle until the next pointermove */
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
