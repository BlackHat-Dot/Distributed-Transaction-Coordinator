/* GOLD CURSOR — site-wide, presentation only. Arrow follows exactly; a phrase pill follows with a light lag.
   Labels: add data-cursor="Phrase" to any element (data-cursor="" = no pill). Sensible automatic labels otherwise.
   Text fields / selects keep the native cursor. Touch + reduced-motion: disabled. */
(function () {
  "use strict";
  if (!(window.matchMedia && matchMedia("(hover:hover) and (pointer:fine)").matches)) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var NATIVE = 'textarea,select,[contenteditable=""],[contenteditable="true"],input:not([type=button]):not([type=submit]):not([type=reset]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=file])';
  var ACTIVE = 'a[href],button,[role=button],.btn,.nav-btn,.filter-tab,summary,.seat-tile,input[type=checkbox],input[type=radio],input[type=range],label,[data-cursor]';
  var LAG = 0.22, doc = document.documentElement;
  var cur, pill, txt, x = 0, y = 0, px = 0, py = 0, pw = 0, ph = 0, label = "", last = null, shown = false, raf = 0, swapT = 0, t0 = 0, downAt = 0, upT = 0;

  function build() {
    cur = document.createElement("div"); cur.className = "gc"; cur.setAttribute("aria-hidden", "true");
    cur.innerHTML = '<svg class="gc-arrow" viewBox="0 0 22 22"><defs><linearGradient id="gc-g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffdc92"/><stop offset=".58" stop-color="#ffb020"/><stop offset="1" stop-color="#ff8a3d"/></linearGradient></defs><path d="M3 3l14 6.2-6 2-2.2 6.3z"/></svg>';
    pill = document.createElement("div"); pill.className = "gc-pill"; pill.setAttribute("aria-hidden", "true"); pill.innerHTML = "<span></span>"; txt = pill.firstChild;
    document.body.appendChild(cur); document.body.appendChild(pill);
  }
  function labelFor(t) {
    if (!t || !t.closest) return "";
    var d = t.closest("[data-cursor]"); if (d) return d.getAttribute("data-cursor");
    var s = t.closest(".seat-tile");
    if (s) return { AVAILABLE: "Select seat", SELECTED: "Deselect seat", HELD: "Held", BOOKED: "Booked" }[s.getAttribute("data-status")] || "Seat";
    if (t.closest("#topology-canvas-container") && !t.closest("button,a,input,select,label")) return "Drag \u00B7 scroll to zoom";
    var b = t.closest("button,[role=button],.btn,.nav-btn,.filter-tab,summary");
    if (b) {
      if (b.disabled || b.getAttribute("aria-disabled") === "true") return "Unavailable";
      var w = (b.getAttribute("aria-label") || b.title || b.textContent || "").replace(/\s+/g, " ").trim();
      return w && w.length <= 22 && /[A-Za-z]/.test(w) ? w : "Click";
    }
    if (t.closest("a[href]")) return "Open";
    return "";
  }
  function setLabel(next) {
    if (next === label) return;
    var had = !!label; label = next;
    clearTimeout(swapT);
    if (!next) { pill.classList.remove("on"); return; }
    if (!had) { txt.textContent = next; pill.classList.add("on"); measure(); return; }
    pill.classList.add("swap");
    swapT = setTimeout(function () { txt.textContent = next; pill.classList.remove("swap"); measure(); }, 130);
  }
  function measure() { pw = pill.offsetWidth; ph = pill.offsetHeight; }
  function kick() { if (!raf) { t0 = performance.now(); raf = requestAnimationFrame(tick); } }

  function tick(now) {
    var f = Math.min(50, now - t0) / 16.67; t0 = now;
    cur.style.transform = "translate3d(" + x.toFixed(1) + "px," + y.toFixed(1) + "px,0)";
    var tx = x + 16, ty = y + 20;                                  /* pill target: below-right, flipped near edges */
    if (tx + pw > innerWidth - 8) tx = x - pw - 6;
    if (ty + ph > innerHeight - 8) ty = y - ph - 10;
    var k = 1 - Math.pow(1 - LAG, f);
    px += (tx - px) * k; py += (ty - py) * k;
    var busy = Math.abs(tx - px) > .2 || Math.abs(ty - py) > .2;
    if (!busy) { px = tx; py = ty; }
    pill.style.transform = "translate3d(" + px.toFixed(1) + "px," + py.toFixed(1) + "px,0)";
    raf = busy ? requestAnimationFrame(tick) : 0;
  }
  function onMove(e) {
    if (e.pointerType && e.pointerType !== "mouse") { cur.classList.remove("on"); pill.classList.remove("on"); doc.classList.remove("gc-on"); shown = false; return; }
    x = e.clientX; y = e.clientY;
    if (!shown) { shown = true; doc.classList.add("gc-on"); cur.classList.add("on"); px = x + 16; py = y + 20; }
    var t = e.target;
    if (t !== last) {
      last = t;
      var native = t.closest && t.closest(NATIVE);
      cur.classList.toggle("hide", !!native);
      cur.classList.toggle("act", !native && !!(t.closest && t.closest(ACTIVE)));
      setLabel(native ? "" : labelFor(t));
    }
    kick();
  }
  function init() {
    build();
    addEventListener("pointermove", onMove, { passive: true });
    /* click: the word "DTC" pops up right where clicked and floats up */
    addEventListener("pointerdown", function (e) {
      if ((e.pointerType && e.pointerType !== "mouse") || e.button !== 0) return;
      clearTimeout(upT); downAt = performance.now(); cur.classList.add("down");
      var m = document.createElement("div"); m.className = "gc-mark"; m.setAttribute("aria-hidden", "true"); m.textContent = "DTC";
      m.style.left = e.clientX + "px";
      m.style.top = e.clientY + "px";
      m.addEventListener("animationend", function () { m.remove(); });
      document.body.appendChild(m);
    }, { passive: true });
    addEventListener("pointerup", function () {
      var wait = Math.max(0, 300 - (performance.now() - downAt));   /* arrow returns after the stamp has read */
      clearTimeout(upT); upT = setTimeout(function () { cur.classList.remove("down"); }, wait);
    }, { passive: true });
    doc.addEventListener("mouseleave", function () { cur.classList.remove("on"); pill.classList.remove("on"); label = ""; last = null; });
    doc.addEventListener("mouseenter", function () { if (shown) cur.classList.add("on"); });
    addEventListener("blur", function () { cur.classList.remove("down"); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
