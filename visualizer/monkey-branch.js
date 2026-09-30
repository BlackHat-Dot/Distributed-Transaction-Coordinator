/**
 * DTC OBSERVABILITY CONSOLE — MONKEY BRANCH & COMPANION
 * Fully integrated interactive wooden branch, autonomous rigged monkey,
 * woodpecker recovery, non-stressful user opt-in, and center booking celebration.
 */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const NS = "http://www.w3.org/2000/svg";
  const E = (t, a, p) => {
    const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]);
    if (p) p.append(e);
    return e;
  };
  const RM = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Geometry configuration tuned for header health strip
  const BY = 36;       // Branch baseline Y
  const D = 20;        // Break drop distance
  const GAP = 16;      // Break gap width
  const PILL_TOP = 62; // Top offset where pills hang

  const SV = [
    { k: "coord", n: "Coordinator", p: ":8002", i: 0, st: "up", sag: 0, brk: 0, tsag: 0, tbrk: 0 },
    { k: "inv", n: "Inventory", p: ":8000", i: 1, st: "up", sag: 0, brk: 0, tsag: 0, tbrk: 0 },
    { k: "pay", n: "Payment", p: ":8001", i: 2, st: "up", sag: 0, brk: 0, tsag: 0, tbrk: 0 },
    { k: "rec", n: "Recovery Worker", p: "", i: 3, st: "up", sag: 0, brk: 0, tsag: 0, tbrk: 0 }
  ];

  let bk, fr, wrap, pillsEl, helpBtn, stripEl;
  let W = 760, H = 112, time = 0, tok = 0;
  let M = null;
  const MO = [];
  let wp = null;
  let hs = null;
  const ptr = { x: 0, y: 0, active: false };

  // =========================================================================
  // 1. User Preference & Opt-In State ("only if the user willing to see this")
  // =========================================================================
  window.isMonkeyEnabled = function () {
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has("monkey")) return urlParams.get("monkey") === "1" || urlParams.get("monkey") === "true";
    return localStorage.getItem("dtc_monkey_enabled") === "true";
  };

  window.setMonkeyEnabled = function (enabled) {
    localStorage.setItem("dtc_monkey_enabled", enabled ? "true" : "false");
    applyMonkeyPreference(enabled);

    // Close opt-in prompt card if open
    const optinCard = $("#mb-optin-card");
    if (optinCard) optinCard.classList.add("hidden");
  };

  window.toggleMonkeyCompanion = function () {
    window.setMonkeyEnabled(!window.isMonkeyEnabled());
  };

  function applyMonkeyPreference(enabled) {
    // 1. Update all toggle switches across pages
    const toggleInputs = document.querySelectorAll("#toggle-monkey-mode");
    toggleInputs.forEach((inp) => (inp.checked = enabled));

    const toggleLabels = document.querySelectorAll("#monkey-toggle-label");
    toggleLabels.forEach((lbl) => lbl.classList.toggle("active", enabled));

    const toggleIcons = document.querySelectorAll("#monkey-toggle-icon");
    toggleIcons.forEach((ico) => (ico.style.opacity = enabled ? "1" : "0.45"));

    // 2. Toggle top-right strip mode (Classic Clean vs Monkey Branch)
    if (stripEl) {
      if (enabled) {
        stripEl.classList.remove("classic-mode");
        setTimeout(() => {
          layout();
          SV.forEach(drawSeg);
          if (M) {
            M.pose = "cele";
            setTimeout(() => { if (M) M.pose = "idle"; }, 800);
          }
        }, 50);
      } else {
        stripEl.classList.add("classic-mode");
      }
    }
  }

  function initOptInCard() {
    const optinCard = $("#mb-optin-card");
    const btnYes = $("#btn-optin-yes");
    const btnNo = $("#btn-optin-no");
    const btnClose = $("#btn-optin-close");

    // Check if user has already made a choice
    const savedChoice = localStorage.getItem("dtc_monkey_enabled");

    if (savedChoice === null && optinCard) {
      // First visit: ask politely after a gentle 1.5s delay so it's completely stress-free
      setTimeout(() => {
        if (localStorage.getItem("dtc_monkey_enabled") === null) {
          optinCard.classList.remove("hidden");
        }
      }, 1500);
    }

    if (btnYes) {
      btnYes.addEventListener("click", () => {
        window.setMonkeyEnabled(true);
      });
    }

    if (btnNo) {
      btnNo.addEventListener("click", () => {
        window.setMonkeyEnabled(false);
      });
    }

    if (btnClose) {
      btnClose.addEventListener("click", () => {
        // Dismiss quietly
        if (optinCard) optinCard.classList.add("hidden");
        if (localStorage.getItem("dtc_monkey_enabled") === null) {
          localStorage.setItem("dtc_monkey_enabled", "false");
          applyMonkeyPreference(false);
        }
      });
    }

    // Bind change on toggle switches
    const toggleInputs = document.querySelectorAll("#toggle-monkey-mode");
    toggleInputs.forEach((inp) => {
      inp.addEventListener("change", (e) => {
        window.setMonkeyEnabled(e.target.checked);
      });
    });
  }

  // =========================================================================
  // 2. Center Booking Celebration Overlay ("shows them monkey in the middle")
  // =========================================================================
  let celebTimerId = null;
  let celebAnimId = null;

  window.triggerBookingCelebration = function (details = {}) {
    // Only show if the user is willing to see the monkey
    if (!window.isMonkeyEnabled()) return;

    const overlay = $("#mb-center-celebration");
    if (!overlay) return;

    const titleEl = $("#mb-celeb-title");
    const descEl = $("#mb-celeb-desc");
    const timerEl = $("#mb-celeb-timer");
    const closeBtn = $("#btn-close-celebration");
    const optoutBtn = $("#btn-optout-monkey");
    const svgEl = $("#mb-celeb-svg");

    if (titleEl) titleEl.textContent = details.title || "Seat Secured!";
    if (descEl) descEl.textContent = details.seat ? `${details.seat} • 2PC Saga Completed` : "Transaction confirmed with ACID guarantee";

    overlay.classList.remove("hidden");

    // Draw cute celebration monkey in SVG
    if (svgEl) {
      drawCelebrationMonkey(svgEl);
    }

    // Feasible duration: 2.8 seconds
    const totalMs = 2800;
    const startT = performance.now();

    if (celebTimerId) clearInterval(celebTimerId);
    if (celebAnimId) cancelAnimationFrame(celebAnimId);

    celebTimerId = setInterval(() => {
      const elapsed = performance.now() - startT;
      const rem = Math.max(0, Math.ceil((totalMs - elapsed) / 1000));
      if (timerEl) timerEl.textContent = `Auto-closing in ${rem}s`;
      if (elapsed >= totalMs) {
        dismissCelebration();
      }
    }, 250);

    function dismissCelebration() {
      if (celebTimerId) { clearInterval(celebTimerId); celebTimerId = null; }
      if (celebAnimId) { cancelAnimationFrame(celebAnimId); celebAnimId = null; }
      overlay.classList.add("hidden");
    }

    if (closeBtn) {
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        dismissCelebration();
      };
    }

    if (optoutBtn) {
      optoutBtn.onclick = (e) => {
        e.stopPropagation();
        dismissCelebration();
        window.setMonkeyEnabled(false);
      };
    }
  };

  function drawCelebrationMonkey(svg) {
    svg.innerHTML = "";
    const c = "#7a4a2a", f = "#f0cf9b";
    const g = E("g", { transform: "translate(45, 52)" }, svg);

    // Confetti particles
    const confettiColors = ["#ffd84a", "#6fd26a", "#38bdf8", "#b58cff", "#ff5d4d"];
    const confetti = [];
    for (let i = 0; i < 14; i++) {
      const ang = (i / 14) * Math.PI * 2;
      const rad = 24 + Math.random() * 16;
      const dot = E("circle", {
        cx: Math.cos(ang) * rad,
        cy: Math.sin(ang) * rad - 10,
        r: 1.8 + Math.random() * 1.4,
        fill: confettiColors[i % confettiColors.length]
      }, g);
      confetti.push({ dot, ang, rad, speed: 0.8 + Math.random() * 1.2 });
    }

    // Limbs and body
    const tail = E("path", { d: "M-6 -6 Q-18 -18 -8 -26", fill: "none", stroke: c, "stroke-width": 3, "stroke-linecap": "round" }, g);
    const leftLeg = E("line", { x1: -5, y1: 0, x2: -10, y2: 12, stroke: c, "stroke-width": 3.6, "stroke-linecap": "round" }, g);
    const rightLeg = E("line", { x1: 5, y1: 0, x2: 10, y2: 12, stroke: c, "stroke-width": 3.6, "stroke-linecap": "round" }, g);

    // Body
    E("ellipse", { cx: 0, cy: -8, rx: 9, ry: 11, fill: c }, g);
    E("ellipse", { cx: 0, cy: -7, rx: 5.5, ry: 7, fill: f }, g);

    // Arms waving upward in joy
    const leftArm = E("path", { d: "M-6 -14 Q-16 -22 -14 -30", fill: "none", stroke: c, "stroke-width": 3.5, "stroke-linecap": "round" }, g);
    const rightArm = E("path", { d: "M6 -14 Q16 -22 14 -30", fill: "none", stroke: c, "stroke-width": 3.5, "stroke-linecap": "round" }, g);

    // Golden Ticket in hand
    const ticket = E("g", { transform: "translate(14, -34) rotate(15)" }, g);
    E("rect", { x: -4, y: -6, width: 14, height: 9, rx: 1.5, fill: "#ffd84a", stroke: "#b8901a", "stroke-width": 0.8 }, ticket);
    E("line", { x1: -1, y1: -3, x2: 7, y2: -3, stroke: "#9e7710", "stroke-width": 0.7 }, ticket);

    // Head
    const head = E("g", { transform: "translate(0, -22)" }, g);
    E("circle", { r: 9, fill: c }, head);
    [-8, 8].forEach((x) => {
      E("circle", { cx: x, cy: -1, r: 3, fill: c }, head);
      E("circle", { cx: x, cy: -1, r: 1.5, fill: f }, head);
    });
    E("ellipse", { cy: 2.5, rx: 6, ry: 5, fill: f }, head);
    [-2.5, 2.5].forEach((x) => E("circle", { cx: x, cy: 0, r: 1.4, fill: "#1a0f08" }, head));
    E("path", { d: "M-2.5 4.5 Q0 7 2.5 4.5", fill: "none", stroke: "#5a2f14", "stroke-width": 1 }, head);

    // Party hat
    E("polygon", { points: "0,-18 -5,-9 5,-9", fill: "#ff5d4d" }, head);
    E("circle", { cx: 0, cy: -18, r: 1.6, fill: "#ffd84a" }, head);

    let t0 = performance.now();
    function animateCeleb(now) {
      const dt = (now - t0) / 1000;
      const bob = Math.abs(Math.sin(dt * 9)) * 8;
      g.setAttribute("transform", `translate(45, ${54 - bob})`);
      tail.setAttribute("d", `M-6 -6 Q-18 -18 -8 ${-24 + Math.sin(dt * 12) * 5}`);

      confetti.forEach((item, idx) => {
        const pulse = Math.sin(dt * 6 + idx) * 3;
        item.dot.setAttribute("cx", Math.cos(item.ang) * (item.rad + pulse));
        item.dot.setAttribute("cy", Math.sin(item.ang) * (item.rad + pulse) - 10);
      });

      celebAnimId = requestAnimationFrame(animateCeleb);
    }
    celebAnimId = requestAnimationFrame(animateCeleb);
  }

  // =========================================================================
  // 3. Stage & Branch Elements Initialization
  // =========================================================================
  function initElements() {
    stripEl = $("#monkey-branch-strip");
    bk = $("#mb-back");
    fr = $("#mb-front");
    wrap = $("#mb-wrap");
    pillsEl = $("#mb-pills");
    helpBtn = $("#mb-help");

    if (!bk || !fr || !wrap || !pillsEl) return false;

    // Associate existing DOM pill nodes
    SV[0].el = document.getElementById("node-health-coord");
    SV[1].el = document.getElementById("node-health-inv");
    SV[2].el = document.getElementById("node-health-pay");
    SV[3].el = document.getElementById("node-health-celery");

    SV[0].lat = document.getElementById("lat-coord");
    SV[1].lat = document.getElementById("lat-inv");
    SV[2].lat = document.getElementById("lat-pay");
    SV[3].lat = document.getElementById("sweep-countdown");

    return true;
  }

  function initBranchDefs() {
    const defs = E("defs", {}, bk);
    defs.innerHTML = `
      <linearGradient id="mb-wg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#a3683a"/>
        <stop offset=".45" stop-color="#7a4a2a"/>
        <stop offset="1" stop-color="#4b2b17"/>
      </linearGradient>
    `;

    SV.forEach((s) => {
      s.g = E("g", {}, bk);
      s.rope = E("path", { fill: "none", stroke: "#b89a6a", "stroke-width": 1.6, "stroke-linecap": "round" }, s.g);
      s.pc = [0, 1].map(() => ({
        b: E("path", { stroke: "url(#mb-wg)", "stroke-width": 18, fill: "none", "stroke-linecap": "round" }, s.g),
        t: E("path", { stroke: "#2b160a", "stroke-width": 18, fill: "none", "stroke-dasharray": "2 8", opacity: 0.3 }, s.g),
        h: E("path", { stroke: "#e0a86a", "stroke-width": 2.2, fill: "none", "stroke-linecap": "round", opacity: 0.35, transform: "translate(0 -4)" }, s.g)
      }));
      s.lv = [0.18, 0.48, 0.76].map((t) => {
        const g = E("g", {}, s.g);
        E("path", { d: "M0 0Q-7-8-2-18Q5-11 0 0Z", fill: "#5fbf55", transform: "rotate(-28)" }, g);
        E("path", { d: "M0 0Q-7-8-2-16Q5-11 0 0Z", fill: "#4aa84a", transform: "rotate(26)" }, g);
        return { g, t };
      });
    });
  }

  function layout() {
    if (!wrap) return;
    W = wrap.clientWidth || 760;
    H = wrap.clientHeight || 112;
    [bk, fr].forEach((v) => v && v.setAttribute("viewBox", `0 0 ${W} ${H}`));

    const wrapRect = wrap.getBoundingClientRect();
    const r = SV.map((s) => {
      if (!s.el) return { l: 0, r: 0 };
      const pr = s.el.getBoundingClientRect();
      return {
        l: pr.left - wrapRect.left,
        r: pr.right - wrapRect.left
      };
    });

    SV.forEach((s, i) => {
      s.xm = (r[i].l + r[i].r) / 2 || (W * (i + 0.5)) / 4;
      s.x0 = i ? (r[i - 1].r + r[i].l) / 2 : 0;
      s.x1 = i < 3 ? (r[i].r + r[i + 1].l) / 2 : W;
    });
  }

  const piece = (s, side) => {
    const g = GAP * s.brk;
    const dr = D * s.brk;
    const sg = 18 * s.sag - 3 * (1 - s.sag) * (1 - s.brk);
    return side
      ? { xa: s.xm + g, ya: BY + dr, xb: s.x1, yb: BY, cy: BY + dr / 2 + sg }
      : { xa: s.x0, ya: BY, xb: s.xm - g, yb: BY + dr, cy: BY + dr / 2 + sg };
  };

  const qy = (p, x) => {
    const denom = p.xb - p.xa || 1;
    const t = Math.max(0, Math.min(1, (x - p.xa) / denom));
    return (1 - t) * (1 - t) * p.ya + 2 * (1 - t) * t * p.cy + t * t * p.yb;
  };

  const surf = (x) => {
    const s = SV.find((s) => x >= s.x0 && x <= s.x1);
    if (!s) return null;
    if (s.brk > 0.5 && Math.abs(x - s.xm) < GAP * s.brk + 2) return null;
    return qy(piece(s, x > s.xm ? 1 : 0), x);
  };

  function drawSeg(s) {
    [0, 1].forEach((sd) => {
      const p = piece(s, sd);
      const d = `M${p.xa} ${p.ya}Q${(p.xa + p.xb) / 2} ${p.cy} ${p.xb} ${p.yb}`;
      const o = s.pc[sd];
      o.b.setAttribute("d", d);
      o.t.setAttribute("d", d);
      o.h.setAttribute("d", d);
    });

    s.lv.forEach((l, j) => {
      const x = s.x0 + (s.x1 - s.x0) * l.t;
      const sy = surf(x) ?? BY;
      const sway = RM ? 0 : Math.sin(time * (s.sag > 0.5 ? 7 : 1.6) + j * 2) * (s.sag > 0.5 ? 6 : 3.5);
      l.g.setAttribute("transform", `translate(${x} ${sy - 8}) rotate(${sway})`);
      l.g.style.opacity = Math.max(0, 1 - s.brk * 1.6);
    });

    const ry = BY + D * s.brk + 10 * s.sag + 6;
    const pt = PILL_TOP;
    s.rope.setAttribute("d", `M${s.xm - 22} ${pt}L${s.xm} ${ry}L${s.xm + 22} ${pt}`);
    s.rope.style.opacity = Math.max(0, 1 - s.brk * 2.8);
  }

  // =========================================================================
  // 4. Rigged Monkey Creation & Kinematics
  // =========================================================================
  function mk(sc = 0.62) {
    const c = "#7a4a2a", f = "#f0cf9b";
    const root = E("g", {}, fr);
    const inn = E("g", {}, root);
    const m = {
      root, inn, sc,
      x: 0, y: 0, tx: null,
      pose: "idle", mode: "stand",
      speed: 95, dir: 1, rot: 0, spin: 0, amp: 0, vx: 0,
      say: "", sayT: 0, banana: false, q: false, ground: false,
      hangK: 0, swingPhase: 0, hx: 0, followHang: false
    };

    const st = { stroke: c, "stroke-linecap": "round", fill: "none" };
    m.tail = E("g", {}, inn);
    E("path", { ...st, d: "M0 0Q-15 3-12-12Q-10-20-4-17", "stroke-width": 3.4 }, m.tail);

    const limb = (len, paw) => {
      const g = E("g", {}, inn);
      E("line", { x1: 0, y1: 0, x2: 0, y2: len, ...st, "stroke-width": 4.2 }, g);
      E("circle", { cx: 0, cy: len, r: 2.8, fill: paw ? f : c }, g);
      return g;
    };

    m.ll = limb(12);
    m.rl = limb(12);
    E("ellipse", { cx: 0, cy: -20, rx: 8.5, ry: 11, fill: c }, inn);
    E("ellipse", { cx: 1, cy: -18, rx: 5.2, ry: 7, fill: f }, inn);

    m.la = limb(21, 1);
    m.ra = limb(21, 1);
    m.ban = E("path", { d: "M-2 0Q3-8 10-6Q6 0-2 0Z", fill: "#ffd84a", stroke: "#b8901a", "stroke-width": 0.9, transform: "translate(0 21)" }, m.ra);

    m.hd = E("g", {}, inn);
    E("circle", { r: 8.8, fill: c }, m.hd);
    [-8.5, 8.5].forEach((x) => {
      E("circle", { cx: x, cy: -1, r: 3.2, fill: c }, m.hd);
      E("circle", { cx: x, cy: -1, r: 1.6, fill: f }, m.hd);
    });
    E("ellipse", { cy: 2.6, rx: 6, ry: 5.2, fill: f }, m.hd);
    [-2.6, 2.6].forEach((x) => E("circle", { cx: x, cy: 0, r: 1.5, fill: "#1a0f08" }, m.hd));
    E("path", { d: "M-2.5 4.5Q0 7 2.5 4.5", ...st, stroke: "#5a2f14", "stroke-width": 1 }, m.hd);

    m.band = E("rect", { x: -8.5, y: -8, width: 17, height: 3.5, rx: 1.8, display: "none" }, m.hd);
    m.sw = E("path", { d: "M0 0Q-3.5 5 0 7Q3.5 5 0 0Z", fill: "#5cc8ff", transform: "translate(11 -42)" }, inn);
    m.bubG = E("g", { class: "mb-speech-bubble", display: "none" }, fr);
    m.bubTail = E("path", { fill: "#141c18", stroke: "#24362e", "stroke-width": 1.2 }, m.bubG);
    m.bubRect = E("rect", { rx: 5, ry: 5, fill: "#141c18", stroke: "#24362e", "stroke-width": 1.2 }, m.bubG);
    m.bub = E("text", {
      "text-anchor": "middle",
      "dominant-baseline": "central",
      "font-size": 10.5,
      "font-weight": 800,
      fill: "#ffffff",
      "font-family": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      "letter-spacing": "0.02em"
    }, m.bubG);

    m.speak = (t, ms = 1400) => {
      m.say = t;
      m.sayT = time + Math.max(ms, 1200) / 1000;
    };
    MO.push(m);
    return m;
  }

  const T = (x, y, r) => `translate(${x} ${y}) rotate(${r})`;

  function lims(m) {
    if (m.ground) return [16, W - 16];
    let lo = SV[0].x0 + 8, hi = SV[3].x1 - 8;
    for (const s of SV.slice(0, 3)) {
      if (s.brk > 0.5) {
        const w = GAP * s.brk + 6;
        if (m.x < s.xm) hi = Math.min(hi, s.xm - w);
        else lo = Math.max(lo, s.xm + w);
      }
    }
    return [lo, hi];
  }

  function poseOf(m, t) {
    const o = { la: 6, ra: 6, ll: 0, rl: 0, tl: Math.sin(t * 2) * 10, bob: Math.sin(t * 2.4) * 0.5, hd: 0 };
    const sl = m.slow ? 0.5 : 1;
    const sw = Math.sin(m.swingPhase || 0);

    switch (m.pose) {
      case "walk": {
        const p = t * 9 * sl * (m.speed / 95);
        o.ll = Math.sin(p) * 32;
        o.rl = -o.ll;
        o.la = -o.ll * 0.8;
        o.ra = -o.rl * 0.8;
        o.bob = -Math.abs(Math.sin(p)) * 2;
        o.tl = Math.sin(p) * 16;
        break;
      }
      case "sit":
        o.ll = o.rl = -75;
        o.bob = -6;
        o.la = o.ra = -24;
        break;
      case "scratch":
        o.ra = 146 + Math.sin(t * 20) * 14;
        o.hd = Math.sin(t * 20) * 4;
        break;
      case "wave":
        o.ra = -145 + Math.sin(t * 10) * 22;
        break;
      case "eat":
        o.ra = 155 + Math.sin(t * 9) * 6;
        o.hd = Math.sin(t * 9) * 3;
        break;
      case "hang": {
        const brach = Math.abs(m.vx || 0) > 8 ? Math.sin((m.swingPhase || 0) * 1.8) * 18 : 0;
        o.la = 180 + brach - (m.rot || 0) * 0.25;
        o.ra = 180 - brach + (m.rot || 0) * 0.25;
        o.ll = sw * 14 - (m.rot || 0) * 0.45;
        o.rl = -sw * 14 + (m.rot || 0) * 0.45;
        o.tl = -(m.rot || 0) * 0.7 + Math.sin(t * 3) * 8;
        break;
      }
      case "hangOne": {
        o.ra = 180;
        o.la = 140 + Math.sin(t * 5) * 10 - (m.rot || 0) * 0.35;
        o.ll = sw * 12 - (m.rot || 0) * 0.4;
        o.rl = -sw * 12 + (m.rot || 0) * 0.4;
        o.hd = Math.sin(t * 4) * 5;
        o.tl = -(m.rot || 0) * 0.6;
        break;
      }
      case "confuse":
        o.ra = 146 + Math.sin(t * 20) * 12;
        o.hd = Math.sin(t * 6) * 9;
        break;
      case "cele":
        o.bob = Math.abs(Math.sin(t * 9)) * 10;
        o.la = 143 + Math.sin(t * 12) * 15;
        o.ra = -143 - Math.sin(t * 12) * 15;
        break;
      case "reach": {
        o.ra = 180;
        o.la = m.ra_ || (140 + Math.sin(t * 6) * 10);
        o.ll = sw * 10 - (m.rot || 0) * 0.35;
        o.rl = -sw * 10 + (m.rot || 0) * 0.35;
        o.tl = -(m.rot || 0) * 0.5;
        break;
      }
      case "no":
        o.hd = Math.sin(t * 18) * 14;
        break;
      case "shock":
        o.la = 130;
        o.ra = -130;
        o.hd = Math.sin(t * 40) * 4;
        o.bob = Math.sin(t * 30);
        break;
      case "bonk":
        o.la = o.ra = 150;
        o.bob = Math.abs(Math.sin(t * 14)) * 7;
        break;
    }
    return o;
  }

  function upd(m, dt) {
    if (m.pend && m.pend.my !== tok) {
      m.pend.rej("x");
      m.pend = null;
      m.tx = null;
    }
    m.slow = SV.some((s) => m.x >= s.x0 && m.x <= s.x1 && s.st === "slow") && m.mode === "stand" && !m.ground;

    if (m === M && m.follow && m.mode === "stand" && ptr.active) m.tx = ptr.x;

    if (m.mode === "stand" && m.tx != null) {
      const l = lims(m);
      const tx = Math.max(l[0], Math.min(l[1], m.tx));
      const d = tx - m.x;
      if (Math.abs(d) < 2) {
        m.x = tx;
        const ok = Math.abs(m.tx - tx) < 3;
        m.tx = null;
        m.pose = "idle";
        m.pend && m.pend.res(ok);
        m.pend = null;
      } else {
        m.dir = d > 0 ? 1 : -1;
        const step = Math.sign(d) * Math.min(Math.abs(d), m.speed * (m.slow ? 0.45 : 1) * dt);
        m.x += step;
        m.pose = "walk";
      }
    }

    if (m.mode === "hang" && m.followHang && ptr.active) {
      const l = lims({ x: m.hx, ground: false });
      const targetX = Math.min(Math.max(ptr.x, l[0]), l[1]);
      const diff = targetX - m.hx;
      const moveStep = Math.sign(diff) * Math.min(Math.abs(diff), 150 * dt);
      m.hx += moveStep;
      m.vx = moveStep / dt;
      if (Math.abs(diff) > 3) {
        m.dir = diff > 0 ? 1 : -1;
        m.pose = "hang";
      } else {
        m.vx *= 0.85;
        m.pose = "reach";
      }
    }

    if (m.pose === "bonk") {
      m.x += m.vx * dt;
      m.vx *= 0.95;
      m.spin += 540 * dt * Math.min(1, Math.abs(m.vx) / 60);
    }

    const targetHangK = m.mode === "hang" ? 1 : 0;
    m.hangK += (targetHangK - m.hangK) * Math.min(1, dt * 8);

    const sc = m.sc;
    let px, py, rot = 0, hoff = 0;

    if (m.mode === "hang" || m.hangK > 0.01) {
      const pv = m.pv ? m.pv() : [m.x, (surf(m.x) ?? BY) + 9];
      px = pv[0];
      m.x = px;

      m.swingPhase = (m.swingPhase || 0) + dt * 2.5;
      const targetAmp = RM ? 0 : m.pose === "hang" ? 22 : m.pose === "hangOne" ? 16 : m.pose === "reach" ? 12 : 9;
      m.amp += (targetAmp - m.amp) * Math.min(1, dt * 3);

      const inertiaAngle = -Math.min(20, Math.max(-20, (m.vx || 0) * 0.16));
      rot = (Math.sin(m.swingPhase) * m.amp + inertiaAngle) * m.hangK;

      if (m.pose === "reach") {
        m.dir = ptr.x > px ? 1 : -1;
        m.ra_ = Math.atan2(-(ptr.x - px) * m.dir, ptr.y - pv[1] - 20 * sc) * 57.3;
      }

      hoff = 48 * sc * m.hangK;
      const standY = (surf(px) ?? BY) - 8;
      const hangAnchorY = pv[1];
      py = standY + (hangAnchorY - standY) * m.hangK;
      m.y = py + hoff;
    } else {
      if (m.mode === "stand") {
        const sy = surf(m.x);
        if (m.ground) {
          m.y += (H - 4 - m.y) * Math.min(1, dt * 4);
        } else if (sy != null) {
          m.y += (sy - 8 - m.y) * Math.min(1, dt * 14);
          rot = Math.atan2((surf(m.x + 6) ?? sy) - (surf(m.x - 6) ?? sy), 12) * 57.3;
        } else {
          const s = SV.find((s) => m.x >= s.x0 && m.x <= s.x1);
          if (s) m.x += (m.x >= s.xm ? 1 : -1) * 80 * dt;
        }
      }
      px = m.x;
      py = m.y;
    }

    const o = poseOf(m, time);
    if (m.mode !== "hang") py -= o.bob;
    m.rot = rot;

    m.root.setAttribute("transform", T(px, py, rot));
    m.inn.setAttribute("transform", `translate(0 ${hoff}) translate(0 -22) rotate(${m.spin}) translate(0 22) scale(${m.dir * sc} ${sc})`);
    m.la.setAttribute("transform", T(-6, -26, o.la));
    m.ra.setAttribute("transform", T(6, -26, o.ra));
    m.ll.setAttribute("transform", T(-4, -10, o.ll));
    m.rl.setAttribute("transform", T(4, -10, o.rl));
    m.tail.setAttribute("transform", T(-6, -13, o.tl));
    m.hd.setAttribute("transform", T(0, -36, o.hd));
    m.ban.setAttribute("display", m.banana ? "" : "none");
    m.sw.setAttribute("display", m.slow || m.pose === "shock" || m.sad ? "" : "none");
    const say = time < m.sayT ? m.say : m.q ? "?" : "";
    if (!say) {
      m.bubG.setAttribute("display", "none");
    } else {
      m.bubG.removeAttribute("display");
      m.bub.textContent = say;

      // Tone-specific color palettes
      let bg = "rgba(18, 28, 23, 0.96)";
      let border = "#3dd68c";
      let textCol = "#ffffff";
      let glow = "drop-shadow(0 2px 6px rgba(0,0,0,0.5))";

      if (say === "CRASH!" || say === "Declined!" || say === "409") {
        bg = "rgba(44, 14, 14, 0.96)";
        border = "#ff5d4d";
        textCol = "#ffb3b3";
        glow = "drop-shadow(0 2px 8px rgba(255, 93, 77, 0.45))";
      } else if (say === "WIN (200)" || say === "Refunded") {
        bg = "rgba(14, 38, 20, 0.96)";
        border = "#52e27c";
        textCol = "#a3ffbc";
        glow = "drop-shadow(0 2px 8px rgba(82, 226, 124, 0.45))";
      } else if (say === "Hold!" || say === "?") {
        bg = "rgba(36, 30, 10, 0.96)";
        border = "#ffd84a";
        textCol = "#ffeb99";
        glow = "drop-shadow(0 2px 8px rgba(255, 216, 74, 0.4))";
      }

      m.bubRect.setAttribute("fill", bg);
      m.bubRect.setAttribute("stroke", border);
      m.bubTail.setAttribute("fill", bg);
      m.bubTail.setAttribute("stroke", border);
      m.bub.setAttribute("fill", textCol);
      m.bubG.style.filter = glow;

      const bubW = Math.max(22, say.length * 7.2 + 16);
      const bubH = 20;
      const headY = py - (m.mode === "hang" ? 16 : 28) * sc;

      let bubX, bubY, tailPath;

      if (headY >= 28) {
        // Plenty of headroom above monkey: place bubble centered above head
        bubX = Math.max(bubW / 2 + 6, Math.min(W - bubW / 2 - 6, px));
        bubY = Math.max(bubH / 2 + 4, headY - bubH / 2 - 6);
        const tailTipX = px;
        const tailTipY = headY - 1;
        const tailBaseY = bubY + bubH / 2;
        const tailBaseX = Math.max(bubX - bubW / 2 + 6, Math.min(bubX + bubW / 2 - 6, px));
        tailPath = `M${tailBaseX - 4} ${tailBaseY} L${tailTipX} ${tailTipY} L${tailBaseX + 4} ${tailBaseY} Z`;
      } else {
        // Ceiling clearance is tight (standing on branch): place bubble to the side of head
        const preferRight = px + 14 + bubW + 6 <= W || px - 14 - bubW - 6 < 0;
        if (preferRight) {
          bubX = px + 14 + bubW / 2;
          bubY = Math.max(bubH / 2 + 4, Math.min(H - bubH / 2 - 4, headY + 6));
          bubX = Math.min(W - bubW / 2 - 6, bubX);
          tailPath = `M${bubX - bubW / 2 + 0.5} ${bubY - 3} L${px + 7 * sc} ${headY + 3} L${bubX - bubW / 2 + 0.5} ${bubY + 4} Z`;
        } else {
          bubX = px - 14 - bubW / 2;
          bubY = Math.max(bubH / 2 + 4, Math.min(H - bubH / 2 - 4, headY + 6));
          bubX = Math.max(bubW / 2 + 6, bubX);
          tailPath = `M${bubX + bubW / 2 - 0.5} ${bubY - 3} L${px - 7 * sc} ${headY + 3} L${bubX + bubW / 2 - 0.5} ${bubY + 4} Z`;
        }
      }

      // Hard clamp guarantees bubble is NEVER clipped regardless of window/stage size
      bubX = Math.max(bubW / 2 + 4, Math.min(W - bubW / 2 - 4, bubX));
      bubY = Math.max(bubH / 2 + 4, Math.min(H - bubH / 2 - 4, bubY));

      m.bubRect.setAttribute("x", bubX - bubW / 2);
      m.bubRect.setAttribute("y", bubY - bubH / 2);
      m.bubRect.setAttribute("width", bubW);
      m.bubRect.setAttribute("height", bubH);

      m.bubTail.setAttribute("d", tailPath);

      m.bub.setAttribute("x", bubX);
      m.bub.setAttribute("y", bubY);
    }
  }

  // =========================================================================
  // 5. Director & Scripting
  // =========================================================================
  const sleep = (ms, my) => new Promise((r, j) => setTimeout(() => (my === tok ? r() : j("x")), ms));
  const tween = (ms, fn, my) =>
    new Promise((res, rej) => {
      const t0 = performance.now();
      const st = (n) => {
        if (my !== tok) return rej("x");
        const k = Math.min(1, (n - t0) / ms);
        fn(k);
        k < 1 ? requestAnimationFrame(st) : res();
      };
      requestAnimationFrame(st);
    });

  async function play(fn) {
    if (!window.isMonkeyEnabled()) return;
    const my = ++tok;
    if (wp) wp.mode = "perch";
    hs = null;
    M.follow = false;
    M.followHang = false;
    try {
      await fn(my);
    } catch (e) {
      if (e !== "x") console.error("Monkey director error:", e);
    }
  }

  const go = (fn) =>
    play(async (my) => {
      await fn(my);
      if (my !== tok) return;
      await idle(my);
    });

  const goStand = (m, x, my) => {
    m.mode = "stand";
    m.followHang = false;
    return new Promise((res, rej) => {
      m.tx = x;
      m.pend = { res, rej, my };
    });
  };

  const pose = async (m, p, ms, my) => {
    m.pose = p;
    await sleep(ms, my);
    m.pose = "idle";
  };

  async function hang(m, pv, ms, p, my, land) {
    m.pv = pv;
    m.mode = "hang";
    m.pose = p;
    m.amp = 0;
    m.swingPhase = 0;
    await sleep(ms, my);
    if (land) {
      m.mode = "stand";
      m.x = land;
      m.pose = "idle";
    }
  }

  async function hop(m, tx, my) {
    const x0 = m.x, y0 = m.y, p0 = m.pose;
    await tween(
      360,
      (k) => {
        m.x = x0 + (tx - x0) * k;
        m.y = y0 - Math.sin(k * Math.PI) * 18;
        m.pose = "cele";
      },
      my
    );
    m.pose = p0;
  }

  async function idle(my) {
    while (my === tok) {
      if (!window.isMonkeyEnabled()) {
        await sleep(1000, my);
        continue;
      }
      if (M.ground) {
        M.pose = "sit";
        M.sad = true;
        await sleep(1500, my);
        continue;
      }
      M.sad = false;
      const b = SV.slice(0, 3).find((s) => s.brk > 0.5);
      if (b && Math.abs(M.x - b.xm) < 55) {
        M.dir = b.xm > M.x ? 1 : -1;
        M.q = true;
        await pose(M, "confuse", 1400, my);
        M.q = false;
        await hang(M, () => [b.xm - M.dir * 18, BY + 6], 1400, "hangOne", my, b.xm - M.dir * 26);
        continue;
      }

      const acts = ["walk", "scratch", "banana", "hang", "wave", "sit"];
      const a = acts[Math.floor(Math.random() * acts.length)];
      if (a === "walk") {
        const l = lims(M);
        const tx = l[0] + Math.random() * (l[1] - l[0]);
        await goStand(M, tx, my);
        await sleep(600 + Math.random() * 800, my);
      } else if (a === "scratch") {
        await pose(M, "scratch", 900, my);
      } else if (a === "banana") {
        M.banana = true;
        await pose(M, "eat", 1200, my);
        M.banana = false;
      } else if (a === "hang") {
        const l = lims(M);
        const pvX = Math.max(l[0] + 16, Math.min(l[1] - 16, M.x));
        await hang(M, () => [pvX, (surf(pvX) ?? BY) + 8], 1500, "hang", my, pvX);
      } else if (a === "wave") {
        await pose(M, "wave", 900, my);
      } else {
        await pose(M, "sit", 1200, my);
      }
      await sleep(1000 + Math.random() * 1200, my);
    }
  }

  // =========================================================================
  // 6. Scenario Orchestrations
  // =========================================================================
  const happy = () =>
    go(async (my) => {
      M.ground = false;
      await goStand(M, SV[0].xm, my);
      await pose(M, "cele", 450, my);
      await hang(M, () => [SV[0].xm + 18, BY + 6], 550, "hang", my, SV[1].x0 + 10);
      await goStand(M, SV[1].xm, my);
      await pose(M, "wave", 400, my);
      await hang(M, () => [SV[1].xm + 18, BY + 6], 550, "hang", my, SV[2].x0 + 10);
      await goStand(M, SV[2].xm, my);
      await pose(M, "cele", 800, my);
    });

  const decline = () =>
    go(async (my) => {
      M.ground = false;
      await goStand(M, SV[0].xm, my);
      M.banana = true;
      M.speak("Hold!", 700);
      await sleep(700, my);
      await goStand(M, SV[1].xm, my);
      await goStand(M, SV[2].xm, my);
      M.banana = false;
      M.speak("Declined!", 900);
      await pose(M, "shock", 900, my);
      M.banana = true;
      M.dir = -1;
      await goStand(M, SV[1].xm, my);
      await goStand(M, SV[0].xm, my);
      M.banana = false;
      M.speak("Refunded", 900);
      await pose(M, "wave", 800, my);
    });

  const race = () =>
    go(async (my) => {
      const monos = [M, ...[1, 2, 3, 4].map(() => mk(0.45))];
      monos.forEach((m, idx) => {
        m.ground = false;
        m.x = SV[0].x0 + 12 + idx * 8;
        m.speed = 100 + Math.random() * 30;
      });
      const raceOver = monos.map(
        (m, idx) =>
          new Promise(async (resolve) => {
            await sleep(idx * 80, my);
            const targetX = idx === 0 ? SV[2].xm : SV[1].xm + (idx - 2) * 12;
            const ok = await goStand(m, targetX, my).catch(() => false);
            if (idx === 0 && ok) {
              m.speak("WIN (200)", 1000);
              await pose(m, "cele", 1100, my);
            } else {
              m.speak("409", 700);
              m.pose = "bonk";
              m.vx = -(120 + Math.random() * 60);
              await sleep(800, my);
            }
            resolve();
          })
      );
      await Promise.all(raceOver);
      monos.slice(1).forEach((m) => {
        m.root.remove();
        if (m.bubG) m.bubG.remove();
        MO.splice(MO.indexOf(m), 1);
      });
    });

  const crash = () =>
    go(async (my) => {
      M.ground = false;
      await goStand(M, SV[1].xm - 20, my);
      setSt(1, "down");
      await tween(320, () => {}, my);
      M.speak("CRASH!", 800);
      await hang(M, () => [SV[1].xm - GAP - 4, BY + D + 4], 900, "hangOne", my);
      M.q = true;
      await pose(M, "confuse", 1200, my);
      M.q = false;
      await tween(
        450,
        (k) => {
          M.y = BY + D + (H - 4 - (BY + D)) * k;
          M.rot = k * 180;
        },
        my
      );
      M.mode = "stand";
      M.ground = true;
      M.rot = 0;
      M.sad = true;
      await pose(M, "sit", 1200, my);
    });

  const recover = () =>
    go(async (my) => {
      M.ground = false;
      M.sad = false;
      M.mode = "stand";
      for (const s of SV) {
        if (s.st !== "up") {
          await wpFlyTo(s.xm, my);
          await wpPeck(1000, my);
          setSt(s.i, "up");
        }
      }
      await wpFlyHome(my);
      await hop(M, SV[0].xm, my);
      await pose(M, "cele", 900, my);
    });

  // =========================================================================
  // 7. Woodpecker
  // =========================================================================
  function mkWoodpecker() {
    const g = E("g", {}, fr);
    const body = E("g", {}, g);
    E("polygon", { points: "0,0 -8,12 8,12", fill: "#2b2b2b" }, body);
    E("ellipse", { cx: 0, cy: -6, rx: 5.5, ry: 7.5, fill: "#2b2b2b" }, body);
    E("ellipse", { cx: 0, cy: -5, rx: 3.2, ry: 5, fill: "#f0f0f0" }, body);
    E("polygon", { points: "0,-14 -4,-6 4,-6", fill: "#e03020" }, body);
    E("polygon", { points: "0,-8 11,-6 0,-4", fill: "#e0a830" }, body);
    E("circle", { cx: 2, cy: -8, r: 1.2, fill: "#fff" }, body);
    E("circle", { cx: 2.4, cy: -8, r: 0.6, fill: "#000" }, body);
    const wing = E("polygon", { points: "-3,-4 -10,3 -3,6", fill: "#1a1a1a" }, body);
    const w = {
      g, body, wing,
      x: W - 20, y: BY - 8,
      mode: "perch", flap: 0,
      peckCount: 0
    };
    return w;
  }

  function updWp(dt) {
    if (!wp) return;
    if (wp.mode === "perch") {
      wp.x = W - 20;
      wp.y = BY - 8;
      wp.body.setAttribute("transform", "scale(-1 1)");
      wp.wing.setAttribute("transform", "scale(1 1)");
    } else if (wp.mode === "fly") {
      wp.flap += dt * 26;
      const f = Math.sin(wp.flap);
      wp.wing.setAttribute("transform", `scale(1 ${f})`);
      wp.body.setAttribute("transform", `scale(${wp.dir} 1) rotate(${wp.rot || 0})`);
    } else if (wp.mode === "peck") {
      wp.peckCount += dt * 24;
      const p = Math.sin(wp.peckCount) * 8;
      wp.body.setAttribute("transform", `scale(${wp.dir} 1) rotate(${p})`);
    }
    wp.g.setAttribute("transform", `translate(${wp.x} ${wp.y})`);
  }

  async function wpFlyTo(tx, my) {
    wp.mode = "fly";
    const x0 = wp.x, y0 = wp.y, ty = BY - 10;
    wp.dir = tx > x0 ? 1 : -1;
    await tween(
      650,
      (k) => {
        wp.x = x0 + (tx - x0) * k;
        wp.y = y0 + (ty - y0) * k - Math.sin(k * Math.PI) * 22;
        wp.rot = (tx > x0 ? 1 : -1) * (1 - k) * -12;
      },
      my
    );
  }

  async function wpPeck(ms, my) {
    wp.mode = "peck";
    wp.peckCount = 0;
    for (let i = 0; i < 6; i++) {
      spawnWoodChip(wp.x + (wp.dir === 1 ? 8 : -8), BY + 4);
    }
    await sleep(ms, my);
  }

  async function wpFlyHome(my) {
    wp.mode = "fly";
    const x0 = wp.x, y0 = wp.y, tx = W - 20, ty = BY - 8;
    wp.dir = tx > x0 ? 1 : -1;
    await tween(
      600,
      (k) => {
        wp.x = x0 + (tx - x0) * k;
        wp.y = y0 + (ty - y0) * k - Math.sin(k * Math.PI) * 18;
      },
      my
    );
    wp.mode = "perch";
  }

  const chips = [];
  function spawnWoodChip(x, y) {
    const c = E("circle", { cx: x, cy: y, r: 1.4, fill: "#e0a86a" }, fr);
    chips.push({
      el: c, x, y,
      vx: (Math.random() - 0.5) * 60,
      vy: -30 - Math.random() * 40,
      life: 0.55
    });
  }

  function updChips(dt) {
    for (let i = chips.length - 1; i >= 0; i--) {
      const c = chips[i];
      c.life -= dt;
      if (c.life <= 0) {
        c.el.remove();
        chips.splice(i, 1);
        continue;
      }
      c.x += c.vx * dt;
      c.y += c.vy * dt;
      c.vy += 180 * dt;
      c.el.setAttribute("cx", c.x);
      c.el.setAttribute("cy", c.y);
      c.el.style.opacity = c.life / 0.55;
    }
  }

  // =========================================================================
  // 8. Service Health State Updates
  // =========================================================================
  function setSt(i, st) {
    const s = SV[i];
    if (!s) return;
    s.st = st;
    s.tsag = st === "slow" ? 1 : 0;
    s.tbrk = st === "down" ? 1 : 0;

    if (s.el) {
      s.el.classList.toggle("slow", st === "slow");
      s.el.classList.toggle("down", st === "down");
    }

    if (st === "down" && M && Math.abs(M.x - s.xm) < GAP * 1.5 + 10 && !M.ground) {
      M.mode = "hang";
      M.pv = () => [s.xm - GAP - 4, BY + D + 4];
      M.pose = "hangOne";
      M.q = true;
    }
  }

  // =========================================================================
  // 9. Pointer Guidance
  // =========================================================================
  function initPointer() {
    if (!wrap) return;

    wrap.addEventListener("pointerenter", (e) => {
      if (!window.isMonkeyEnabled()) return;
      ptr.active = true;
      if (!hs && !M.ground) {
        const branchLine = (surf(e.clientX - wrap.getBoundingClientRect().left) ?? BY) + 8;
        const rect = wrap.getBoundingClientRect();
        const cursorY = e.clientY - rect.top;
        if (cursorY > branchLine + 12) {
          M.mode = "hang";
          M.followHang = true;
          M.hx = Math.max(SV[0].x0 + 12, Math.min(SV[3].x1 - 12, e.clientX - rect.left));
          M.pv = () => [M.hx, (surf(M.hx) ?? BY) + 9];
        } else {
          M.follow = true;
        }
      }
    });

    wrap.addEventListener("pointerleave", () => {
      ptr.active = false;
      if (M && M.mode === "hang" && M.followHang) {
        M.followHang = false;
        M.mode = "stand";
        M.pose = "idle";
      }
      if (M) M.follow = false;
    });

    wrap.addEventListener("pointermove", (e) => {
      if (!window.isMonkeyEnabled()) return;
      const rect = wrap.getBoundingClientRect();
      ptr.x = e.clientX - rect.left;
      ptr.y = e.clientY - rect.top;

      if (!hs && M && !M.ground) {
        const branchLine = (surf(ptr.x) ?? BY) + 8;
        if (M.mode === "stand" && ptr.y > branchLine + 18) {
          M.mode = "hang";
          M.followHang = true;
          M.follow = false;
          M.hx = ptr.x;
          M.pv = () => [M.hx, (surf(M.hx) ?? BY) + 9];
        } else if (M.mode === "hang" && ptr.y < branchLine - 4) {
          M.mode = "stand";
          M.followHang = false;
          M.follow = true;
          M.pose = "walk";
        }
      }
    });

    wrap.addEventListener("click", () => {
      if (M && M.ground && helpBtn) {
        helpBtn.hidden = false;
        helpBtn.style.left = `${Math.min(W - 100, Math.max(20, M.x - 35))}px`;
        helpBtn.style.top = `${H - 32}px`;
      }
    });

    if (helpBtn) {
      helpBtn.onclick = (e) => {
        e.stopPropagation();
        helpBtn.hidden = true;
        recover();
      };
    }
  }

  // =========================================================================
  // 10. Animation Loop
  // =========================================================================
  let last = performance.now();
  function loop(now) {
    const dt = Math.min(0.06, (now - last) / 1000);
    last = now;
    time += dt;

    if (window.isMonkeyEnabled() && stripEl && !stripEl.classList.contains("classic-mode")) {
      SV.forEach((s) => {
        s.sag += (s.tsag - s.sag) * Math.min(1, dt * 6);
        s.brk += (s.tbrk - s.brk) * Math.min(1, dt * 8);
        drawSeg(s);
      });

      MO.forEach((m) => upd(m, dt));
      updWp(dt);
      updChips(dt);
    }

    requestAnimationFrame(loop);
  }

  // =========================================================================
  // 11. Telemetry Hooks
  // =========================================================================
  function hookConsoleEvents() {
    const observeNode = (nodeKey, idx) => {
      const elNode = SV[idx].el;
      const elLat = SV[idx].lat;
      if (!elNode) return;

      const checkHealthState = () => {
        const isOffline = elNode.classList.contains("offline");
        const latText = elLat ? elLat.textContent.trim() : "";
        const latMatch = latText.match(/(\d+)\s*ms/);
        const latVal = latMatch ? parseInt(latMatch[1], 10) : 0;

        if (isOffline || latText === "CRASHED" || latText === "OFFLINE") {
          setSt(idx, "down");
        } else if (latVal > 800) {
          setSt(idx, "slow");
        } else {
          setSt(idx, "up");
        }
      };

      const mo = new MutationObserver(checkHealthState);
      mo.observe(elNode, { attributes: true, attributeFilter: ["class"] });
      if (elLat) mo.observe(elLat, { childList: true, characterData: true, subtree: true });
      checkHealthState();
    };

    observeNode("coord", 0);
    observeNode("inv", 1);
    observeNode("pay", 2);

    const btnHappy = document.getElementById("btn-demo-happy");
    const btnFail = document.getElementById("btn-demo-fail");
    const btnRace = document.getElementById("btn-demo-race");
    const btnCrash = document.getElementById("btn-demo-crash");
    const btnReset = document.getElementById("btn-reset-seats");

    if (btnHappy) btnHappy.addEventListener("click", () => happy());
    if (btnFail) btnFail.addEventListener("click", () => decline());
    if (btnRace) btnRace.addEventListener("click", () => race());
    if (btnCrash) btnCrash.addEventListener("click", () => crash());
    if (btnReset) {
      btnReset.addEventListener("click", () => {
        SV.forEach((_, i) => setSt(i, "up"));
        if (M) {
          M.ground = false;
          M.sad = false;
          M.mode = "stand";
          M.pose = "cele";
          setTimeout(() => { if (M) M.pose = "idle"; }, 800);
        }
      });
    }

    window.triggerMonkeyRecovery = recover;
  }

  // =========================================================================
  // 12. Global Initialization
  // =========================================================================
  function init() {
    // 1. Initialize Opt-in and Toggle Controls everywhere (index & booking)
    initOptInCard();

    const isEnabled = window.isMonkeyEnabled();

    // 2. Initialize Stage & Branch if on index.html
    const hasStage = initElements();
    if (hasStage) {
      initBranchDefs();
      layout();

      M = mk(0.62);
      M.x = SV[0].xm;
      M.y = BY - 8;

      wp = mkWoodpecker();

      initPointer();
      hookConsoleEvents();

      window.addEventListener("resize", () => {
        layout();
        SV.forEach(drawSeg);
      });

      if (window.ResizeObserver && wrap) {
        new ResizeObserver(() => {
          layout();
          SV.forEach(drawSeg);
        }).observe(wrap);
      }

      requestAnimationFrame((t) => {
        last = t;
        loop(t);
        idle(tok);
      });
    }

    // 3. Apply preference state
    applyMonkeyPreference(isEnabled);

    if (new URLSearchParams(window.location.search).has("test_celeb")) {
      window.triggerBookingCelebration({ seat: "Seat #34 (Row C)", saga: "2PC Atomic Saga" });
    }

    if (new URLSearchParams(window.location.search).has("test_speech")) {
      const speechText = new URLSearchParams(window.location.search).get("test_speech") || "CRASH!";
      setTimeout(() => {
        if (M) M.speak(speechText, 80000);
      }, 400);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
