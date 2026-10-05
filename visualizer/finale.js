/* FINALE v3 — the noth.in effect on the word "ONCE."
   A giant black word on a light page. The cursor drags a heavy, springy blob of black goo (metaball: lumps fuse, the tail thins and
   snakes, sags with gravity, flings droplets when you whip the mouse). Only where the goo overlaps the LETTERS does an image show through.

   VARIANTS (new in v3): every letter shows a different painted material - glass-bubble foam, liquid chrome, blue foil, gold foil,
   pink cream, iridescent bubble wrap. Each time the goo comes back to a letter after leaving it, that letter swaps to the next one.
   Own images/videos (like noth.in): <script>window.FINALE_MEDIA=["a.mp4","b.jpg"]</script> before this script, or data-media="a.mp4,b.jpg" on
   <footer class="site-footer">. They join the rotation. Presentation only; reads/writes no app state. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var WORD = window.FINALE_WORD || "ONCE.", SC = .6, LIFE = 2700, TS = 448, TCSS = 520, TAU = 6.2832;

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;
    var ROTATE = footer.getAttribute("data-rotate") === "true";
    var MEDIA = window.FINALE_MEDIA || footer.getAttribute("data-media") || "";
    if (typeof MEDIA === "string") MEDIA = MEDIA ? MEDIA.split(",") : [];

    var fx = document.createElement("div");
    fx.className = "fx"; fx.id = "fx-finale";
    fx.innerHTML =
      '<canvas class="fx-cv" aria-hidden="true"></canvas>' +
      '<div class="fx-ui">' +
        '<div class="fx-top"><h2 class="fx-h">Ask a thousand times.<br>Booked exactly once.</h2>' +
          '<div class="fx-cta"><a class="fx-pill" href="booking.html">Book a seat <span aria-hidden="true">\u2192</span></a></div></div>' +
        '<div class="fx-low"><p class="fx-sub">Double clicks, retries, races and crashes can\u2019t double-book a seat or double-charge a card. Proven live, not just promised.</p>' +
          '<div class="fx-links"><button type="button" class="fx-replay">Replay tour</button><a href="https://github.com/BlackHat-Dot/Distributed-transaction-coordinator" target="_blank" rel="noopener noreferrer">GitHub</a></div></div>' +
      '</div>';
    footer.insertBefore(fx, footer.firstChild);
    fx.querySelector(".fx-replay").addEventListener("click", function () { if (window.startScrollTour) window.startScrollTour(); });

    var cv = fx.querySelector(".fx-cv"), ctx = cv.getContext("2d");
    if (!ctx) return;
    var tp = fx.querySelector(".fx-top"), low = fx.querySelector(".fx-low");
    var cs = getComputedStyle(fx), tok = function (n, d) { return (cs.getPropertyValue(n) || "").trim() || d; };
    var INK = tok("--fx-ink", "#07080a"), IVORY = tok("--fx-ivory", "#f3f1ec");
    var ink = [parseInt(INK.slice(1, 3), 16) || 7, parseInt(INK.slice(3, 5), 16) || 8, parseInt(INK.slice(5, 7), 16) || 10];
    var FONT = '"Montserrat","Schibsted Grotesk",Arial Black,system-ui,sans-serif';

    function mk(rf) { var c = document.createElement("canvas"); return { c: c, x: c.getContext("2d", rf ? { willReadFrequently: true } : undefined) }; }
    var txt = mk(), mask = mk(), F = mk(true), M = mk(), B = mk();
    var W = 0, H = 0, dpr = 1, fw = 1, fh = 1, R = 120, ZL = [], ZR = [];
    var seed = 11, rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };

    var spr = document.createElement("canvas"); spr.width = spr.height = 96;
    (function () { var g = spr.getContext("2d"), r = g.createRadialGradient(48, 48, 0, 48, 48, 48);
      r.addColorStop(0, "rgba(0,0,0,1)"); r.addColorStop(.45, "rgba(0,0,0,.92)"); r.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = r; g.fillRect(0, 0, 96, 96); })();

    var stamps = [], drips = [], cur = { x: 0, y: 0 }, vel = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, last = { x: 0, y: 0 }, headR = 0, spd = 0;
    var inside = false, userUsed = false, ready = false, visible = false, running = false, raf = 0, lastT = 0, lastDrip = 0, lastFling = 0, T0 = performance.now();

    /* ================= VARIANTS: painted materials (seamless tiles, built lazily) ================= */
    function mkNoise(P) { var g = new Float32Array(P * P), i; for (i = 0; i < g.length; i++) g[i] = rnd();
      return function (x, y) { var xi = Math.floor(x), yi = Math.floor(y), fx_ = x - xi, fy = y - yi; fx_ = fx_ * fx_ * (3 - 2 * fx_); fy = fy * fy * (3 - 2 * fy);
        var x0 = ((xi % P) + P) % P, x1 = (x0 + 1) % P, y0 = ((yi % P) + P) % P, y1 = (y0 + 1) % P, a = g[y0 * P + x0], b = g[y0 * P + x1], c = g[y1 * P + x0], d = g[y1 * P + x1];
        return a + (b - a) * fx_ + (c - a) * fy + (a - b - c + d) * fx_ * fy; }; }
    function mkFbm(oct, P0, rough) { var ns = [], o; for (o = 0; o < oct; o++) ns.push(mkNoise(P0 << o));
      return function (u, v) { var s = 0, a = 1, t = 0, o2; for (o2 = 0; o2 < oct; o2++) { s += a * ns[o2](u * (P0 << o2), v * (P0 << o2)); t += a; a *= rough; } return s / t; }; }
    function lut(st) { var L = new Uint8Array(768), i, j, t; for (i = 0; i < 256; i++) { t = i / 255;
        for (j = 1; j < st.length - 1 && st[j][0] < t; j++); var a = st[j - 1], b = st[j], k = clamp((t - a[0]) / (b[0] - a[0]), 0, 1); k = k * k * (3 - 2 * k);
        L[i * 3] = a[1] + (b[1] - a[1]) * k; L[i * 3 + 1] = a[2] + (b[2] - a[2]) * k; L[i * 3 + 2] = a[3] + (b[3] - a[3]) * k; } return L; }
    function bake(hfn, K, colfn) {
      var S = TS, c = document.createElement("canvas"); c.width = c.height = S; var g = c.getContext("2d"), id = g.createImageData(S, S), d = id.data, h = new Float32Array(S * S), x, y, o = [0, 0, 0];
      for (y = 0; y < S; y++) for (x = 0; x < S; x++) h[y * S + x] = hfn(x / S, y / S);
      for (y = 0; y < S; y++) for (x = 0; x < S; x++) {
        var xm = (x + S - 1) % S, xp = (x + 1) % S, ym = (y + S - 1) % S, yp = (y + 1) % S;
        var nx = (h[y * S + xm] - h[y * S + xp]) * K, ny = (h[ym * S + x] - h[yp * S + x]) * K, il = 1 / Math.sqrt(nx * nx + ny * ny + 1);
        nx *= il; ny *= il; colfn(nx, ny, il, h[y * S + x], o); var i = (y * S + x) * 4; d[i] = o[0]; d[i + 1] = o[1]; d[i + 2] = o[2]; d[i + 3] = 255; }
      g.putImageData(id, 0, 0); return { c: c, css: TCSS, sp: .02, pat: null };
    }
    var spec = function (nx, ny, nz, p) { return Math.pow(Math.max(0, -.45 * nx - .55 * ny + .7 * nz), p); };
    function envFn(L, a, b, sp) { return function (nx, ny, nz, h, o) { var t = clamp(.5 + .5 * (nx * a + ny * b), 0, 1), i = (t * 255 | 0) * 3, s = sp ? spec(nx, ny, nz, 40) * 255 * sp : 0;
        o[0] = Math.min(255, L[i] + s); o[1] = Math.min(255, L[i + 1] + s); o[2] = Math.min(255, L[i + 2] + s); }; }
    var BUILD = [
      function chrome() { var f = mkFbm(3, 3, .5), w = mkFbm(2, 2, .5);
        return bake(function (u, v) { return f(u + .22 * w(u, v), v + .22 * w(v + .3, u)); }, 220,
          envFn(lut([[0, 6, 8, 12], [.16, 70, 88, 115], [.28, 255, 255, 255], [.4, 14, 20, 30], [.58, 150, 175, 210], [.76, 255, 255, 255], [.9, 26, 36, 54], [1, 8, 10, 16]]), 1.5, 1.1, .25)); },
      function foilBlue() { var f = mkFbm(3, 3, .5);
        return bake(function (u, v) { var n = 1 - Math.abs(2 * f(u, v) - 1); return n * n; }, 75,
          envFn(lut([[0, 4, 8, 40], [.22, 20, 60, 200], [.42, 90, 170, 255], [.58, 235, 248, 255], [.74, 30, 80, 220], [1, 6, 14, 70]]), 1.3, 1.3, .6)); },
      function cream() { var f = mkFbm(3, 2, .5), L = [-.4, -.5, .77];
        return bake(function (u, v) { return .5 + .5 * Math.sin(TAU * 6 * f(u, v)); }, 26, function (nx, ny, nz, h, o) {
          var dif = clamp(L[0] * nx + L[1] * ny + L[2] * nz, 0, 1), sp = spec(nx, ny, nz, 36) * 255, k = .42 + .7 * dif;
          o[0] = Math.min(255, 250 * k + sp); o[1] = Math.min(255, 150 * k + sp * .9); o[2] = Math.min(255, 190 * k + sp * .95); }); },
      function foilGold() { var f = mkFbm(3, 3, .5);
        return bake(function (u, v) { var n = 1 - Math.abs(2 * f(u, v) - 1); return n * n; }, 60,
          envFn(lut([[0, 70, 42, 18], [.24, 196, 140, 66], [.44, 255, 230, 168], [.6, 255, 250, 235], [.78, 190, 132, 62], [1, 60, 36, 16]]), 1.2, 1.4, .5)); },
      function wrap() { var cols = 9, rows = 10;
        function hf(u, v) { var r = Math.floor(v * rows), best = 0, dr, dc; for (dr = -1; dr <= 1; dr++) { var rr = r + dr, off = (((rr % 2) + 2) % 2) * .5 / cols, cy = (rr + .5) / rows, c0 = Math.floor((u - off) * cols);
            for (dc = -1; dc <= 1; dc++) { var du = u - ((c0 + dc + .5) / cols + off), dv = v - cy; du -= Math.round(du); dv -= Math.round(dv);
              var q = Math.sqrt(du * du + dv * dv) / (.47 / cols); if (q < 1) { var hh = Math.sqrt(1 - q * q); if (hh > best) best = hh; } } } return best; }
        return bake(hf, 34, function (nx, ny, nz, h, o) {
          if (h < .04) { o[0] = 12; o[1] = 14; o[2] = 22; return; }
          var t = (1 - nz) * 1.8 + h * .3, b = .5 + .5 * nz, sp = spec(nx, ny, nz, 30) * 255, rim = Math.pow(1 - nz, 2) * 90;
          o[0] = Math.min(255, (.84 + .16 * Math.cos(TAU * t)) * 235 * b + sp + rim); o[1] = Math.min(255, (.84 + .16 * Math.cos(TAU * (t + .33))) * 235 * b + sp + rim); o[2] = Math.min(255, (.84 + .16 * Math.cos(TAU * (t + .67))) * 245 * b + sp + rim); }); }
    ];
    function makeFoam() {
      var S = 640, c = document.createElement("canvas"); c.width = c.height = Math.round(S * dpr); var x = c.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0);
      var sc = clamp(W / 1440, .7, 1.25), tile = function (cx, cy, r, fn) { for (var ox = -S; ox <= S; ox += S) for (var oy = -S; oy <= S; oy += S) { var X = cx + ox, Y = cy + oy; if (X + r < 0 || X - r > S || Y + r < 0 || Y - r > S) continue; fn(X, Y); } };
      x.fillStyle = "#3d2412"; x.fillRect(0, 0, S, S);
      for (var q = 0; q < 230; q++) { var bx = rnd() * S, by = rnd() * S, br = (36 + rnd() * 90) * sc, am = rnd() > .84;
        tile(bx, by, br, function (X, Y) { var g = x.createRadialGradient(X, Y, 0, X, Y, br);
          g.addColorStop(0, am ? "rgba(205,167,99,.95)" : "rgba(255,255,252,1)"); g.addColorStop(.7, am ? "rgba(168,132,70,.7)" : "rgba(246,241,231,.97)"); g.addColorStop(1, "rgba(232,226,214,0)"); x.fillStyle = g; x.fillRect(X - br, Y - br, br * 2, br * 2); }); }
      var list = [], i; for (i = 0; i < 1400; i++) list.push({ x: rnd() * S, y: rnd() * S, r: (2.5 + 42 * Math.pow(rnd(), 3.1)) * sc, tint: rnd() });
      list.sort(function (a, b) { return b.r - a.r; });
      list.forEach(function (b) { tile(b.x, b.y, b.r, function (cx, cy) { var r = b.r, body = x.createRadialGradient(cx - r * .2, cy - r * .25, r * .05, cx, cy, r);
        body.addColorStop(0, "rgba(255,252,246,.22)"); body.addColorStop(.55, "rgba(236,226,210,.12)"); body.addColorStop(.8, "rgba(80,52,30,.34)"); body.addColorStop(.92, "rgba(255,255,255,.98)"); body.addColorStop(1, "rgba(255,255,255,0)");
        x.fillStyle = body; x.beginPath(); x.arc(cx, cy, r, 0, TAU); x.fill();
        if (b.tint > .94 && r > 6) { var cg = x.createRadialGradient(cx + r * .25, cy + r * .4, 0, cx + r * .25, cy + r * .4, r * .75); cg.addColorStop(0, "rgba(230,208,160,.22)"); cg.addColorStop(1, "rgba(230,208,160,0)"); x.fillStyle = cg; x.beginPath(); x.arc(cx, cy, r * .93, 0, TAU); x.fill(); }
        if (r > 4) { x.fillStyle = "rgba(255,255,255,1)"; x.beginPath(); x.ellipse(cx - r * .36, cy - r * .42, r * .27, r * .14, -.6, 0, TAU); x.fill(); x.fillStyle = "rgba(255,255,255,.4)"; x.beginPath(); x.arc(cx + r * .42, cy + r * .46, r * .09, 0, TAU); x.fill(); } }); });
      return { c: c, css: S, sp: .022, pat: null };
    }
    var TILES = [], NV = 1 + BUILD.length + MEDIA.length, vz = [MEDIA.length ? 1 + BUILD.length : 0], zt = [0, 0, 0, 0, 0], built = 0, timer = 0;
    MEDIA.forEach(function (src) { src = src.trim(); var m = { media: true, ok: false };
      if (/\.(mp4|webm|mov|m4v)(\?|$)/i.test(src)) { m.el = document.createElement("video"); m.el.src = src; m.el.muted = true; m.el.loop = true; m.el.playsInline = true; m.el.preload = "auto"; m.el.setAttribute("muted", "");
        m.el.addEventListener("loadeddata", function () { m.ok = true; }); }
      else { m.el = new Image(); m.el.onload = function () { m.ok = true; }; m.el.src = src; }
      TILES[1 + BUILD.length + MEDIA.indexOf(src)] = m; });
    function queueBuild() { if (timer || built >= BUILD.length) return; timer = setTimeout(function () { timer = 0; seed = 7 + built * 31; TILES[1 + built] = BUILD[built](); built++; kick(); queueBuild(); }, built ? 220 : 500); }
    function drawMedia(x, m, bx, by, bw, bh) { var el = m.el, w = el.videoWidth || el.naturalWidth, h = el.videoHeight || el.naturalHeight; if (!m.ok || !w || !h) return false;
      if (el.paused && el.play) { var pr = el.play(); if (pr && pr.catch) pr.catch(function () {}); }
      var k = Math.max(W / w, H / h), dw = w * k, dh = h * k; x.drawImage(el, (W - dw) / 2, (H - dh) / 2, dw, dh); return true; }
    function paintZone(b, i, x0, y0, w, h, t) {
      var v = vz[i], T = TILES[v]; if (!T || (T.media && !T.ok)) T = TILES[0]; if (!T) return;
      b.save(); b.beginPath(); b.rect(x0, y0, w, h); b.clip();
      if (T.media) drawMedia(b, T);
      else { if (!T.pat) T.pat = B.x.createPattern(T.c, "repeat"); var px = (W / 2 - cur.x) * .08, py = (H / 2 - cur.y) * .08, ph = i * 173;
        if (T.pat.setTransform && window.DOMMatrix) T.pat.setTransform(new DOMMatrix().translate(-t * T.sp + px + ph, -t * T.sp * .55 + py + ph * .6).scale(T.css / T.c.width));
        b.fillStyle = T.pat; b.fillRect(x0, y0, w, h); }
      b.restore();
    }

    function ls(x, px) { if ("letterSpacing" in x) x.letterSpacing = px + "px"; }
    function build() {
      var r = fx.getBoundingClientRect(); W = Math.round(r.width); H = Math.round(r.height);
      if (W < 50 || H < 50) return;
      dpr = Math.min(window.devicePixelRatio || 1, W * H > 1.6e6 ? 1.5 : 2);
      [cv, txt.c, mask.c, B.c].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
      fw = Math.ceil(W * SC); fh = Math.ceil(H * SC); F.c.width = M.c.width = fw; F.c.height = M.c.height = fh;
      [txt.x, mask.x, B.x, ctx].forEach(function (x) { x.setTransform(dpr, 0, 0, dpr, 0, 0); });
      R = clamp(W * .125, 80, 270);
      seed = 11; TILES[0] = makeFoam(); queueBuild();
      var x = txt.x; x.font = "900 100px " + FONT; ls(x, -2);
      var em = x.measureText(WORD).width / 100, m = x.measureText("O"), asc = (m.actualBoundingBoxAscent || 72) / 100, capEm = asc + (m.actualBoundingBoxDescent || 0) / 100;
      var topR = tp.offsetHeight + 30, lowR = low.offsetHeight + 30, padX = clamp(W * .012, 8, 24);
      var fs = Math.min((W - padX * 2) / em, (H - topR - lowR) / capEm * 1.02);
      var cy = topR + (H - topR - lowR) / 2 + (W < 760 ? 0 : H * .03), base = cy + (asc * fs - capEm * fs / 2);
      [txt.x, mask.x].forEach(function (c) { c.clearRect(0, 0, W, H); c.font = "900 " + fs + "px " + FONT; ls(c, -fs * .02); c.textAlign = "center"; c.textBaseline = "alphabetic"; });
      txt.x.fillStyle = IVORY; txt.x.fillRect(0, 0, W, H); txt.x.fillStyle = INK; txt.x.fillText(WORD, W / 2, base);
      mask.x.fillStyle = "#fff"; mask.x.fillText(WORD, W / 2, base); mask.x.strokeStyle = "#fff"; mask.x.lineJoin = "round"; mask.x.lineWidth = fs * .014; mask.x.strokeText(WORD, W / 2, base);
      var tot = txt.x.measureText(WORD).width, x0 = W / 2 - tot / 2, i; ZL = []; ZR = [];       /* one zone per letter */
      for (i = 0; i < WORD.length; i++) { ZL.push(i ? ZR[i - 1] : -1e4); ZR.push(i === WORD.length - 1 ? 1e4 : x0 + txt.x.measureText(WORD.slice(0, i + 1)).width); }
      cur.x = tgt.x = last.x = clamp(cur.x || W / 2, 0, W); cur.y = tgt.y = last.y = clamp(cur.y || H / 2, 0, H);
      ready = true; fx.classList.add("fx-ready"); kick();
    }

    /* ================= THE GOO ================= */
    function addStamp(x, y, now, r, life, drip) { stamps.push({ x: x, y: y, t: now, r: r, life: life, d: drip, jx: drip ? 0 : (rnd() - .5) * .5, jy: drip ? 0 : (rnd() - .5) * .5, ph: rnd() * TAU }); if (stamps.length > 560) stamps.shift(); }
    function field(now) {
      var f = F.x, i, s, x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9; f.clearRect(0, 0, fw, fh);
      var put = function (x, y, r) { if (r * SC < 3) return; var rr = r * 1.3 * SC; f.drawImage(spr, x * SC - rr, y * SC - rr, rr * 2, rr * 2);
        x0 = Math.min(x0, x * SC - rr); y0 = Math.min(y0, y * SC - rr); x1 = Math.max(x1, x * SC + rr); y1 = Math.max(y1, y * SC + rr); };
      for (i = stamps.length - 1; i >= 0; i--) {
        s = stamps[i]; var age = now - s.t; if (age > s.life) { stamps.splice(i, 1); continue; }
        var k = 1 - age / s.life, a = 1 - k, r = s.r * (s.d ? .4 + .6 * k : .2 + .8 * Math.pow(k, .85));
        /* the tail snakes sideways and sags with gravity the older it gets */
        put(s.x + s.jx * r + Math.sin(now * .0034 + s.ph) * a * R * .2, s.y + s.jy * r + a * a * R * .42, r);
      }
      if (headR > .8) { var an = now * .0021, hr = headR * (1 + .06 * Math.sin(now * .008)), st = clamp(spd * .5, 0, .6), ux = spd > .05 ? vel.x / (spd * 1000 / 1000 + 1e-6) : 0;
        put(cur.x, cur.y, hr);                                                              /* the head is a lumpy mass that trails behind the motion */
        put(cur.x + Math.cos(an) * hr * .5 - vel.x * 70 * st, cur.y + Math.sin(an * 1.3) * hr * .45 - vel.y * 70 * st, hr * .66);
        put(cur.x + Math.cos(-an * .8 + 2) * hr * .56 - vel.x * 120 * st, cur.y + Math.sin(an * .9 + 1) * hr * .5 - vel.y * 120 * st, hr * .55); }
      if (x1 < x0) return null;
      x0 = Math.max(0, Math.floor(x0)); y0 = Math.max(0, Math.floor(y0)); x1 = Math.min(fw, Math.ceil(x1)); y1 = Math.min(fh, Math.ceil(y1));
      var w = x1 - x0, h = y1 - y0; if (w < 2 || h < 2) return null;
      var id = f.getImageData(x0, y0, w, h), d = id.data, n = d.length, v, t;
      for (i = 3; i < n; i += 4) { v = d[i]; if (v <= 112) { d[i] = 0; continue; } if (v >= 126) d[i] = 255; else { t = (v - 112) / 14; d[i] = t * t * (3 - 2 * t) * 255; } d[i - 3] = ink[0]; d[i - 2] = ink[1]; d[i - 1] = ink[2]; }
      M.x.clearRect(0, 0, fw, fh); M.x.putImageData(id, x0, y0); return { x0: x0, y0: y0, w: w, h: h };
    }
    function render(now) {
      ctx.globalCompositeOperation = "source-over"; ctx.drawImage(txt.c, 0, 0, W, H);
      var bb = field(now); if (!bb) return;
      var sx = bb.x0 / SC, sy = bb.y0 / SC, sw = bb.w / SC, sh = bb.h / SC;
      var bx = Math.max(0, Math.floor(sx)), by = Math.max(0, Math.floor(sy)), bw = Math.min(W, Math.ceil(sx + sw)) - bx, bh = Math.min(H, Math.ceil(sy + sh)) - by;
      ctx.drawImage(M.c, bb.x0, bb.y0, bb.w, bb.h, sx, sy, sw, sh);
      var b = B.x; b.globalCompositeOperation = "source-over"; b.clearRect(bx, by, bw, bh);
      var t = reduce ? 0 : now - T0;
      paintZone(b, 0, bx, by, bw, bh, t);
      b.globalCompositeOperation = "destination-in";
      b.drawImage(mask.c, bx * dpr, by * dpr, bw * dpr, bh * dpr, bx, by, bw, bh); b.drawImage(M.c, bb.x0, bb.y0, bb.w, bb.h, sx, sy, sw, sh);
      b.globalCompositeOperation = "source-over";
      ctx.drawImage(B.c, bx * dpr, by * dpr, bw * dpr, bh * dpr, bx, by, bw, bh);
    }

    /* ================= driver ================= */
    function ghostPos(now) { var t = now * .0006, topR = tp.offsetHeight + 30, aH = H - topR - low.offsetHeight - 30;
      return { x: W * (.5 + .36 * Math.sin(t * 1.15)), y: topR + aH * (.5 + .3 * Math.sin(t * 1.9 + 1.2)) }; }
    function drip(now, x, y, vx, vy, r, life, g) { if (drips.length < 14) drips.push({ x: x, y: y, vx: vx, vy: vy, r: r, t: now, life: life, g: g, acc: 0 }); }
    function step(now) {
      raf = 0; if (!ready || !visible) { running = false; return; }
      var dt = clamp(now - (lastT || now - 16), 1, 40); lastT = now;
      var auto = !reduce && !inside && !userUsed, active = inside || auto, i;
      if (auto) { var g = ghostPos(now); if (!stamps.length && headR < 1) { cur.x = last.x = g.x; cur.y = last.y = g.y; } tgt.x = g.x; tgt.y = g.y; }
      if (active) {                                              /* the head is a heavy spring: it lags, overshoots a little, wobbles */
        var K = inside ? .0011 : .0006, C = 2 * .6 * Math.sqrt(K), n = Math.ceil(dt / 8), h = dt / n, s;
        for (s = 0; s < n; s++) { vel.x += ((tgt.x - cur.x) * K - vel.x * C) * h; vel.y += ((tgt.y - cur.y) * K - vel.y * C) * h; cur.x += vel.x * h; cur.y += vel.y * h; }
      } else { vel.x *= .9; vel.y *= .9; }
      spd += (Math.sqrt(vel.x * vel.x + vel.y * vel.y) - spd) * .2;
      var hT = active ? R * (.8 + clamp(spd * .16, 0, .5)) : 0;      /* fast = fatter, slow = tighter */
      headR += (hT - headR) * (1 - Math.exp(-dt / (active ? 100 : 170)));
      if (active) {
        var dx = cur.x - last.x, dy = cur.y - last.y, dist = Math.sqrt(dx * dx + dy * dy), gap = R * .15;
        if (dist > gap) { var m = Math.min(26, Math.floor(dist / gap)), j, q, rr;
          for (j = 1; j <= m; j++) { q = j / m; rr = headR * (.78 + rnd() * .22); var px = last.x + dx * q, py = last.y + dy * q;
            addStamp(px, py, now, rr, LIFE, false);
            if (rnd() < .4) { var a = rnd() * TAU, o = rr * (.85 + rnd() * .5); addStamp(px + Math.cos(a) * o, py + Math.sin(a) * o, now, rr * (.16 + rnd() * .22), LIFE * .55, false); }  /* ragged lumps on the edge */
          } last.x = cur.x; last.y = cur.y; }
        if (spd > 1.5 && now - lastFling > 90) { lastFling = now; for (i = 0; i < 2; i++) drip(now, cur.x, cur.y, vel.x * (.12 + rnd() * .2) + (rnd() - .5) * .3, vel.y * (.12 + rnd() * .2) - .1 - rnd() * .2, 7 + rnd() * R * .12, 520 + rnd() * 380, .0016); }  /* whip the mouse = splatter */
        if (now - lastDrip > 190 && stamps.length > 24) { lastDrip = now; var sp = stamps[Math.floor(rnd() * stamps.length * .6)]; if (sp && !sp.d) drip(now, sp.x, sp.y + R * .2, 0, .02, 8 + rnd() * R * .07, 900 + rnd() * 500, .00009); }  /* long drips from the old trail */
        if (ROTATE) { if (headR > R * .3) { if (zt[0] && now - zt[0] > 2000) vz[0] = (vz[0] + 1) % NV; zt[0] = 0; } else if (!zt[0]) zt[0] = now; }   /* come back to a letter = a new material */
      }
      for (i = drips.length - 1; i >= 0; i--) { var d = drips[i], age = now - d.t; if (age > d.life) { drips.splice(i, 1); continue; }
        d.vy += d.g * dt; d.x += d.vx * dt; d.y += d.vy * dt; d.acc += Math.abs(d.vy * dt) + Math.abs(d.vx * dt);
        if (d.acc > d.r * .35) { d.acc = 0; addStamp(d.x, d.y, now, d.r * (1 - age / d.life * .45), 460, true); } }
      render(now);
      if (stamps.length || drips.length || headR > .8 || auto) { running = true; raf = requestAnimationFrame(step); } else { running = false; lastT = 0; }
    }
    function kick() { if (!running && !raf) { running = true; raf = requestAnimationFrame(step); } }
    function setPtr(e) { var r = fx.getBoundingClientRect(); tgt.x = e.clientX - r.left; tgt.y = e.clientY - r.top;
      if (!inside) { cur.x = last.x = tgt.x; cur.y = last.y = tgt.y; vel.x = vel.y = 0; } inside = true; userUsed = true; kick(); }
    fx.addEventListener("pointermove", setPtr, { passive: true }); fx.addEventListener("pointerdown", setPtr, { passive: true });
    function out() { inside = false; kick(); } fx.addEventListener("pointerleave", out); fx.addEventListener("pointercancel", out);
    if ("IntersectionObserver" in window) new IntersectionObserver(function (en) { visible = en[0].isIntersecting; if (visible) kick(); }, { threshold: 0 }).observe(fx); else visible = true;
    document.addEventListener("visibilitychange", function () { if (!document.hidden) kick(); });
    var rt = 0, lw = 0, lh = 0;
    function onSize() { clearTimeout(rt); rt = setTimeout(function () { var r = fx.getBoundingClientRect(); if (Math.round(r.width) !== lw || Math.round(r.height) !== lh) { lw = Math.round(r.width); lh = Math.round(r.height); build(); render(performance.now()); } }, 120); }
    if ("ResizeObserver" in window) new ResizeObserver(onSize).observe(fx); else addEventListener("resize", onSize);
    var fonts = (document.fonts && document.fonts.load) ? Promise.all([document.fonts.load('900 100px "Montserrat"'), document.fonts.load('500 20px "Schibsted Grotesk"')]).catch(function () {}) : Promise.resolve();
    fonts.then(function () { var r = fx.getBoundingClientRect(); lw = Math.round(r.width); lh = Math.round(r.height); build(); render(performance.now()); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
