/* SCROLL TOUR — presentation layer. Reads the DOM once (clones <main>), never touches app state.
   Trigger: keep scrolling past the bottom of the page (or click the Guided Tour button).
   Flow: page rolls up into a scroll -> teacher walks in carrying it -> hangs it on the board ->
   it unrolls showing your dashboard -> teacher points at each section while a caption explains it. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var html = document.documentElement, body = document.body;

  /* EDIT THESE: one step per dashboard section (selector is looked up inside <main>). */
  var STEPS = [
    { sel: ".proof-panel", title: "1 · Live proof panel",
      text: "Fire real scenarios from here: happy path, payment decline with compensation, a 5x race attack and a crash simulation. The counters above track races defended and row locks taken." },
    { sel: ".topology-section", title: "2 · Service topology",
      text: "Client, Coordinator, Inventory and Payment, each with its own PostgreSQL database, plus Redis for the broker and the mutex. Pulses travel along the edges as RPCs fire." },
    { sel: ".recovery-panel", title: "3 · Crash recovery",
      text: "Celery Beat sweeps every 90 seconds for orphaned sagas and rolls them forward or compensates them. Watch the lease countdown and the recovered counter." },
    { sel: ".tx-feed-panel", title: "4 · Transaction matrix",
      text: "Every saga with its phase waterfall, status and amount. Click a row to open its full audit trail." },
    { sel: ".forensic-panel", title: "5 · Forensic audit stream",
      text: "An append-only SagaEvent log: every transition is recorded with its actor and payload, so history is never overwritten." }
  ];

  var TEACHER = '<svg viewBox="0 0 220 400" aria-hidden="true"><defs><linearGradient id="st-par" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f0dcb4"/><stop offset="1" stop-color="#b99865"/></linearGradient></defs>' +
    '<ellipse cx="110" cy="388" rx="70" ry="9" fill="rgba(0,0,0,.45)"/>' +
    '<g class="st-legL" style="transform-origin:96px 250px"><rect x="84" y="244" width="26" height="124" rx="10" fill="#2a2a33"/><path d="M78 368h40a8 8 0 0 1 8 8v4H70v-4a8 8 0 0 1 8-8z" fill="#17171c"/></g>' +
    '<g class="st-legR" style="transform-origin:124px 250px"><rect x="110" y="244" width="26" height="124" rx="10" fill="#2a2a33"/><path d="M104 368h40a8 8 0 0 1 8 8v4h-56v-4a8 8 0 0 1 8-8z" fill="#17171c"/></g>' +
    '<g class="st-bob">' +
      '<rect x="68" y="128" width="84" height="132" rx="22" fill="#f1f1ee"/>' +
      '<path d="M70 150Q70 135 84 132L110 198 136 132Q150 135 150 150L152 252Q110 268 68 252Z" fill="#70737a"/>' +
      '<circle cx="110" cy="224" r="3" fill="#2f3136"/><circle cx="110" cy="240" r="3" fill="#2f3136"/>' +
      '<rect x="98" y="108" width="24" height="28" rx="8" fill="#e8b996"/>' +
      '<ellipse cx="110" cy="84" rx="33" ry="38" fill="#f4cfae"/><circle cx="77" cy="88" r="7" fill="#eab995"/><circle cx="143" cy="88" r="7" fill="#eab995"/>' +
      '<path d="M76 78Q74 40 112 38 148 40 144 78 136 56 112 56 90 58 76 78Z" fill="#2b2622"/>' +
      '<circle cx="97" cy="86" r="11" fill="rgba(255,255,255,.12)" stroke="#2b2622" stroke-width="3"/><circle cx="123" cy="86" r="11" fill="rgba(255,255,255,.12)" stroke="#2b2622" stroke-width="3"/><path d="M108 86h4" stroke="#2b2622" stroke-width="3"/>' +
      '<circle cx="97" cy="87" r="2.6" fill="#2b2622"/><circle cx="123" cy="87" r="2.6" fill="#2b2622"/>' +
      '<path d="M88 70q9-5 18 0M114 70q9-5 18 0" stroke="#2b2622" stroke-width="3" fill="none" stroke-linecap="round"/>' +
      '<path d="M99 106Q110 114 121 106" stroke="#8a4b3a" stroke-width="3" fill="none" stroke-linecap="round"/>' +
      '<g class="st-prop" transform="rotate(-12 110 212)" style="transition:opacity .3s"><rect x="30" y="200" width="160" height="24" rx="12" fill="url(#st-par)"/><rect x="24" y="196" width="14" height="32" rx="5" fill="#3a2b1d"/><rect x="182" y="196" width="14" height="32" rx="5" fill="#3a2b1d"/></g>' +
      '<g class="st-arm st-armL" style="transform-origin:78px 148px"><rect x="68" y="140" width="20" height="78" rx="10" fill="#f1f1ee"/><circle cx="78" cy="224" r="10" fill="#f4cfae"/></g>' +
      '<g class="st-arm st-armR" style="transform-origin:142px 148px"><rect x="132" y="140" width="20" height="78" rx="10" fill="#f1f1ee"/><circle cx="142" cy="224" r="10" fill="#f4cfae"/></g>' +
    '</g></svg>';

  var busy = false, S = null;
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, reduce ? 0 : ms); }); };
  function anim(el, kf, d, o) {
    return el.animate(kf, Object.assign({ duration: reduce ? 0 : d, fill: "forwards", easing: "cubic-bezier(.4,0,.2,1)" }, o || {})).finished;
  }
  function mk(cls, inner, tag) { var d = document.createElement(tag || "div"); d.className = cls; if (inner) d.innerHTML = inner; return d; }

  /* p = 0..1: the roller descends from the top of the screen and the page above its edge is wrapped into it.
     Implemented by clipping <body> at the roller's edge (the roller + stage live outside <body>). */
  function setP(p) {
    var H = innerHeight, edge = p * H, T = 30 + p * 26;
    S.p = p;
    S.roller.style.display = p > 0 ? "" : "none";
    S.roller.style.height = T + "px";
    S.roller.style.transform = "translateY(" + (edge - T) + "px)";
    body.style.clipPath = p > 0 ? "inset(" + (scrollY + edge) + "px 0 0 0)" : "";
  }
  function animateP(from, to, D) {
    return new Promise(function (res) {
      if (reduce || D <= 0) { setP(to); return res(); }
      var t0 = performance.now();
      (function f(now) {
        var t = Math.min(1, (now - t0) / D), e = t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        setP(from + (to - from) * e);
        t < 1 ? requestAnimationFrame(f) : res();
      })(t0);
    });
  }

  function arms(l, r) {
    if (l != null) S.armL.style.transform = "rotate(" + l + "deg)";
    if (r != null) S.armR.style.transform = "rotate(" + r + "deg)";
  }

  /* Pan the sheet to a section, move the highlight ring, aim the arm at it. */
  function focus(i) {
    var g = S.geo[i]; if (!g) return;
    var SW = S.screen.clientWidth, SH = S.screen.clientHeight;
    var s = Math.min(SW / 1440, (SH - 28) / g.h);          /* zoom so the whole section fits the board */
    var ty = Math.min(0, Math.max(-g.y * s + (SH - g.h * s) / 2, -(S.sheet.offsetHeight * s - SH)));
    var tx = 1440 * s <= SW ? (SW - 1440 * s) / 2 : Math.min(0, Math.max(-g.x * s + (SW - g.w * s) / 2, SW - 1440 * s));
    S.sheet.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + s + ")";
    var r = S.ring.style; r.left = g.x * s + tx - 6 + "px"; r.top = g.y * s + ty - 6 + "px"; r.width = g.w * s + 12 + "px"; r.height = g.h * s + 12 + "px";
    var sr = S.screen.getBoundingClientRect(), tr = S.svg.getBoundingClientRect(), k = tr.width / 220;
    var cx = sr.left + Math.min(sr.width - 20, Math.max(20, (g.x + g.w / 2) * s + tx));
    var cy = sr.top + Math.min(SH - 20, Math.max(20, g.y * s + ty + g.h * s / 2));
    var vx = cx - (tr.left + 142 * k), vy = cy - (tr.top + 148 * k);
    arms(6, Math.max(-170, Math.min(-20, Math.atan2(-vx, vy) * 180 / Math.PI)));
    S.cur = i;
    S.title.textContent = STEPS[i].title; S.text.textContent = STEPS[i].text;
    S.dots.forEach(function (d, n) { d.classList.toggle("on", n === i); });
    S.back.disabled = i === 0;
    S.next.textContent = i === STEPS.length - 1 ? "Finish" : "Next";
  }

  function measure() {
    S.geo = STEPS.map(function (st) {
      var el = S.sheet.querySelector(st.sel); if (!el) return null;
      var r = el.getBoundingClientRect(), sr = S.sheet.getBoundingClientRect(), s = sr.width / 1440;
      return { x: (r.left - sr.left) / s, y: (r.top - sr.top) / s, w: r.width / s, h: r.height / s };
    });
  }

  function build() {
    var main = document.querySelector("main"); if (!main) return false;
    busy = true; S = { p: 0, o: 0 };
    /* inert snapshot of the dashboard (ids stripped so nothing collides with the live page) */
    var clone = main.cloneNode(true);
    clone.querySelectorAll("[id]").forEach(function (n) { n.removeAttribute("id"); });
    clone.querySelectorAll("[onclick]").forEach(function (n) { n.removeAttribute("onclick"); });
    clone.setAttribute("inert", ""); clone.setAttribute("aria-hidden", "true");

    var root = mk("", "", "div"); root.id = "st-root"; root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true"); root.setAttribute("aria-label", "Guided tour");
    root.innerHTML =
      '<div class="st-stage"><div class="st-teacher">' + TEACHER + '</div>' +
      '<div class="st-board"><div class="st-frame"><div class="st-chalk"></div><div class="st-screen"><div class="st-sheet"></div><div class="st-ring"></div></div><div class="st-rod"></div><div class="st-pull"></div></div></div></div>' +
      '<div class="st-card"><h4></h4><p></p><div class="st-foot"><div class="st-dots">' + STEPS.map(function () { return "<i></i>"; }).join("") + '</div>' +
      '<div class="st-btns"><button class="btn btn-outline btn-sm st-skip">Skip</button><button class="btn btn-outline btn-sm st-back">Back</button><button class="btn btn-primary btn-sm st-next">Next</button></div></div></div>';
    var q = function (s) { return root.querySelector(s); };
    S.root = root; S.roller = mk(""); S.roller.id = "st-roller";
    S.teacher = q(".st-teacher"); S.svg = q(".st-teacher svg"); S.armL = q(".st-armL"); S.armR = q(".st-armR"); S.prop = q(".st-prop");
    S.screen = q(".st-screen"); S.sheet = q(".st-sheet"); S.ring = q(".st-ring"); S.rod = q(".st-rod"); S.pull = q(".st-pull");
    S.card = q(".st-card"); S.title = q("h4"); S.text = q("p"); S.dots = [].slice.call(root.querySelectorAll(".st-dots i"));
    S.back = q(".st-back"); S.next = q(".st-next");
    S.sheet.appendChild(clone);
    html.appendChild(root); html.appendChild(S.roller); /* outside <body>, so the body clip never hides them */
    S.roller.style.display = "none";
    arms(-35, 35); /* carrying pose */
    measure();

    S.next.onclick = function () { S.cur === STEPS.length - 1 ? end() : focus(S.cur + 1); };
    S.back.onclick = function () { focus(S.cur - 1); };
    q(".st-skip").onclick = end;
    S.key = function (e) {
      if (e.key === "Escape") end();
      else if (e.key === "ArrowRight") S.next.click();
      else if (e.key === "ArrowLeft" && S.cur > 0) S.back.click();
    };
    S.resize = function () { if (S.ready) { measure(); focus(S.cur); } };
    return true;
  }
  function destroy(afterTour) {
    body.style.clipPath = ""; html.style.overflow = "";
    if (S) { S.root.remove(); S.roller.remove(); }
    S = null; busy = false; if (afterTour) cool = performance.now() + 1200;
  }

  async function play() {
    var root = S.root, q = function (s) { return root.querySelector(s); };
    S.playing = true; html.style.overflow = "hidden"; hint(false);
    document.addEventListener("keydown", S.key); addEventListener("resize", S.resize);
    await animateP(S.p, 1, 950 * (1 - S.p) + 250);   /* 1. rest of the page wraps into the roll */
    root.classList.add("on"); await wait(380);       /* 2. stage fades in */
    S.roller.style.display = "none";
    var tx = -(S.teacher.getBoundingClientRect().right + 40);
    S.teacher.classList.add("st-walking");           /* 3. teacher walks in with the scroll */
    root.classList.add("st-walking");
    await anim(S.teacher, [{ transform: "translateX(" + tx + "px)" }, { transform: "translateX(0)" }], 1800, { easing: "cubic-bezier(.25,.6,.35,1)" });
    root.classList.remove("st-walking");
    await wait(250);
    arms(null, -155); await wait(600);               /* 4. hangs it on the board */
    S.prop.style.opacity = 0; S.rod.style.opacity = 1; S.pull.style.opacity = 1; await wait(300);
    var H = S.screen.clientHeight;                   /* 5. unrolls */
    anim(S.pull, [{ transform: "translateY(0)" }, { transform: "translateY(" + (H - 8) + "px)" }], 1300);
    await anim(S.screen, [{ clipPath: "inset(0 0 100% 0)" }, { clipPath: "inset(0 0 0% 0)" }], 1300);
    S.pull.style.opacity = 0;
    focus(0); S.ready = true; S.card.classList.add("show"); /* 6. explain */
  }

  async function end() {
    if (!S || !S.ready) return; /* ignore Esc while the intro is still animating */
    S.ready = false;
    document.removeEventListener("keydown", S.key); removeEventListener("resize", S.resize);
    S.card.classList.remove("show");
    S.ring.style.opacity = 0;
    await anim(S.screen, [{ clipPath: "inset(0 0 0% 0)" }, { clipPath: "inset(0 0 100% 0)" }], 700);
    S.root.classList.remove("on"); await wait(380);
    S.roller.style.display = "";
    await animateP(1, 0, 950);                       /* page unrolls back */
    destroy(true);
  }
  function start() { if (busy || !build()) return; play(); }

  /* ---- scroll-driven trigger: keep scrolling past the bottom and the page wraps up ---- */
  var LIM = 600, cool = 0, rel = 0, lastY = 0, hintEl = null;
  function atBottom() { return scrollY + innerHeight >= document.documentElement.scrollHeight - 2; }
  function hint(on) { if (hintEl) hintEl.classList.toggle("show", !!on); }
  function pull(dy) {
    if (S && S.playing) return false;
    if (!S) { if (reduce || performance.now() < cool || !atBottom() || dy <= 0 || busy) return false; if (!build()) return false; }
    hint(false);
    S.o = Math.max(0, Math.min(LIM, S.o + dy));
    setP(S.o / LIM);
    clearTimeout(rel);
    if (S.o >= LIM) { play(); return true; }
    rel = setTimeout(async function () {            /* let go early -> springs back */
      if (!S || S.playing) return;
      await animateP(S.p, 0, 400 * S.p + 60); destroy(); hint(atBottom());
    }, 650);
    return true;
  }
  addEventListener("wheel", function (e) {
    var dy = e.deltaY * (e.deltaMode === 1 ? 16 : 1);
    if (pull(dy)) e.preventDefault();
  }, { passive: false });
  addEventListener("touchstart", function (e) { lastY = e.touches[0].clientY; }, { passive: true });
  addEventListener("touchmove", function (e) {
    var y = e.touches[0].clientY, dy = (lastY - y) * 1.4; lastY = y;
    if (pull(dy) && e.cancelable) e.preventDefault();
  }, { passive: false });
  addEventListener("scroll", function () { if (!S) hint(!reduce && atBottom() && performance.now() > cool); }, { passive: true });

  function addButton() {
    var anchor = document.getElementById("btn-view-stack");
    if (!anchor || document.getElementById("btn-scroll-tour")) return;
    var b = document.createElement("button");
    b.id = "btn-scroll-tour"; b.className = "btn btn-outline"; b.title = "Animated guided tour of this dashboard";
    b.innerHTML = "\uD83D\uDCDC Guided Tour";
    b.addEventListener("click", start);
    anchor.insertAdjacentElement("afterend", b);
  }
  function init() {
    addButton();
    hintEl = document.createElement("div"); hintEl.id = "st-hint"; hintEl.setAttribute("aria-hidden", "true");
    hintEl.textContent = "Keep scrolling to roll up the page \uD83D\uDCDC"; body.appendChild(hintEl);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  window.startScrollTour = start;
})();
