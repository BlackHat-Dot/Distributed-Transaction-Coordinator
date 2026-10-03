/* SCROLL TOUR (inline) — sits between <main> and your footer and plays as the visitor scrolls (scroll back = rewind).
   It overlaps the LAST SCREEN of the real page with an identical copy, so the hand-over is seamless:
   0.0-0.8  the last screen of your page lifts off and shrinks to the middle of the screen
   0.8-2.0  it winds up into a roll (the roll ends up in the centre)       2.0-2.8  the roll flies to the teacher's raised hand
   2.8-3.5  she catches it and lowers it       3.6-4.8  she walks to the board       5.0-5.5  hangs it
   5.5-6.2  it unrolls       6.2+  she stands still and teaches (0.9 per step), then the page continues to the footer.
   Presentation only: clones <main>, never touches app state. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ===== EDIT THESE: one step per dashboard section (selector is looked up inside <main>) ===== */
  var STEPS = [
    { sel: ".proof-panel", title: "1 · Live proof panel",
      text: "Fire real scenarios from here: happy path, payment decline with compensation, a 5x race attack and a crash simulation." },
    { sel: ".topology-section", title: "2 · Service topology",
      text: "Client, Coordinator, Inventory and Payment, each with its own database, plus Redis for the broker and the mutex." },
    { sel: ".recovery-panel", title: "3 · Crash recovery",
      text: "Celery Beat sweeps for orphaned sagas and rolls them forward or compensates them." },
    { sel: ".tx-feed-panel", title: "4 · Transaction matrix",
      text: "Every saga with its phase waterfall, status and amount. Click a row to open its audit trail." },
    { sel: ".forensic-panel", title: "5 · Forensic audit stream",
      text: "An append-only event log: every transition is recorded with its actor and payload." }
  ];
  /* Timeline in "units" of scroll (1 unit = UNIT x viewport height). Everything below is a pure function of scroll. */
  var UNIT = .45, INTRO = 6.2, PER = .6, N = STEPS.length;
  var OUTRO_START = INTRO + PER * N, OUTRO_DUR = 2.4, TOTAL = OUTRO_START + OUTRO_DUR;

  /* Side-profile teacher facing right. viewBox 270x400, shoulder (104,140), hip (102,240). */
  var TEACHER = '<svg viewBox="0 0 270 400" aria-hidden="true"><defs>' +
    '<linearGradient id="st-roll" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5a5040"/><stop offset=".5" stop-color="#2a251d"/><stop offset="1" stop-color="#110e0b"/></linearGradient><linearGradient id="st-par" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f4e3bd"/><stop offset=".55" stop-color="#d8bd8a"/><stop offset="1" stop-color="#a98a58"/></linearGradient></defs>' +
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
      '<g class="st-roll" style="opacity:0;transform:translate(190px,-900px)"><rect x="-62" y="-9" width="124" height="18" rx="9" fill="url(#st-roll)" stroke="#ff9f0a" stroke-opacity=".35"/>' +
        '<path d="M-56 -3H56M-56 3H56" stroke="#fff" stroke-opacity=".08" stroke-width="1"/>' +
        '<rect x="-68" y="-8" width="10" height="16" rx="4" fill="#d8bd8a"/><rect x="58" y="-8" width="10" height="16" rx="4" fill="#d8bd8a"/></g>' +
      '<g class="st-uA" style="transform-origin:104px 140px;transform:rotate(5deg)"><rect x="95" y="134" width="18" height="52" rx="9" fill="#e9e9e6"/>' +
        '<g class="st-fA" style="transform-origin:104px 180px;transform:rotate(5deg)"><rect x="96" y="174" width="16" height="48" rx="8" fill="#f4cfae"/><circle cx="104" cy="226" r="9" fill="#f4cfae"/></g></g>' +
    '</g></svg>';
  var VBW = 270, SHX = 104, SHY = 140;

  var S = null, sd = 0, st = 0, raf = 0, pose = "", idx = -2;
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
        '<div class="st-stage"><div class="st-row"><div class="st-teacher">' + TEACHER + '</div>' +
        '<div class="st-board"><div class="st-frame"><div class="st-chalk"></div><div class="st-screen"><div class="st-sheet"></div><div class="st-ring"></div></div><div class="st-rod"></div><div class="st-pull"></div></div></div></div></div>' +
        '<div class="st-cover"></div>' +
        '<div class="st-holder"><div class="st-shadow"></div><div class="st-leaf"><div class="st-paper-in"></div></div><div class="st-roller"><i class="st-cap l"></i><i class="st-cap r"></i></div></div>' +
        '<div class="st-card"><h4></h4><p></p><div class="st-foot"><div class="st-dots">' + STEPS.map(function () { return "<i></i>"; }).join("") + '</div>' +
        '<div class="st-btns"><button class="btn btn-outline btn-sm st-skip">Skip tour</button><button class="btn btn-outline btn-sm st-back">Back</button><button class="btn btn-primary btn-sm st-next">Next</button></div></div></div>' +
      '</div></div>';
    var q = function (s) { return host.querySelector(s); };
    S = { host: host, track: q(".st-track"), pin: q(".st-pin"), teacher: q(".st-teacher"), board: q(".st-board"), svg: q(".st-teacher svg"),
      uA: q(".st-uA"), fA: q(".st-fA"), legA: q(".st-legA"), legB: q(".st-legB"), bob: q(".st-bob"), roll: q(".st-roll"),
      screen: q(".st-screen"), sheet: q(".st-sheet"), ring: q(".st-ring"), rod: q(".st-rod"), pull: q(".st-pull"), frame: q(".st-frame"),
      cover: q(".st-cover"), holder: q(".st-holder"), shadow: q(".st-shadow"), leaf: q(".st-leaf"), paperIn: q(".st-paper-in"), roller: q(".st-roller"),
      card: q(".st-card"), title: q("h4"), text: q("p"), dots: [].slice.call(host.querySelectorAll(".st-dots i")),
      skip: q(".st-skip"), back: q(".st-back"), next: q(".st-next"), fresh: false };
    S.paperIn.appendChild(mkClone()); S.sheet.appendChild(mkClone());
    S.dots.forEach(function (dot, dotIdx) {
      dot.title = "Jump to step " + (dotIdx + 1);
      dot.onclick = function () { goto(INTRO + PER * dotIdx + .05); };
    });
    S.next.onclick = function () {
      if (idx < N - 1) {
        goto(INTRO + PER * (Math.max(idx, -1) + 1) + .05);
      } else if (idx === N - 1) {
        goto(OUTRO_START + .05);
      } else {
        window.location.href = "booking.html";
      }
    };
    S.back.onclick = function () {
      if (idx === N) {
        goto(INTRO + PER * (N - 1) + .05);
      } else {
        goto(INTRO + PER * (idx - 1) + .05);
      }
    };
    S.skip.onclick = function () {
      if (idx === N) {
        goto(0);
      } else {
        goto(OUTRO_START + .05);
      }
    };
    return true;
  }

  function goto(sv) {
    var top = S.track.getBoundingClientRect().top + scrollY + sv * UNIT * innerHeight;
    scrollTo({ top: top, behavior: reduce ? "auto" : "smooth" });
  }
  function refresh() { S.paperIn.replaceChildren(mkClone()); S.sheet.replaceChildren(mkClone()); layout(); }

  /* measure everything once per resize / content refresh */
  function layout() {
    var vh = innerHeight, vw = innerWidth;
    S.track.style.height = (TOTAL * UNIT + 1) * vh + "px";
    S.teacher.style.transform = ""; S.bob.style.transform = "";
    S.holder.style.width = vw + "px"; S.holder.style.height = vh + "px";
    /* paper = copy of the real <main>, same width and position, bottom-aligned to the last screen of the real page */
    var mr = document.querySelector("main").getBoundingClientRect();
    S.paperIn.style.left = mr.left + "px"; S.paperIn.style.width = mr.width + "px";
    S.paperIn.style.top = Math.min(0, vh - S.paperIn.offsetHeight) + "px";
    /* board is locked to landscape widescreen 16:9 */
    S.pin.style.setProperty("--st-ar", "1.778");
    var nat = S.svg.getBoundingClientRect(), pr = S.pin.getBoundingClientRect(), fr = S.frame.getBoundingClientRect();
    S.k = nat.width / VBW; S.dist = Math.max(0, nat.left - 14); S.topRel = nat.top - pr.top;
    S.bx = (fr.left + fr.width / 2 - nat.left) / S.k; S.by = (fr.top + 16 - nat.top) / S.k; S.sx = (fr.width + 20) / (136 * S.k);
    S.aimRail = aim(fr.left + fr.width / 2, fr.top + 16);

    /* Measure each step's element unscaled directly from S.sheet */
    var prevTransition = S.sheet.style.transition;
    S.sheet.style.transition = "none";
    S.sheet.style.transform = "none";
    var sr = S.sheet.getBoundingClientRect();
    S.geo = STEPS.map(function (stp) {
      var el = S.sheet.querySelector(stp.sel); if (!el) return null;
      var r = el.getBoundingClientRect();
      return { x: r.left - sr.left, y: r.top - sr.top, w: r.width, h: r.height };
    });
    void S.sheet.offsetHeight; // force reflow
    S.sheet.style.transition = prevTransition;

    var SW = S.screen.clientWidth;
    var defaultScale = SW / 1440;
    S.defaultScale = defaultScale;
    if (idx >= 0 && idx < N) {
      focus(idx);
    } else {
      S.sheet.style.transform = "translate(" + ((SW - 1440 * defaultScale) / 2).toFixed(1) + "px, 0px) scale(" + defaultScale.toFixed(3) + ")";
    }
    pose = ""; render(sd);
  }

  function focus(i) {
    if (i === N) {
      S.ring.style.opacity = 0;
      S.card.classList.add("completed");
      S.title.innerHTML = '<span class="st-badge-complete">✓ Tour Complete</span><br>All 5 Invariants Verified';
      S.text.innerHTML = "You've explored the full saga coordinator architecture &mdash; from distributed mutex locks and relational idempotency to deterministic crash recovery and tamper-proof forensic audit streams.";
      S.dots.forEach(function (d) { d.classList.add("on"); });
      S.back.disabled = false;
      S.skip.textContent = "Replay ↺";
      S.next.textContent = "Live Booking ➔";
      return;
    }
    S.card.classList.remove("completed");
    var g = S.geo[i]; if (!g) return;
    var SW = S.screen.clientWidth, SH = S.screen.clientHeight;

    /* Zoom in on section i so all numbers, texts, and diagrams are large, clear, and readable */
    var padX = 28, padY = 28;
    var availW = SW - padX * 2, availH = SH - padY * 2;
    var scale = Math.min(availW / g.w, availH / g.h);
    // Scale up to 0.96x fit so every section fills the landscape board with comfortable breathing room
    var targetScale = clamp(scale * 0.96, 0.52, 0.95);

    var cx = g.x + g.w / 2;
    var cy = g.y + g.h / 2;
    var tx = (SW / 2) - (cx * targetScale);
    var ty = (SH / 2) - (cy * targetScale);

    // Top-align step 0 (proof panel) so its header, stats, and scenarios start cleanly at the top
    if (i === 0) {
      ty = -(g.y * targetScale) + padY;
    }

    S.sheet.style.transform = "translate(" + tx.toFixed(1) + "px," + ty.toFixed(1) + "px) scale(" + targetScale.toFixed(3) + ")";

    var rx = g.x * targetScale + tx;
    var ry = g.y * targetScale + ty;
    var rw = g.w * targetScale;
    var rh = g.h * targetScale;

    var r = S.ring.style;
    r.opacity = 1;
    r.left = (rx - 6).toFixed(1) + "px";
    r.top = (ry - 6).toFixed(1) + "px";
    r.width = (rw + 12).toFixed(1) + "px";
    r.height = (rh + 12).toFixed(1) + "px";

    var sr = S.screen.getBoundingClientRect();
    arms(aim(sr.left + rx + rw / 2, sr.top + ry + rh / 2), -6);

    S.title.textContent = STEPS[i].title;
    S.text.textContent = STEPS[i].text;
    S.dots.forEach(function (d, n) { d.classList.toggle("on", n === i); });
    S.back.disabled = i === 0;
    S.skip.textContent = "Skip tour";
    S.next.textContent = i === N - 1 ? "Finish ➔" : "Next";
  }

  /* ---- the whole story as a pure function of scroll position s ---- */
  function render(s) {
    var H = innerHeight, W = innerWidth, k = S.k, dist = S.dist;

    /* 1) the last screen of the page lifts off and shrinks to the centre; 2) it winds up into a roll in the centre */
    var f0 = seg(s, 0, .15), p1 = ease(seg(s, .15, .9)), eR = ease(seg(s, .8, 2.0)), p3 = seg(s, 2.0, 2.8);
    var sc = 1 - .42 * p1, Tn = eR * .132 * W, Gh = H * (1 - eR) + Tn * eR, cy = (H - Gh * sc) / 2;
    S.holder.style.display = s >= 2.8 ? "none" : "";
    S.holder.style.transform = "translateY(" + cy + "px) scale(" + sc + ")";
    S.holder.style.opacity = (1 - seg(p3, 0, .3)) * f0;
    S.cover.style.display = p1 >= 1 ? "none" : ""; S.cover.style.opacity = (1 - p1) * f0;
    S.leaf.style.clipPath = "inset(0 0 " + eR * 100 + "% 0 round " + (p1 * 22 / sc) + "px)";
    S.paperIn.style.transform = "translateY(" + -eR * H + "px)";
    S.shadow.style.height = (1 - eR) * H + "px"; S.shadow.style.opacity = p1; S.shadow.style.borderRadius = (p1 * 22 / sc) + "px";
    S.roller.style.height = Tn + "px"; S.roller.style.display = Tn > 1 ? "" : "none";
    S.teacher.style.opacity = seg(s, .2, .8);
    S.board.style.opacity = seg(s, 1.9, 2.5);

    /* 3) the roll leaves the centre, flies to the raised hand (arc), is caught, lowered, carried, then hung on the board */
    var x = 190, y = 132, rot = -6, scl = 1;
    if (s < 2.8) {
      var e = ease(p3), x0 = (W / 2 - 14) / k, y0 = (H / 2 - S.topRel) / k, s0 = W * sc / (136 * k);
      x = x0 + (190 - x0) * e; y = y0 + (132 - y0) * e - Math.sin(Math.PI * p3) * 80; rot = -6 * e; scl = s0 + (1 - s0) * Math.pow(e, .8);
    } else if (s < 3.1) { y = 132 + 10 * Math.sin(Math.PI * seg(s, 2.8, 3.1)); }
    else if (s < 5.0) { var eD = ease(seg(s, 3.1, 3.5)); y = 132 + 58 * eD; rot = -6 - 6 * eD; }
    else {
      var q = ease(seg(s, 5.0, 5.5));
      x = 190 + (S.bx - 190) * q; y = 190 + (S.by - 190) * q; rot = -12 * (1 - q); scl = 1 + (S.sx - 1) * q;
    }
    S.roll.style.opacity = s < 2.0 ? 0 : s < 5.5 ? seg(s, 2.0, 2.2) : 0;
    S.roll.style.transform = "translate(" + x + "px," + y + "px) rotate(" + rot + "deg) scale(" + (s >= 5.0 ? scl + "," + 1 : scl + "," + scl) + ")";

    /* walk: position and gait share one clock so she plants her feet exactly at the end */
    var pE = seg(s, 3.6, 4.8), w = quad(pE), env = pE > 0 && pE < 1 ? Math.min(1, pE * 6, (1 - pE) * 6) : 0;
    var ph = dist * w / (150 * k) * 2 * Math.PI, sn = Math.sin(ph);
    S.teacher.style.transform = "translateX(" + -dist * (1 - w) + "px)";
    S.legA.style.transform = "rotate(" + (-5 + sn * 20 * env) + "deg)";
    S.legB.style.transform = "rotate(" + (5 - sn * 20 * env) + "deg)";
    S.bob.style.transform = "translateY(" + -Math.abs(Math.cos(ph)) * 2.2 * env + "px)";

    /* board: rod, unroll during intro, then roll up during outro */
    var pG = seg(s, 5.5, 6.2), eG = ease(pG);
    var pRollUp = seg(s, OUTRO_START + .2, OUTRO_START + 1.2), eRollUp = ease(pRollUp);
    var boardOpen = s < OUTRO_START ? eG : Math.max(0, 1 - eRollUp);

    S.rod.style.opacity = s >= 5.4 ? 1 : 0;
    S.pull.style.opacity = s >= 5.4 && boardOpen > 0.005 ? 1 : 0;
    S.pull.style.transform = "translateY(" + boardOpen * (S.screen.clientHeight - 8) + "px)";
    S.screen.style.clipPath = "inset(0 0 " + (1 - boardOpen) * 100 + "% 0)";

    /* arm poses & gestures */
    if (s < OUTRO_START) {
      S.teacher.classList.remove("waving");
      var want = s < 3.1 ? "up" : s < 4.8 ? "carry" : s < 5.5 ? "rail" : s < INTRO ? "rest" : "teach";
      if (want !== pose) {
        pose = want;
        if (want === "up") arms(-84, -16); else if (want === "carry") arms(-25, -55);
        else if (want === "rail") arms(S.aimRail, -8); else if (want === "rest") arms(6, 6);
      }
    } else {
      pose = "outro";
      var waving = s >= OUTRO_START + .15 && s < OUTRO_START + 1.8;
      S.teacher.classList.toggle("waving", waving);
      if (s < OUTRO_START + 1.8) {
        arms(-74, -20);
        var bow = Math.sin(seg(s, OUTRO_START + .2, OUTRO_START + 1.0) * Math.PI);
        S.bob.style.transform = "translateY(" + (bow * 4) + "px)";
      } else {
        arms(6, 6);
        S.bob.style.transform = "";
      }
    }

    var i = s < INTRO ? -1 : s < OUTRO_START ? Math.min(N - 1, Math.floor((s - INTRO) / PER)) : N;
    /* hide the card just before the dissolve starts so it doesn't linger */
    S.card.classList.toggle("show", s >= INTRO - .05 && s < OUTRO_START + 1.35);
    if (i !== idx) { idx = i; if (i >= 0) focus(i); else S.ring.style.opacity = 0; }

    /* ── smooth dissolve before handing off to the footer ───────────────── */
    /* Teacher finishes waving at OUTRO_START+1.8 (11.0); fade across ~0.7    */
    /* units so the pin is fully transparent before it un-sticks at TOTAL.    */
    S.pin.style.opacity = 1 - ease(seg(s, OUTRO_START + 1.4, TOTAL - 0.3));
  }

  /* scroll -> target; displayed value chases it a little each frame (the friction / inertia) */
  function onScroll() {
    if (!S) return;
    var r = S.track.getBoundingClientRect(), vh = innerHeight;
    /* JS pinning (works even when an ancestor has overflow set, where CSS sticky silently fails) */
    var mode = r.top > 0 ? "before" : r.bottom >= vh ? "fixed" : "after", ps = S.pin.style;
    if (mode !== S.mode) {
      S.mode = mode;
      ps.position = mode === "fixed" ? "fixed" : "absolute";
      ps.top = mode === "after" ? "" : "0"; ps.bottom = mode === "after" ? "0" : "";
      ps.visibility = mode === "before" ? "hidden" : "visible";
    }
    st = clamp(-r.top / (innerHeight * UNIT), 0, TOTAL);
    if (st === 0 && r.top < innerHeight * 1.5 && !S.fresh) { S.fresh = true; refresh(); }   /* fresh snapshot of live data just before it is needed */
    if (r.top > innerHeight * 2.5) S.fresh = false;
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function tick() {
    raf = 0;
    var d = st - sd;
    sd = reduce || Math.abs(d) < .0005 ? st : sd + d * .12;
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
    var qm = window.location.search.match(/tour_pos=([\d.]+)/);
    if (qm) {
      setTimeout(function () {
        var sv = parseFloat(qm[1]);
        var top = S.track.getBoundingClientRect().top + scrollY + sv * UNIT * innerHeight;
        window.scrollTo({ top: top, behavior: "instant" });
        sd = st = sv;
        render(sv);
      }, 150);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  window.startScrollTour = function () { if (S) goto(0); };
})();
