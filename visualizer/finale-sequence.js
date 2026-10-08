/* FINALE SEQUENCE v1.0 — ONCE. -> DTC.  Presentation only; reads/writes no application state.
   One time-driven timeline (seconds from lock). Everything is a pure function of t except the chase,
   which is simulated once up-front (pure pursuit with inertia) so the catch is physically earned and deterministic.

     0.0  hold on ONCE.            4.6  C turns back toward E        (E trembles from 3.9, bolts at 5.0)
     0.5  C fills in, mouth opens  5.35 pursuit begins               catch -> swallow -> 1.0s stillness
     1.5  C turns left             ...  C settles right, counter and slot return, D and T rise -> DTC
     2.2  wind-up, lunge: N (2.5)  The dot is shoved out of frame by E's bolt.
     3.6  lunge: O                                                                                      */
(function () {
  "use strict";
  var fx = document.getElementById("fx-finale");
  if (!fx) return;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var GL = {"O":[[334,292,289,299,263,308,240,320,220,333,211,340,189,362,166,395,158,411,152,427,147,446,143,471,142,508,147,544,154,568,161,585,172,605,184,622,195,635,214,653,227,663,245,674,264,683,287,691,324,698,367,698,383,696,405,691,428,683,449,673,468,661,478,653,505,625,526,594,534,577,543,550,549,512,548,469,542,435,535,415,523,390,511,371,503,361,480,339,466,328,446,315,436,310,411,301,369,292],[334,401,359,401,369,403,383,408,399,418,414,434,424,452,429,469,432,487,432,500,427,529,424,538,413,558,403,569,390,579,371,587,356,590,337,590,318,586,304,580,288,568,278,557,267,537,264,529,260,509,260,487,263,466,269,448,276,435,293,417,302,411,318,404]],"N":[[625,301,621,307,619,314,619,330,620,331,620,430,619,431,619,455,620,456,620,491,619,492,619,504,620,505,620,627,619,628,619,638,620,639,620,658,619,659,619,673,621,681,624,686,628,690,639,694,658,694,659,693,714,694,724,692,733,686,736,680,737,674,737,663,736,662,737,519,739,514,744,512,754,521,770,548,788,573,798,591,811,608,827,634,842,654,856,678,864,687,872,692,880,694,896,694,897,693,955,694,963,693,973,688,977,682,979,674,979,664,978,663,978,587,979,586,978,329,979,328,979,312,974,302,962,296,886,295,875,297,869,300,864,305,860,320,861,366,862,367,862,391,861,392,862,467,859,476,857,478,854,478,847,472,834,450,795,393,742,311,733,301,726,297,718,295,636,296]],"E":[[1522,299,1514,306,1512,312,1512,324,1513,325,1512,671,1513,680,1516,686,1522,691,1534,694,1551,694,1552,693,1572,693,1573,694,1734,693,1735,694,1752,693,1759,690,1763,686,1766,677,1768,637,1766,627,1765,606,1758,597,1751,594,1644,595,1633,589,1629,578,1629,555,1631,548,1637,542,1643,540,1664,540,1665,541,1735,540,1742,537,1747,532,1750,526,1752,513,1751,508,1751,461,1749,453,1746,448,1743,446,1733,443,1717,443,1716,442,1642,443,1639,442,1632,436,1630,430,1629,408,1632,402,1636,398,1642,395,1749,396,1759,392,1764,386,1766,379,1766,360,1768,347,1766,333,1766,314,1764,304,1761,300,1750,296,1582,297,1581,296,1537,295]],"P":[[1798,636,1791,639,1782,648,1778,658,1778,668,1781,676,1790,686,1799,690,1812,690,1824,684,1830,677,1833,670,1833,656,1828,646,1820,639,1808,635]]};                                   /* traced contours, flattened [x,y,...] in design space */
  var PI = Math.PI, TAU = PI * 2;

  /* design space = the "cover" frame the mask is drawn into (1919 wide, aspect 1890:1080), measured from the live site */
  var FW = 1919, VASP = 1890 / 1080;
  var CY = 495.3, R = 202.5, RING = 92, CC = 8, SLOT = 45, KMAX = 0.67, RHO = 7, HOME = 1250.5;
  var TOP = 292.8, BOT = 697, FIN = { C: 1365.5, D: 351, T: 767 };
  var LET = { O: { c: [346, CY], l: 142, r: 550, m: 0 }, N: { c: [800, CY], l: 619, r: 980, m: 0 },
              E: { c: [1640, CY] }, P: { c: [1806, 662.8] } };

  var cl = function (x) { return x < 0 ? 0 : x > 1 ? 1 : x; };
  var lerp = function (a, b, t) { return a + (b - a) * t; };
  var sm = function (x) { return x * x * (3 - 2 * x); };
  var io = function (p) { return p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; };
  var oc = function (p) { return 1 - Math.pow(1 - p, 3); };
  var wrap = function (x) { x = (x + PI) % TAU; if (x < 0) x += TAU; return x - PI; };
  function kf(t, a) {
    if (t <= a[0][0]) return a[0][1];
    for (var i = 1; i < a.length; i++) if (t < a[i][0]) return lerp(a[i - 1][1], a[i][1], sm((t - a[i - 1][0]) / (a[i][0] - a[i - 1][0])));
    return a[a.length - 1][1];
  }

  /* ---------- timeline constants ---------- */
  var TB = 5.0, TCH = 5.35, DUR = 1.7, S = [], IC = 0, TC = 0, TR = 0, END = 0;
  var OPK = [[0, 1], [2.9, 1], [3.2, .12], [3.45, .12], [3.75, .8], [4.25, .8], [4.55, .1], [5.1, .1], [5.5, .7]];  /* mouth opening: bites on N and O */
  var WP = [[1650, 495], [1752, 450], [1808, 340], [1715, 262], [1470, 275], [1245, 350], [1115, 455]];             /* E's escape: out, up into the corner, back along the top, cut down */
  function cr(a, b, c, d, t) { return .5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t * t + (-a + 3 * b - 3 * c + d) * t * t * t); }
  function Epos(tau) {
    var n = WP.length, u = sm(cl(tau / DUR)) * (n - 1), i = Math.min(n - 2, Math.floor(u)), f = u - i;
    var P = function (j) { return WP[Math.max(0, Math.min(n - 1, j))]; }, a = P(i - 1), b = P(i), c = P(i + 1), d = P(i + 2);
    return [cr(a[0], b[0], c[0], d[0], f), cr(a[1], b[1], c[1], d[1], f)];
  }
  function simulate() {                                  /* C pursues E with inertia: accelerates from rest, steers with a lead, heading follows velocity */
    var DT = 1 / 120, x = 346, y = CY, vx = 0, vy = 0, f = 0;
    S = [];
    for (var i = 0; i < 700; i++) {
      var tt = i * DT, tau = TCH + tt - TB, e = Epos(tau), e2 = Epos(tau + .03);
      S.push([x, y, f, vx, vy]);
      if (Math.hypot(e[0] - x, e[1] - y) < .62 * R && tau > 1.15) break;
      var dx = e[0] + (e2[0] - e[0]) / .03 * .15 - x, dy = e[1] + (e2[1] - e[1]) / .03 * .15 - y, dl = Math.hypot(dx, dy) || 1;
      var sp = Math.min(1800, 3400 * tt), k = 1 - Math.exp(-7 * DT);
      vx += (dx / dl * sp - vx) * k; vy += (dy / dl * sp - vy) * k; x += vx * DT; y += vy * DT;
      if (Math.hypot(vx, vy) > 40) f += wrap(Math.atan2(vy, vx) - f) * (1 - Math.exp(-8 * DT));
    }
    IC = S.length - 1; TC = TCH + IC / 120; TR = TC + 1.0; END = TR + 2.0;
  }
  function sim(tt) {
    var q = tt * 120, i = Math.min(IC, Math.max(0, Math.floor(q))), j = Math.min(IC, i + 1), f = q - i, a = S[i], b = S[j];
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f), lerp(a[3], b[3], f), lerp(a[4], b[4], f)];
  }

  /* ---------- state of the C and of E at time t ---------- */
  function cBase(t) {                                    /* position / heading from the catch onward (momentum carries it on) */
    var q = S[IC], u = t - TC, d = (1 - Math.exp(-6 * Math.min(u, 1.5))) / 6;
    return { x: q[0] + q[3] * d, y: q[1] + q[4] * d, th: q[2] };
  }
  function cState(t) {
    var s = { x: HOME, y: CY, th: PI * io(cl((t - 1.5) / .7)), sc: 1, ring: 1 - io(cl((t - .5) / 1)), m: io(cl((t - .7) / .9)), op: kf(t, OPK) };
    if (t < TCH) {
      s.th += PI * io(cl((t - 4.6) / .7));
      if (t < 2.5) s.x = HOME + 26 * oc(cl((t - 2.2) / .3));
      else if (t < 3.6) s.x = lerp(HOME + 26, 800, io(cl((t - 2.5) / .8)));
      else s.x = lerp(800, 346, io(cl((t - 3.6) / .9)));
    } else if (t < TC) {
      var q = sim(t - TCH), e = Epos(t - TB), dist = Math.hypot(e[0] - q[0], e[1] - q[1]);
      s.x = q[0]; s.y = q[1]; s.th = q[2]; s.ring = 0; s.m = 1;
      s.op = lerp(s.op, 1, sm(cl(1 - dist / (4 * R))));
    } else {
      var u = t - TC, b = cBase(Math.min(t, TR));
      s.ring = 0; s.m = 1; s.x = b.x; s.y = b.y; s.th = b.th;
      s.op = u < .28 ? lerp(kf(TC, OPK) > 0 ? .95 : 1, .05, io(u / .28)) : .05;
      s.sc = 1 + .045 * Math.sin(PI * cl(u / .55));
      if (t >= TR) {
        var p = io(cl((t - TR) / 1.6)), r = io(cl((t - TR - .5) / 1));
        s.x = lerp(b.x, FIN.C, p); s.y = lerp(b.y, CY, p);
        s.th = b.th + wrap(-b.th) * io(cl((t - TR) / 1.2));
        s.ring = r; s.m = 1 - r; s.sc = 1;
      }
    }
    var pl = 0;                                          /* mass absorbed makes the body swell a little while it is taken in */
    ["N", "O"].forEach(function (k) { pl += Math.sin(PI * LET[k].m); });
    s.sc += .028 * pl;
    return s;
  }
  function eState(t, c) {
    if (t < TB) {
      var lean = sm(cl((t - 3.9) / .8)), tr = lean * 1.4;
      return { x: 1640 + 10 * lean + tr * Math.sin(t * 45), y: CY + tr * Math.sin(t * 37), s: 1, r: .045 * lean, a: 1 };
    }
    var tau = t - TB, tt = Math.min(tau, DUR), p = Epos(tt), p2 = Epos(tt + .03);
    var s0 = 1 - .2 * sm(cl(tau / .6)), vx = (p2[0] - p[0]) / .03;
    var r0 = Math.max(-1, Math.min(1, vx / 1800)) * .16 + .045 * (1 - sm(cl(tau / .4)));
    if (t < TC) return { x: p[0], y: p[1], s: s0, r: r0, a: 1 };
    var u = t - TC, e = sm(cl(u / .42)), mx = c.x + R * .55 * Math.cos(c.th), my = c.y + R * .55 * Math.sin(c.th);
    var q = Epos(TC - TB);                               /* taken into the mouth: drawn toward it, shrinking, then gone under the body */
    return { x: lerp(q[0], mx, e), y: lerp(q[1], my, e), s: s0 * (1 - .95 * e), r: r0, a: 1 - sm(cl((u - .3) / .12)) };
  }

  /* ---------- drawing ---------- */
  var cv = document.createElement("canvas"), ctx = cv.getContext("2d");
  cv.className = "fx-seq"; cv.setAttribute("aria-hidden", "true");
  fx.insertBefore(cv, fx.querySelector(".fx-stage"));
  var W = 0, H = 0, dpr = 1, sc = 1, ox = 0, oy = 0, bg, ink, cur = 0;

  function size() {
    var r = fx.getBoundingClientRect(); W = Math.max(10, Math.round(r.width)); H = Math.max(10, Math.round(r.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2); cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    var covW = W / H > VASP ? W : H * VASP, covH = covW / VASP;
    sc = covW / FW; ox = (W - covW) / 2; oy = (H - covH) / 2;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    bg = ctx.createRadialGradient(W * .5, H * .46, 0, W * .5, H * .46, Math.max(W, H) * .78);   /* same ground as the WebGL base */
    bg.addColorStop(0, "#261f17"); bg.addColorStop(.5, "#16120e"); bg.addColorStop(1, "#0a0807");
    ctx.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * ox, dpr * oy);
    ink = ctx.createLinearGradient(0, (H * .25 - oy) / sc, 0, (H * .78 - oy) / sc);              /* same ivory as the WebGL letters */
    ink.addColorStop(0, "#fbf3e3"); ink.addColorStop(1, "#cfc3ab");
    draw(cur);
  }
  function poly(a) {
    var n = a.length / 2, X = function (i) { return a[(i % n) * 2]; }, Y = function (i) { return a[(i % n) * 2 + 1]; };
    ctx.moveTo((X(n - 1) + X(0)) / 2, (Y(n - 1) + Y(0)) / 2);
    for (var i = 0; i < n; i++) ctx.quadraticCurveTo(X(i), Y(i), (X(i) + X(i + 1)) / 2, (Y(i) + Y(i + 1)) / 2);
    ctx.closePath();
  }
  function glyph(k, tx, ty, sx, sy, rot, al) {
    if (al <= .003) return;
    var c = LET[k].c; ctx.save(); ctx.globalAlpha = al;
    ctx.translate(c[0] + tx, c[1] + ty); ctx.rotate(rot); ctx.scale(sx, sy); ctx.translate(-c[0], -c[1]);
    ctx.beginPath(); GL[k].forEach(poly); ctx.fill("evenodd"); ctx.restore();
  }
  function rr(x, y, w, h, r) {
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h); ctx.arcTo(x, y + h, x, y + h - r, r); ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r); ctx.closePath();
  }
  function drawD(x) {
    var t = TOP, b = BOT, m = (t + b) / 2, rc = x + 380 - R, ct = t + 111, cb = b - 111, cr_ = (cb - ct) / 2;
    ctx.beginPath(); ctx.moveTo(x + 22, t); ctx.lineTo(rc, t); ctx.arc(rc, m, R, -PI / 2, PI / 2); ctx.lineTo(x + 22, b);
    ctx.arcTo(x, b, x, b - 22, 22); ctx.lineTo(x, t + 22); ctx.arcTo(x, t, x + 22, t, 22); ctx.closePath();
    ctx.moveTo(rc, ct); ctx.lineTo(x + 144, ct); ctx.arcTo(x + 132, ct, x + 132, ct + 12, 12); ctx.lineTo(x + 132, cb - 12);
    ctx.arcTo(x + 132, cb, x + 144, cb, 12); ctx.lineTo(rc, cb); ctx.arc(rc, m, cr_, PI / 2, -PI / 2, true); ctx.closePath(); ctx.fill("evenodd");
  }
  function drawT(x) { ctx.beginPath(); rr(x, TOP, 360, 112, 22); rr(x + 119, TOP, 122, BOT - TOP, 22); ctx.fill(); }

  /* The C / mouth form. One parametric outline: disc, counter (ring 1 -> 0), mouth edges y = h0 + k·x
     (slot of the real C: h0 = 45, k = 0  ->  open wedge: h0 = 0, k = .67). Built inset by RHO then stroked with round joins,
     so every tip is softened exactly like the glyph's own corners. */
  function drawC(c) {
    var co = Math.cos(c.th), si = Math.sin(c.th), k = KMAX * c.m * c.op, q = Math.sqrt(1 + k * k), z = c.sc;
    var P = function (x, y) { return [c.x + z * (x * co - y * si), c.y + z * (x * si + y * co)]; };
    var Rr = R - RHO, hh = SLOT * (1 - c.m) + RHO * q, A = 1 + k * k, B = 2 * hh * k;
    var xo = (-B + Math.sqrt(B * B - 4 * A * (hh * hh - Rr * Rr))) / (2 * A), phi = Math.atan2(hh + k * xo, xo);
    var rc = RING * c.ring + RHO, cc = CC * c.ring, hit = false, xi = 0;
    if (c.ring > .02) {
      var b2 = hh * k - cc, ds = b2 * b2 - A * (cc * cc + hh * hh - rc * rc);
      if (ds >= 0) { xi = (-b2 + Math.sqrt(ds)) / A; hit = xi < xo; }
    }
    ctx.beginPath(); var p = P(xo, -(hh + k * xo)); ctx.moveTo(p[0], p[1]);
    ctx.arc(c.x, c.y, Rr * z, c.th - phi, c.th + phi, true);
    if (hit) {
      p = P(xi, hh + k * xi); ctx.lineTo(p[0], p[1]);
      var al = Math.atan2(hh + k * xi, xi - cc), pc = P(cc, 0);
      ctx.arc(pc[0], pc[1], rc * z, c.th + al, c.th - al + TAU, false);
    } else { p = P(0, hh); ctx.lineTo(p[0], p[1]); p = P(0, -hh); ctx.lineTo(p[0], p[1]); }
    ctx.closePath(); ctx.lineJoin = "round"; ctx.lineWidth = 2 * RHO * z; ctx.fill(); ctx.stroke();
  }

  function draw(t) {
    cur = t;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);
    ctx.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * ox, dpr * oy); ctx.fillStyle = ctx.strokeStyle = ink;
    var c = cState(t), e = eState(t, c);

    /* O and N: drawn toward the C as it nears (lean), then taken in as its leading edge crosses them (compress, fall toward the body) */
    ["O", "N"].forEach(function (k) {
      var L = LET[k], a = cl((L.r - (c.x - R * c.sc)) / (L.r - L.l)); if (a > L.m) L.m = a; a = L.m;
      var pre = sm(cl(1 - ((c.x - R * c.sc) - L.r) / 220)) * (1 - sm(cl(a * 6))), g = sm(a);
      glyph(k, 14 * pre + (c.x - L.c[0]) * g * .8, 0, (1 - .9 * g) * (1 - .15 * g), 1 - .9 * g, .03 * pre, 1 - sm(cl((a - .8) / .2)));
    });
    var dx = Math.max(0, t - 5.04) * 850;                /* the dot: shoved clear by E's bolt, carries on out of frame */
    if (LET.P.c[0] + dx < 2060) glyph("P", dx, 0, 1, 1, 0, 1);
    glyph("E", e.x - LET.E.c[0], e.y - CY, e.s, e.s, e.r, e.a);

    var p1 = oc(cl((t - TR - .5) / .9)), p2 = oc(cl((t - TR - .65) / .9));
    if (t > TR) {
      ctx.globalAlpha = p1; ctx.save(); ctx.translate(0, (1 - p1) * 44); drawD(FIN.D); ctx.restore();
      ctx.globalAlpha = p2; ctx.save(); ctx.translate(0, (1 - p2) * 44); drawT(FIN.T); ctx.restore(); ctx.globalAlpha = 1;
    }
    drawC(c);
  }
  function reset() { LET.O.m = LET.N.m = 0; }

  /* ---------- trigger, lock, play ---------- */
  var state = 0, raf = 0, last = 0, tNow = 0, lockY = 0, KEYS = { 32: 1, 33: 1, 34: 1, 35: 1, 36: 1, 37: 1, 38: 1, 39: 1, 40: 1 };
  function stop(e) { e.preventDefault(); }
  function key(e) { if (KEYS[e.keyCode]) e.preventDefault(); }
  function hold() { if (window.scrollY !== lockY) window.scrollTo({ top: lockY, behavior: "instant" }); }
  function lock(on) {
    var m = on ? addEventListener : removeEventListener;
    m("wheel", stop, { passive: false }); m("touchmove", stop, { passive: false }); m("keydown", key); m("scroll", hold);
    document.documentElement.style.overscrollBehavior = on ? "none" : ""; fx.classList.toggle("fx-seq-lock", on);
  }
  function frame(now) {
    tNow += Math.min(.05, (now - last) / 1000); last = now;
    if (tNow >= END) { draw(END); state = 2; lock(false); fx.classList.add("fx-seq-done"); return; }
    draw(tNow); if (tNow > .6) fx.classList.add("fx-seq-live"); raf = requestAnimationFrame(frame);
  }
  function play(top) {
    state = 1; window.scrollTo({ top: window.scrollY + top, behavior: "instant" }); lockY = window.scrollY; lock(true);
    reset(); tNow = 0; draw(0); fx.classList.remove("fx-seq-done"); fx.classList.add("fx-seq-on");
    last = performance.now(); raf = requestAnimationFrame(frame);
  }
  function check() {
    var r = fx.getBoundingClientRect();
    if (state === 0 && r.top <= 3 && r.bottom > innerHeight * .6 && fx.classList.contains("fx-ready")) {
      if (reduce) { state = 2; reset(); draw(END); fx.classList.add("fx-seq-on", "fx-seq-live", "fx-seq-done"); } else play(r.top);
    } else if (state === 2 && r.top >= innerHeight * .98 && !reduce) { state = 0; reset(); draw(0); fx.classList.remove("fx-seq-done"); }   /* scrolled back above: re-arm */
  }

  simulate(); size();
  if ("ResizeObserver" in window) new ResizeObserver(size).observe(fx); else addEventListener("resize", size);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(size);
  addEventListener("scroll", function () { if (state !== 1) check(); }, { passive: true });
  check();
})();
