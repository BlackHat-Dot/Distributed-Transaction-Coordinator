/* SCROLL TOUR (inline) — lives INSIDE the page, between <main> and your footer. Nothing is fullscreen or locked.
   As the visitor scrolls through the section, a sticky stage plays the story, driven directly by scroll position
   (scroll back and it rewinds). When the story ends, the stage releases and the page continues to the footer.

   Story (in "screens" of scroll):
   0.0-1.2  the page winds up into a roll at the top        2.4-3.6  she walks to the board
   1.0-1.5  the roll gathers above her hand                  3.6-4.3  she hangs the roll on the board
   1.2-2.0  it drops into her raised hand                    4.3-5.0  it unrolls showing the dashboard
   2.0-2.4  she catches it and lowers it                     5.0+     she stands still and teaches, 1 step per 0.9 screen

   Placeholder:  <section id="st-tour"></section> in index.html (created automatically after <main> if missing).
   Presentation only: it clones <main> and never touches app state. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ===== EDIT THESE: one step per dashboard section (selector is looked up inside <main>) ===== */
  var STEPS = [
    { sel: ".proof-panel", title: "1 · Live proof panel",
      text: "EXAMPLE — Fire real scenarios from here: happy path, payment decline with compensation, a 5x race attack and a crash simulation." },
    { sel: ".topology-section", title: "2 · Service topology",
      text: "EXAMPLE — Client, Coordinator, Inventory and Payment, each with its own database, plus Redis for the broker and the mutex." },
    { sel: ".recovery-panel", title: "3 · Crash recovery",
      text: "EXAMPLE — Celery Beat sweeps for orphaned sagas and rolls them forward or compensates them." },
    { sel: ".tx-feed-panel", title: "4 · Transaction matrix",
      text: "EXAMPLE — Every saga with its phase waterfall, status and amount. Click a row to open its audit trail." },
    { sel: ".forensic-panel", title: "5 · Forensic audit stream",
      text: "EXAMPLE — An append-only event log: every transition is recorded with its actor and payload." }
  ];
  var INTRO = 5.0, PER = 0.9, N = STEPS.length, TOTAL = INTRO + PER * N;   /* TOTAL = screens of scroll the tour uses */

  /* Side-profile teacher facing right. viewBox 270x400, shoulder (104,140), hip (102,240). */
  var TEACHER = '<svg viewBox="0 0 270 400" aria-hidden="true"><defs>' +
    '<linearGradient id="st-par" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f4e3bd"/><stop offset=".55" stop-color="#d8bd8a"/><stop offset="1" stop-color="#a98a58"/></linearGradient></defs>' +
    '<ellipse cx="108" cy="384" rx="62" ry="8" fill="rgba(0,0,0,.45)"/>' +
    '<g class="st-legB" style="transform-origin:102px 240px;transform:rotate(5deg)"><rect x="91" y="236" width="24" height="126" rx="10" fill="#1f2027"/><path d="M89 358h26q18 2 24 14v8H89z" fill="#0e0e12"/></g>' +
    '<g class="st-legA" style="transform-origin:102px 240px;transform:rotate(-5deg)"><rect x="91" y="236" width="24" height="126" rx="10" fill="#2e303b"/><path d="M89 358h26q18 2 24 14v8H89z" fill="#17171c"/></g>' +
    '<g class="st-bob">' +
      '<rect x="78" y="118" width="50" height="130" rx="22" fill="#f1f1ee"/>' +
      '<rect x="78" y="150" width="50" height="98" rx="18" fill="#70737a"/>' +
      '<path d="M121 128l5 8-5 30-5-30z" fill="#9a3b32"/>' +
      '<rect x="96" y="102" width="18" height="26" rx="7" fill="#e8b996"/>' +
      '<ellipse cx="104" cy="82" rx="28" ry="32" fill="#f4cfae"/>' +
      '<path d="M130 86l9 9-9 3z" fill="#eab995"/><circle cx="92" cy="88" r="6" fill="#eab995"/>' +
      '<path d="M74 88Q64 40 104 38Q135 38 134 64Q118 50 100 58Q88 64 86 94Z" fill="#2b2622"/>' +
      '<circle cx="118" cy="82" r="9" fill="rgba(255,255,255,.14)" stroke="#2b2622" stroke-width="3"/><path d="M109 82H92" stroke="#2b2622" stroke-width="3"/><circle cx="120" cy="83" r="2.4" fill="#2b2622"/>' +
      '<path d="M111 70q7-4 15 0" stroke="#2b2622" stroke-width="3" fill="none" stroke-linecap="round"/>' +
      '<path d="M118 102q6 3 12-1" stroke="#8a4b3a" stroke-width="3" fill="none" stroke-linecap="round"/>' +
      '<g class="st-roll" style="opacity:0;transform:translate(190px,-900px)"><rect x="-62" y="-9" width="124" height="18" rx="9" fill="url(#st-par)"/>' +
        '<path d="M-56 -3H56M-56 3H56" stroke="#8a6d42" stroke-opacity=".45" stroke-width="1"/>' +
        '<rect x="-68" y="-8" width="10" height="16" rx="4" fill="#3a2b1d"/><rect x="58" y="-8" width="10" height="16" rx="4" fill="#3a2b1d"/></g>' +
      '<g class="st-uA" style="transform-origin:104px 140px;transform:rotate(5deg)"><rect x="95" y="134" width="18" height="52" rx="9" fill="#e9e9e6"/>' +
        '<g class="st-fA" style="transform-origin:104px 180px;transform:rotate(5deg)"><rect x="96" y="174" width="16" height="48" rx="8" fill="#f4cfae"/><circle cx="104" cy="226" r="9" fill="#f4cfae"/></g></g>' +
    '</g></svg>';
  var VBW = 270, SHX = 104, SHY = 140;

  var S = null, sd = 0, st = 0, raf = 0, pose = "", idx = -2, cloned = false;
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };
  var seg = function (s, a, b) { return clamp((s - a) / (b - a), 0, 1); };
  var ease = function (t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  var quad = function (t) { return t < .5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t); };
  function mk(cls, inner, tag) { var d = document.createElement(tag || "div"); d.className = cls; if (inner) d.innerHTML = inner; return d; }

  function mkClone() {
    var c = document.querySelector("main").cloneNode(true);
    c.querySelectorAll("[id]").forEach(function (n) { n.removeAttribute("id"); });
    c.querySelectorAll("[onclick]").forEach(function (n) { n.removeAttribute("onclick"); });
    c.setAttribute("inert", ""); c.setAttribute("aria-hidden", "true");
    return c;
  }

  function arms(u, f) {
    if (u != null) S.uA.style.transform = "rotate(" + u + "deg)";
    if (f != null) S.fA.style.transform = "rotate(" + f + "deg)";
  }
  function aim(cx, cy) {
    var tr = S.svg.getBoundingClientRect(), k = tr.width / VBW;
    var vx = cx - (tr.left + SHX * k), vy = cy - (tr.top + SHY * k);
    return Math.max(-170, Math.min(-20, Math.atan2(-vx, vy) * 180 / Math.PI));
  }

  function build() {
    if (!document.querySelector("main")) return false;
    var host = document.getElementById("st-tour");
    if (!host) { host = document.createElement("section"); host.id = "st-tour"; document.querySelector("main").insertAdjacentElement("afterend", host); }
    host.className = "st-tour"; host.setAttribute("aria-label", "Guided tour of the dashboard");
    host.innerHTML =
      '<div class="st-track"><div class="st-pin">' +
        '<div class="st-stage"><div class="st-teacher">' + TEACHER + '</div>' +
        '<div class="st-board"><div class="st-frame"><div class="st-chalk"></div><div class="st-screen"><div class="st-sheet"></div><div class="st-ring"></div></div><div class="st-rod"></div><div class="st-pull"></div></div></div></div>' +
        '<div class="st-edge"></div><div class="st-paper"><div class="st-paper-in"></div></div>' +
        '<div class="st-roller"><i class="st-cap l"></i><i class="st-cap r"></i></div>' +
        '<div class="st-card"><h4></h4><p></p><div class="st-foot"><div class="st-dots">' + STEPS.map(function () { return "<i></i>"; }).join("") + '</div>' +
        '<div class="st-btns"><button class="btn btn-outline btn-sm st-skip">Skip tour</button><button class="btn btn-outline btn-sm st-back">Back</button><button class="btn btn-primary btn-sm st-next">Next</button></div></div></div>' +
      '</div></div>';
    var q = function (s) { return host.querySelector(s); };
    S = { host: host, track: q(".st-track"), pin: q(".st-pin"), teacher: q(".st-teacher"), svg: q(".st-teacher svg"),
      uA: q(".st-uA"), fA: q(".st-fA"), legA: q(".st-legA"), legB: q(".st-legB"), bob: q(".st-bob"), roll: q(".st-roll"),
      screen: q(".st-screen"), sheet: q(".st-sheet"), ring: q(".st-ring"), rod: q(".st-rod"), pull: q(".st-pull"), frame: q(".st-frame"),
      paper: q(".st-paper"), paperIn: q(".st-paper-in"), roller: q(".st-roller"), edge: q(".st-edge"),
      card: q(".st-card"), title: q("h4"), text: q("p"), dots: [].slice.call(host.querySelectorAll(".st-dots i")),
      back: q(".st-back"), next: q(".st-next") };
    S.paperIn.appendChild(mkClone()); S.sheet.appendChild(mkClone());
    S.next.onclick = function () { goto(idx >= N - 1 ? TOTAL + .9 : INTRO + PER * (Math.max(idx, -1) + 1) + .05); };
    S.back.onclick = function () { goto(INTRO + PER * (idx - 1) + .05); };
    q(".st-skip").onclick = function () { goto(TOTAL + .9); };
    return true;
  }

  function goto(sv) {
    var top = S.track.getBoundingClientRect().top + scrollY + sv * innerHeight;
    scrollTo({ top: top, behavior: reduce ? "auto" : "smooth" });
  }

  /* measure everything once per resize / content refresh */
  function layout() {
    var vh = innerHeight;
    S.track.style.height = (TOTAL + 1) * vh + "px";
    S.teacher.style.transform = ""; S.bob.style.transform = "";
    /* board takes the shape of the full dashboard so nothing is cropped */
    var h = S.sheet.offsetHeight || 900;
    S.pin.style.setProperty("--st-ar", clamp(1440 / h, .45, 1.7).toFixed(3));
    var nat = S.svg.getBoundingClientRect(), pr = S.pin.getBoundingClientRect(), fr = S.frame.getBoundingClientRect();
    S.k = nat.width / VBW; S.dist = Math.max(0, nat.left - 14); S.topRel = nat.top - pr.top; S.hx = 14 + 190 * S.k;
    S.bx = (fr.left + fr.width / 2 - nat.left) / S.k; S.by = (fr.top + 16 - nat.top) / S.k; S.sx = (fr.width + 20) / (136 * S.k);
    S.aimRail = aim(fr.left + fr.width / 2, fr.top + 16);
    /* sheet fitted whole inside the board */
    var SW = S.screen.clientWidth, SH = S.screen.clientHeight, fs = Math.min(SW / 1440, SH / h);
    S.fs = fs; S.sheet.style.transform = "translate(" + (SW - 1440 * fs) / 2 + "px," + (SH - h * fs) / 2 + "px) scale(" + fs + ")";
    S.geo = STEPS.map(function (stp) {
      var el = S.sheet.querySelector(stp.sel); if (!el) return null;
      var r = el.getBoundingClientRect(), sr = S.sheet.getBoundingClientRect(), s = sr.width / 1440;
      return { x: (r.left - sr.left) / s, y: (r.top - sr.top) / s, w: r.width / s, h: r.height / s };
    });
    /* paper: the page clone, scaled to the viewport width and bottom-aligned so it continues the real page */
    var sc = innerWidth / 1440, ph = S.paperIn.offsetHeight * sc;
    S.paperIn.style.transform = "translateY(" + Math.min(0, vh - ph) + "px) scale(" + sc + ")";
    pose = ""; idx = -2; render(sd);
  }

  function focus(i) {
    var g = S.geo[i]; if (!g) return;
    var fs = S.fs, SW = S.screen.clientWidth, SH = S.screen.clientHeight, h = S.sheet.offsetHeight;
    var tx = (SW - 1440 * fs) / 2, ty = (SH - h * fs) / 2, r = S.ring.style;
    r.opacity = 1; r.left = g.x * fs + tx - 6 + "px"; r.top = g.y * fs + ty - 6 + "px"; r.width = g.w * fs + 12 + "px"; r.height = g.h * fs + 12 + "px";
    var sr = S.screen.getBoundingClientRect();
    arms(aim(sr.left + g.x * fs + tx + g.w * fs / 2, sr.top + g.y * fs + ty + g.h * fs / 2), -6);
    S.title.textContent = STEPS[i].title; S.text.textContent = STEPS[i].text;
    S.dots.forEach(function (d, n) { d.classList.toggle("on", n === i); });
    S.back.disabled = i === 0;
    S.next.textContent = i === N - 1 ? "Continue" : "Next";
  }

  /* ---- the whole story as a pure function of scroll position s (in screens) ---- */
  function render(s) {
    var H = innerHeight, k = S.k, dist = S.dist;
    /* wind-up */
    var pA = seg(s, 0, 1.2), eA = ease(pA), pB = seg(s, 1.0, 1.5);
    S.paper.style.display = s < 1.2 ? "" : "none";
    S.paper.style.transform = "translateY(" + -eA * H + "px)";
    S.edge.style.display = pA > 0 && pA < 1 ? "" : "none";
    S.edge.style.top = H * (1 - eA) + "px";
    S.roller.style.display = s > 0 && s < 1.5 ? "" : "none";
    S.roller.style.height = 22 + eA * 56 + "px";
    S.roller.style.transformOrigin = S.hx + "px 0";
    S.roller.style.transform = "scaleX(" + (1 - .9 * ease(pB)) + ")";
    S.roller.style.opacity = pB > .75 ? 1 - (pB - .75) / .25 : 1;

    /* roll: drop -> catch -> carry -> hang */
    var x = 190, y, rot, sc = 1;
    if (s < 2.0) {
      var pC = seg(s, 1.2, 2.0), y0 = -(S.topRel / k) - 80;
      if (pC < .78) { var t = pC / .78; y = y0 + (126 - y0) * t * t; rot = 190 + (-196) * ease(t); }
      else if (pC < .9) { y = 126 + 12 * quad((pC - .78) / .12); rot = -6; }
      else { y = 138 - 6 * quad((pC - .9) / .1); rot = -6; }
    } else if (s < 3.8) {
      var eD = ease(seg(s, 2.0, 2.4)); y = 132 + 58 * eD; rot = -6 - 6 * eD;
    } else {
      var q = ease(seg(s, 3.8, 4.3));
      x = 190 + (S.bx - 190) * q; y = 190 + (S.by - 190) * q; rot = -12 * (1 - q); sc = 1 + (S.sx - 1) * q;
    }
    S.roll.style.opacity = s >= 1.2 && s < 4.3 ? 1 : 0;
    S.roll.style.transform = "translate(" + x + "px," + y + "px) rotate(" + rot + "deg) scale(" + sc + ",1)";

    /* walk: position and gait share one clock so she plants her feet exactly at the end */
    var pE = seg(s, 2.4, 3.6), e = quad(pE), env = pE > 0 && pE < 1 ? Math.min(1, pE * 6, (1 - pE) * 6) : 0;
    var ph = dist * e / (150 * k) * 2 * Math.PI, sn = Math.sin(ph);
    S.teacher.style.transform = "translateX(" + -dist * (1 - e) + "px)";
    S.legA.style.transform = "rotate(" + (-5 + sn * 20 * env) + "deg)";
    S.legB.style.transform = "rotate(" + (5 - sn * 20 * env) + "deg)";
    S.bob.style.transform = "translateY(" + -Math.abs(Math.cos(ph)) * 2.2 * env + "px)";

    /* board: rod, then unroll */
    var pG = seg(s, 4.3, 5.0), eG = ease(pG);
    S.rod.style.opacity = s >= 4.2 ? 1 : 0;
    S.pull.style.opacity = s >= 4.2 && pG < 1 ? 1 : 0;
    S.pull.style.transform = "translateY(" + eG * (S.screen.clientHeight - 8) + "px)";
    S.screen.style.clipPath = "inset(0 0 " + (1 - eG) * 100 + "% 0)";

    /* arm poses (arms ease between poses with CSS) */
    var want = s < 2.0 ? "up" : s < 3.6 ? "carry" : s < 4.3 ? "rail" : s < INTRO ? "rest" : "teach";
    if (want !== pose) {
      pose = want;
      if (want === "up") arms(-84, -16); else if (want === "carry") arms(-25, -55);
      else if (want === "rail") arms(S.aimRail, -8); else if (want === "rest") arms(6, 6);
    }
    var i = s >= INTRO ? Math.min(N - 1, Math.floor((s - INTRO) / PER)) : -1;
    S.card.classList.toggle("show", s >= INTRO - .05);
    if (i !== idx) { idx = i; if (i >= 0) focus(i); else S.ring.style.opacity = 0; }
  }

  /* scroll -> target; displayed value chases it a little each frame (the friction / inertia) */
  function onScroll() {
    if (!S) return;
    st = clamp(-S.track.getBoundingClientRect().top / innerHeight, 0, TOTAL);
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function tick() {
    raf = 0;
    if (!cloned && st > 0) { cloned = true; S.paperIn.replaceChildren(mkClone()); S.sheet.replaceChildren(mkClone()); layout(); }  /* fresh snapshot of live data */
    if (cloned && st === 0 && sd < .001) cloned = false;
    var d = st - sd;
    sd = reduce || Math.abs(d) < .0005 ? st : sd + d * .1;
    render(sd);
    if (sd !== st) raf = requestAnimationFrame(tick);
  }

  function addButton() {
    var anchor = document.getElementById("btn-view-stack");
    if (!anchor || document.getElementById("btn-scroll-tour")) return;
    var b = document.createElement("button");
    b.id = "btn-scroll-tour"; b.className = "btn btn-outline"; b.title = "Jump to the guided tour of this dashboard";
    b.innerHTML = "\uD83D\uDCDC Guided Tour";
    b.addEventListener("click", function () { goto(0); });
    anchor.insertAdjacentElement("afterend", b);
  }

  function init() {
    if (!build()) return;
    addButton(); layout();
    var t; addEventListener("resize", function () { clearTimeout(t); t = setTimeout(function () { layout(); onScroll(); }, 120); });
    addEventListener("scroll", onScroll, { passive: true });
    addEventListener("load", function () { layout(); onScroll(); });
    onScroll();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  window.startScrollTour = function () { if (S) goto(0); };
})();
