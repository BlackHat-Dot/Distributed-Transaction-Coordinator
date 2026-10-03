/* SCROLL TOUR — presentation layer. Reads the DOM once (clones <main>), never touches app state.
   Trigger: keep scrolling past the bottom of the page (or click the Guided Tour button).
   Flow:
   1. The page slides up and winds into a roll at the top of the screen (eased, frame-smoothed).
   2. The roll drops from the top into the teacher's raised hand; she catches it.
   3. She walks to the board (gait is driven by distance, so she stops cleanly), then stands still.
   4. She hangs the roll on the board, it unrolls, and she teaches with arm gestures only. */
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

  /* Side-profile teacher facing right. viewBox 270x400, shoulder at (104,140), hip at (102,240).
     Arms are two-segment (upper + forearm) so poses read naturally; the roll is a separate group so it can fly. */
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

  var busy = false, S = null, cool = 0;
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, reduce ? 0 : ms); }); };
  function anim(el, kf, d, o) {
    return el.animate(kf, Object.assign({ duration: reduce ? 0 : d, fill: "forwards", easing: "cubic-bezier(.4,0,.2,1)" }, o || {})).finished;
  }
  function mk(cls, inner, tag) { var d = document.createElement(tag || "div"); d.className = cls; if (inner) d.innerHTML = inner; return d; }
  var ease = function (t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };

  /* ---- the wind-up. p = 0..1.
     The page slides UP (body.top) and disappears into the roll fixed at the top edge; the roll thickens as
     more paper is wound on. A shadow under the roll and a shadow at the page's rising lower edge sell the depth. */
  function setP(p) {
    var H = innerHeight, edge = p * H;
    S.p = p;
    S.roller.style.display = S.edge.style.display = p > 0 ? "" : "none";
    S.roller.style.height = 22 + p * 56 + "px";
    S.edge.style.top = H - edge + "px";
    body.style.top = -edge + "px";
  }
  function animateP(from, to, D) {
    return new Promise(function (res) {
      if (reduce || D <= 0) { setP(to); return res(); }
      var t0 = performance.now();
      (function f(now) {
        var t = Math.min(1, (now - t0) / D);
        setP(from + (to - from) * ease(t));
        t < 1 ? requestAnimationFrame(f) : res();
      })(t0);
    });
  }
  /* smoothing loop for the manual scroll-pull: p chases the target a little each frame */
  function drive() {
    if (S.raf) return;
    (function loop() {
      if (!S || S.playing) return;
      var d = S.target - S.p;
      if (Math.abs(d) < .0008) {
        S.raf = 0; setP(S.target);
        if (S.target >= 1) play();
        else if (S.target <= 0 && S.released) { destroy(); hint(atBottom()); }
        return;
      }
      setP(S.p + d * .14);
      S.raf = requestAnimationFrame(loop);
    })();
  }

  function arms(u, f) {
    if (u != null) S.uA.style.transform = "rotate(" + u + "deg)";
    if (f != null) S.fA.style.transform = "rotate(" + f + "deg)";
  }
  /* shoulder angle that points the arm at a screen coordinate */
  function aim(cx, cy) {
    var tr = S.svg.getBoundingClientRect(), k = tr.width / VBW;
    var vx = cx - (tr.left + SHX * k), vy = cy - (tr.top + SHY * k);
    return Math.max(-170, Math.min(-20, Math.atan2(-vx, vy) * 180 / Math.PI));
  }

  /* Pan the sheet to a section, move the highlight ring, aim the arm at it. */
  function focus(i) {
    var g = S.geo[i]; if (!g) return;
    var SW = S.screen.clientWidth, SH = S.screen.clientHeight;
    var s = Math.min(SW / 1440, (SH - 28) / g.h);
    var ty = Math.min(0, Math.max(-g.y * s + (SH - g.h * s) / 2, -(S.sheet.offsetHeight * s - SH)));
    var tx = 1440 * s <= SW ? (SW - 1440 * s) / 2 : Math.min(0, Math.max(-g.x * s + (SW - g.w * s) / 2, SW - 1440 * s));
    S.sheet.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + s + ")";
    var r = S.ring.style; r.opacity = 1; r.left = g.x * s + tx - 6 + "px"; r.top = g.y * s + ty - 6 + "px"; r.width = g.w * s + 12 + "px"; r.height = g.h * s + 12 + "px";
    var sr = S.screen.getBoundingClientRect();
    var cx = sr.left + Math.min(sr.width - 20, Math.max(20, (g.x + g.w / 2) * s + tx));
    var cy = sr.top + Math.min(SH - 20, Math.max(20, g.y * s + ty + g.h * s / 2));
    arms(aim(cx, cy), -6);
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
    busy = true; S = { p: 0, o: 0, target: 0 };
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
    S.root = root;
    S.roller = mk("", '<i class="st-cap l"></i><i class="st-cap r"></i>'); S.roller.id = "st-roller";
    S.edge = mk(""); S.edge.id = "st-edge";
    S.teacher = q(".st-teacher"); S.svg = q(".st-teacher svg");
    S.uA = q(".st-uA"); S.fA = q(".st-fA"); S.legA = q(".st-legA"); S.legB = q(".st-legB"); S.bob = q(".st-bob"); S.roll = q(".st-roll");
    S.screen = q(".st-screen"); S.sheet = q(".st-sheet"); S.ring = q(".st-ring"); S.rod = q(".st-rod"); S.pull = q(".st-pull");
    S.card = q(".st-card"); S.title = q("h4"); S.text = q("p"); S.dots = [].slice.call(root.querySelectorAll(".st-dots i"));
    S.back = q(".st-back"); S.next = q(".st-next");
    S.sheet.appendChild(clone);
    /* saved page state, restored in destroy() */
    S.saved = { pos: body.style.position, top: body.style.top, bg: html.style.background, ov: html.style.overflow };
    if (getComputedStyle(body).position === "static") body.style.position = "relative";
    html.style.background = "#16100b";
    html.appendChild(root); html.appendChild(S.edge); html.appendChild(S.roller); /* outside <body> so they never move with the page */
    S.roller.style.display = S.edge.style.display = "none";
    arms(5, 5);
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
    if (S) {
      cancelAnimationFrame(S.raf);
      body.style.top = S.saved.top; body.style.position = S.saved.pos;
      html.style.background = S.saved.bg; html.style.overflow = S.saved.ov;
      S.root.remove(); S.roller.remove(); S.edge.remove();
    }
    S = null; busy = false; if (afterTour) cool = performance.now() + 1200;
  }

  /* Walk `dist` px to the right. Position and gait share one clock: legs swing with distance travelled and
     fade to rest as she slows, so she plants both feet exactly where she stops. */
  function walk(dist, ms) {
    return new Promise(function (res) {
      var rest = function () {
        S.teacher.style.transform = "";
        S.legA.style.transform = "rotate(-5deg)"; S.legB.style.transform = "rotate(5deg)"; S.bob.style.transform = ""; res();
      };
      if (reduce || dist <= 1) return rest();
      var k = S.svg.getBoundingClientRect().width / VBW, stride = 150 * k, t0 = performance.now();
      (function f(now) {
        var t = Math.min(1, (now - t0) / ms), e = t < .5 ? 2 * t * t : 1 - 2 * (1 - t) * (1 - t);
        S.teacher.style.transform = "translateX(" + (-dist * (1 - e)) + "px)";
        var env = Math.min(1, t * 6, (1 - t) * 6), ph = dist * e / stride * 2 * Math.PI, s = Math.sin(ph);
        S.legA.style.transform = "rotate(" + (-5 + s * 20 * env) + "deg)";
        S.legB.style.transform = "rotate(" + (5 - s * 20 * env) + "deg)";
        S.bob.style.transform = "translateY(" + (-Math.abs(Math.cos(ph)) * 2.2 * env) + "px)";
        t < 1 ? requestAnimationFrame(f) : rest();
      })(t0);
    });
  }

  async function play() {
    var root = S.root;
    S.playing = true; cancelAnimationFrame(S.raf); S.raf = 0; html.style.overflow = "hidden"; hint(false);
    document.addEventListener("keydown", S.key); addEventListener("resize", S.resize);

    /* 1. the rest of the page winds into the roll */
    await animateP(S.p, 1, 1000 * (1 - S.p) + 350);

    /* 2. classroom fades in; teacher waits at the far left with a hand up. The roll gathers into a point above her hand. */
    var nat = S.svg.getBoundingClientRect();
    var dist = Math.max(0, nat.left - 14);
    S.teacher.style.transform = "translateX(" + -dist + "px)";
    arms(-84, -16);
    root.classList.add("on");
    var tr = S.svg.getBoundingClientRect(), k = tr.width / VBW, hx = tr.left + 190 * k;
    S.roller.style.transformOrigin = hx + "px 0";
    anim(S.roller, [{ transform: "scaleX(1)", opacity: 1 }, { transform: "scaleX(.1)", opacity: 1, offset: .75 }, { transform: "scaleX(.04)", opacity: 0 }], 650, { easing: "cubic-bezier(.5,0,.8,.4)" });
    await wait(500);
    S.roller.style.display = "none"; S.edge.style.display = "none";

    /* 3. the roll drops from the top of the screen into her hand, with a small settle */
    var y0 = -(tr.top / k) - 80;
    S.roll.style.opacity = 1;
    await anim(S.roll, [
      { transform: "translate(190px," + y0 + "px) rotate(190deg)", easing: "cubic-bezier(.55,0,.9,.55)" },
      { transform: "translate(190px,126px) rotate(-6deg)", offset: .78, easing: "ease-out" },
      { transform: "translate(190px,138px) rotate(-6deg)", offset: .9, easing: "ease-in-out" },
      { transform: "translate(190px,132px) rotate(-6deg)" }], 1150, { easing: "linear" });
    await wait(150);

    /* 4. she lowers it to her side-front, carrying position */
    arms(-25, -55);
    await anim(S.roll, [{ transform: "translate(190px,132px) rotate(-6deg)" }, { transform: "translate(190px,190px) rotate(-12deg)" }], 800);
    await wait(250);

    /* 5. walk to the board, then stand still */
    await walk(dist, Math.min(4200, Math.max(1800, dist * 6)));
    await wait(350);

    /* 6. hang the roll on the board's top rail */
    var fr = root.querySelector(".st-frame").getBoundingClientRect(), tn = S.svg.getBoundingClientRect();
    k = tn.width / VBW;
    var bx = (fr.left + fr.width / 2 - tn.left) / k, by = (fr.top + 16 - tn.top) / k, sx = (fr.width + 20) / (136 * k);
    arms(aim(fr.left + fr.width / 2, fr.top + 16), -8); await wait(700);
    await anim(S.roll, [{ transform: "translate(190px,190px) rotate(-12deg) scale(1,1)" }, { transform: "translate(" + bx + "px," + by + "px) rotate(0deg) scale(" + sx + ",1)" }], 1000);
    S.rod.style.opacity = 1; S.pull.style.opacity = 1; S.roll.style.opacity = 0;
    await wait(350);
    arms(6, 6);                                      /* arm comes down; from here she never moves again */
    await wait(500);

    /* 7. unroll the sheet */
    var H = S.screen.clientHeight;
    anim(S.pull, [{ transform: "translateY(0)" }, { transform: "translateY(" + (H - 8) + "px)" }], 1300);
    await anim(S.screen, [{ clipPath: "inset(0 0 100% 0)" }, { clipPath: "inset(0 0 0% 0)" }], 1300);
    S.pull.style.opacity = 0;

    /* 8. teach */
    focus(0); S.ready = true; S.card.classList.add("show");
  }

  async function end() {
    if (!S || !S.ready) return;
    S.ready = false;
    document.removeEventListener("keydown", S.key); removeEventListener("resize", S.resize);
    S.card.classList.remove("show");
    S.ring.style.opacity = 0; arms(6, 6);
    await anim(S.screen, [{ clipPath: "inset(0 0 0% 0)" }, { clipPath: "inset(0 0 100% 0)" }], 700);
    S.rod.style.opacity = 0;
    S.roller.getAnimations().forEach(function (a) { a.cancel(); });
    S.roller.style.display = S.edge.style.display = "";
    setP(1);
    S.root.classList.remove("on"); await wait(450);
    await animateP(1, 0, 1000);                      /* page unwinds back down */
    destroy(true);
  }
  function start() {
    if (busy || !build()) return;
    S.playing = true;                                /* blocks wheel-pull while we position */
    var max = document.documentElement.scrollHeight - innerHeight;
    if (scrollY < max - 2) { scrollTo({ top: max, behavior: reduce ? "auto" : "smooth" }); setTimeout(play, reduce ? 0 : 800); }
    else play();
  }

  /* ---- scroll-driven trigger: keep scrolling past the bottom and the page winds up ---- */
  var LIM = 650, rel = 0, lastY = 0, hintEl = null;
  function atBottom() { return scrollY + innerHeight >= document.documentElement.scrollHeight - 2; }
  function hint(on) { if (hintEl) hintEl.classList.toggle("show", !!on); }
  function pull(dy) {
    if (S && S.playing) return false;
    if (!S) { if (reduce || performance.now() < cool || !atBottom() || dy <= 0 || busy) return false; if (!build()) return false; }
    hint(false);
    S.released = false;
    S.o = Math.max(0, Math.min(LIM, S.o + dy));
    S.target = S.o / LIM;
    drive();
    clearTimeout(rel);
    if (S.o < LIM) rel = setTimeout(function () {   /* let go early -> springs back */
      if (!S || S.playing) return;
      S.o = 0; S.target = 0; S.released = true; drive();
    }, 600);
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
