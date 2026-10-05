/* FINALE v2 — the noth.in effect on the word "ONCE."
   Giant black word on a light page. The cursor drags a blob of black GOO (metaball: stamps merge, stretch, snap into drips).
   Only where the goo overlaps the LETTERS does the foam image show through. Presentation only; touches no app state.

   v2 fixes vs v1: the goo is now a real metaball (thresholded soft sprites, so lumps fuse and tails thin out instead of a round pipe);
   everything is time-based (same feel at 30 / 60 / 144 fps); the blob field is solved at half resolution and ONLY the dirty
   rectangle around the goo is recomposited (was: 5 full-screen canvas passes every frame); the ghost demo hands over without a jump;
   the foam is cream with amber cells like the reference.

   YOUR OWN FOAM (exactly like noth.in): <script>window.FINALE_MEDIA="foam.mp4";</script> before this script
   (.mp4/.webm/.jpg/.png/.webp) or data-media="foam.mp4" on <footer class="site-footer">. Best: foam letters on black, same as the reference. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var WORD = "ONCE.", SC = .5, LIFE = 1050;

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;
    var MEDIA = window.FINALE_MEDIA || footer.getAttribute("data-media") || "";

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
    var txt = mk(), mask = mk(), F = mk(true), M = mk(), B = mk(), foamA = mk(), foamB = mk();
    var W = 0, H = 0, dpr = 1, fw = 1, fh = 1, R = 120, S1 = 640, S2 = 420, pA = null, pB = null;
    var seed = 11, rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };

    /* soft sprite: the building block of the goo (alpha 1 in the core, 0 at the rim) */
    var spr = document.createElement("canvas"); spr.width = spr.height = 96;
    (function () { var g = spr.getContext("2d"), r = g.createRadialGradient(48, 48, 0, 48, 48, 48);
      r.addColorStop(0, "rgba(0,0,0,1)"); r.addColorStop(.45, "rgba(0,0,0,.92)"); r.addColorStop(1, "rgba(0,0,0,0)"); g.fillStyle = r; g.fillRect(0, 0, 96, 96); })();

    var stamps = [], drips = [], cur = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, last = { x: 0, y: 0 }, headR = 0;
    var inside = false, userUsed = false, ready = false, visible = false, running = false, raf = 0, lastT = 0, lastDrip = 0, T0 = performance.now();

    /* ---------- the image inside the letters ---------- */
    var vid = null, img = null, mediaOK = false;
    if (MEDIA) {
      if (/\.(mp4|webm|mov|m4v)(\?|$)/i.test(MEDIA)) {
        vid = document.createElement("video"); vid.src = MEDIA; vid.muted = true; vid.loop = true; vid.playsInline = true; vid.preload = "auto"; vid.setAttribute("muted", "");
        vid.addEventListener("loadeddata", function () { mediaOK = true; }); vid.addEventListener("error", function () { mediaOK = false; });
      } else { img = new Image(); img.onload = function () { mediaOK = true; }; img.src = MEDIA; }
    }
    function drawMedia(x) {
      var el = vid || img, w = vid ? vid.videoWidth : img.naturalWidth, h = vid ? vid.videoHeight : img.naturalHeight;
      if (!w || !h) return false;
      var k = Math.max(W / w, H / h), dw = w * k, dh = h * k; x.drawImage(el, (W - dw) / 2, (H - dh) / 2, dw, dh); return true;
    }

    /* procedural foam: cream masses, amber cells, clear glass bubbles. Seamless tile so it can drift forever. */
    function makeFoam(t, S, withBase, scale, count) {
      t.c.width = t.c.height = Math.round(S * dpr);
      var x = t.x; x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, S, S);
      var tile = function (cx, cy, r, fn) { for (var ox = -S; ox <= S; ox += S) for (var oy = -S; oy <= S; oy += S) { var X = cx + ox, Y = cy + oy; if (X + r < 0 || X - r > S || Y + r < 0 || Y - r > S) continue; fn(X, Y); } };
      if (withBase) {
        x.fillStyle = "#6e4623"; x.fillRect(0, 0, S, S);
        for (var q = 0; q < 170; q++) {
          var bx = rnd() * S, by = rnd() * S, br = (36 + rnd() * 90) * scale, amber = rnd() > .84;
          tile(bx, by, br, function (X, Y) {
            var g = x.createRadialGradient(X, Y, 0, X, Y, br);
            g.addColorStop(0, amber ? "rgba(224,160,70,.9)" : "rgba(252,250,244,1)"); g.addColorStop(.6, amber ? "rgba(200,135,50,.6)" : "rgba(240,234,222,.92)"); g.addColorStop(1, "rgba(232,226,214,0)");
            x.fillStyle = g; x.fillRect(X - br, Y - br, br * 2, br * 2);
          });
        }
      }
      var list = [], i;
      for (i = 0; i < count; i++) list.push({ x: rnd() * S, y: rnd() * S, r: (2.5 + 42 * Math.pow(rnd(), 3.1)) * scale, tint: rnd() });
      list.sort(function (a, b) { return b.r - a.r; });
      list.forEach(function (b) {
        tile(b.x, b.y, b.r, function (cx, cy) {
          var r = b.r, body = x.createRadialGradient(cx - r * .2, cy - r * .25, r * .05, cx, cy, r);
          body.addColorStop(0, "rgba(255,252,246,.14)"); body.addColorStop(.62, "rgba(240,232,218,.20)");
          body.addColorStop(.86, "rgba(90,60,35,.16)"); body.addColorStop(.96, "rgba(255,255,255,.9)"); body.addColorStop(1, "rgba(255,255,255,0)");
          x.fillStyle = body; x.beginPath(); x.arc(cx, cy, r, 0, 6.2832); x.fill();
          if (b.tint > .94 && r > 6) { var cg = x.createRadialGradient(cx + r * .25, cy + r * .4, 0, cx + r * .25, cy + r * .4, r * .75);
            cg.addColorStop(0, "rgba(130,175,255,.22)"); cg.addColorStop(1, "rgba(120,170,255,0)"); x.fillStyle = cg; x.beginPath(); x.arc(cx, cy, r * .93, 0, 6.2832); x.fill(); }
          if (r > 4) { x.fillStyle = "rgba(255,255,255,.95)"; x.beginPath(); x.ellipse(cx - r * .38, cy - r * .42, r * .2, r * .11, -.6, 0, 6.2832); x.fill();
            x.fillStyle = "rgba(255,255,255,.4)"; x.beginPath(); x.arc(cx + r * .42, cy + r * .46, r * .09, 0, 6.2832); x.fill(); }
        });
      });
    }

    function ls(x, px) { if ("letterSpacing" in x) x.letterSpacing = px + "px"; }
    function build() {
      var r = fx.getBoundingClientRect(); W = Math.round(r.width); H = Math.round(r.height);
      if (W < 50 || H < 50) return;
      dpr = Math.min(window.devicePixelRatio || 1, W * H > 1.6e6 ? 1.5 : 2);
      [cv, txt.c, mask.c, B.c].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
      fw = Math.ceil(W * SC); fh = Math.ceil(H * SC); F.c.width = M.c.width = fw; F.c.height = M.c.height = fh;
      [txt.x, mask.x, B.x, ctx].forEach(function (x) { x.setTransform(dpr, 0, 0, dpr, 0, 0); });
      R = clamp(W * .085, 64, 190);
      seed = 11; var sc = clamp(W / 1440, .7, 1.25);
      makeFoam(foamA, S1, true, sc, 1400); makeFoam(foamB, S2, false, sc * .55, 800);
      pA = B.x.createPattern(foamA.c, "repeat"); pB = B.x.createPattern(foamB.c, "repeat");

      var x = txt.x; x.font = "900 100px " + FONT; ls(x, -2);
      var em = x.measureText(WORD).width / 100, m = x.measureText("O"), asc = (m.actualBoundingBoxAscent || 72) / 100, capEm = asc + (m.actualBoundingBoxDescent || 0) / 100;
      var topR = tp.offsetHeight + 30, lowR = low.offsetHeight + 30, padX = clamp(W * .012, 8, 24);
      var fs = Math.min((W - padX * 2) / em, (H - topR - lowR) / capEm * 1.02);
      var cy = topR + (H - topR - lowR) / 2 + (W < 760 ? 0 : H * .03), base = cy + (asc * fs - capEm * fs / 2);
      [txt.x, mask.x].forEach(function (c) { c.clearRect(0, 0, W, H); c.font = "900 " + fs + "px " + FONT; ls(c, -fs * .02); c.textAlign = "center"; c.textBaseline = "alphabetic"; });
      txt.x.fillStyle = IVORY; txt.x.fillRect(0, 0, W, H); txt.x.fillStyle = INK; txt.x.fillText(WORD, W / 2, base);
      mask.x.fillStyle = "#fff"; mask.x.fillText(WORD, W / 2, base);
      cur.x = tgt.x = last.x = clamp(cur.x || W / 2, 0, W); cur.y = tgt.y = last.y = clamp(cur.y || H / 2, 0, H);
      ready = true; fx.classList.add("fx-ready"); kick();
    }

    /* ---------- goo: stamps + drips -> soft field -> threshold ---------- */
    function addStamp(x, y, now, r, life, drip) { stamps.push({ x: x, y: y, t: now, r: r, life: life, jx: drip ? 0 : (rnd() - .5) * .55, jy: drip ? 0 : (rnd() - .5) * .55, d: drip }); if (stamps.length > 340) stamps.shift(); }
    function field(now) {
      var f = F.x, i, s, x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      f.clearRect(0, 0, fw, fh);
      var put = function (x, y, r) { var rr = r * 1.3 * SC; f.drawImage(spr, x * SC - rr, y * SC - rr, rr * 2, rr * 2);
        x0 = Math.min(x0, x * SC - rr); y0 = Math.min(y0, y * SC - rr); x1 = Math.max(x1, x * SC + rr); y1 = Math.max(y1, y * SC + rr); };
      for (i = stamps.length - 1; i >= 0; i--) {
        s = stamps[i]; var age = now - s.t; if (age > s.life) { stamps.splice(i, 1); continue; }
        var k = 1 - age / s.life, r = s.r * (s.d ? .45 + .55 * k : .28 + .72 * Math.pow(k, .75));
        put(s.x + s.jx * r, s.y + s.jy * r, r);
      }
      if (headR > .8) { /* the head: a lumpy mass, not a circle */
        var a = now * .0019, hr = headR * (1 + .05 * Math.sin(now * .007));
        put(cur.x, cur.y, hr);
        put(cur.x + Math.cos(a) * hr * .55, cur.y + Math.sin(a * 1.3) * hr * .5, hr * .62);
        put(cur.x + Math.cos(a * -.8 + 2) * hr * .6, cur.y + Math.sin(a * .9 + 1) * hr * .55, hr * .55);
      }
      if (x1 < x0) return null;
      x0 = Math.max(0, Math.floor(x0)); y0 = Math.max(0, Math.floor(y0)); x1 = Math.min(fw, Math.ceil(x1)); y1 = Math.min(fh, Math.ceil(y1));
      var w = x1 - x0, h = y1 - y0; if (w < 2 || h < 2) return null;
      var id = f.getImageData(x0, y0, w, h), d = id.data, n = d.length, v, t;
      for (i = 3; i < n; i += 4) { v = d[i];
        if (v <= 112) { d[i] = 0; continue; }
        if (v >= 126) d[i] = 255; else { t = (v - 112) / 14; d[i] = t * t * (3 - 2 * t) * 255; }
        d[i - 3] = ink[0]; d[i - 2] = ink[1]; d[i - 1] = ink[2];
      }
      M.x.clearRect(0, 0, fw, fh); M.x.putImageData(id, x0, y0);
      return { x0: x0, y0: y0, w: w, h: h };
    }
    function fill(x, p, ox, oy, bx, by, bw, bh) { if (p.setTransform && window.DOMMatrix) p.setTransform(new DOMMatrix().translate(ox, oy).scale(1 / dpr)); x.fillStyle = p; x.fillRect(bx, by, bw, bh); }

    function render(now) {
      ctx.globalCompositeOperation = "source-over";
      ctx.drawImage(txt.c, 0, 0, W, H);                          /* ivory page + ink word */
      var bb = field(now); if (!bb) return;
      var sx = bb.x0 / SC, sy = bb.y0 / SC, sw = bb.w / SC, sh = bb.h / SC;
      var bx = Math.max(0, Math.floor(sx)), by = Math.max(0, Math.floor(sy)), bw = Math.min(W, Math.ceil(sx + sw)) - bx, bh = Math.min(H, Math.ceil(sy + sh)) - by;
      ctx.drawImage(M.c, bb.x0, bb.y0, bb.w, bb.h, sx, sy, sw, sh);   /* 1. the goo itself: plain black */
      var b = B.x; b.save(); b.beginPath(); b.rect(bx, by, bw, bh); b.clip();   /* 2. foam, only inside letters AND goo, only in the dirty rect */
      b.globalCompositeOperation = "source-over"; b.clearRect(bx, by, bw, bh);
      var t = reduce ? 0 : now - T0, drew = false;
      if (mediaOK) { if (vid && vid.paused) { var pr = vid.play(); if (pr && pr.catch) pr.catch(function () {}); } drew = drawMedia(b); }
      if (!drew) { var px = (W / 2 - cur.x) * .1, py = (H / 2 - cur.y) * .1;
        fill(b, pA, -t * .022 + px, -t * .012 + py, bx, by, bw, bh); fill(b, pB, t * .03 - px * 1.6, -t * .02 - py * 1.6, bx, by, bw, bh); }
      b.globalCompositeOperation = "destination-in";
      b.drawImage(mask.c, bx * dpr, by * dpr, bw * dpr, bh * dpr, bx, by, bw, bh);
      b.drawImage(M.c, bb.x0, bb.y0, bb.w, bb.h, sx, sy, sw, sh);
      b.restore();
      ctx.drawImage(B.c, bx * dpr, by * dpr, bw * dpr, bh * dpr, bx, by, bw, bh);
    }

    /* ---------- driver ---------- */
    function ghostPos(now) {
      var t = now * .0006, topR = tp.offsetHeight + 30, aH = H - topR - low.offsetHeight - 30;
      return { x: W * (.5 + .36 * Math.sin(t * 1.15)), y: topR + aH * (.5 + .3 * Math.sin(t * 1.9 + 1.2)) };
    }
    function spawnDrip(now, x, y) { if (drips.length < 5) drips.push({ x: x + (rnd() - .5) * R * .9, y: y + R * .35, vy: .05 + rnd() * .06, r: 9 + rnd() * R * .09, t: now, life: 700 + rnd() * 600, acc: 0 }); }
    function step(now) {
      raf = 0;
      if (!ready || !visible) { running = false; return; }
      var dt = clamp(now - (lastT || now - 16), 1, 50); lastT = now;
      var auto = !reduce && !inside && !userUsed, active = inside || auto;
      if (auto) { var g = ghostPos(now); if (!stamps.length && headR < 1) { cur.x = last.x = g.x; cur.y = last.y = g.y; } tgt.x = g.x; tgt.y = g.y; }
      headR += ((active ? R * .9 : 0) - headR) * (1 - Math.exp(-dt / (active ? 90 : 160)));
      var spd = 0;
      if (active) {
        var kf = 1 - Math.exp(-dt / (inside ? 55 : 150)); cur.x += (tgt.x - cur.x) * kf; cur.y += (tgt.y - cur.y) * kf;
        var dx = cur.x - last.x, dy = cur.y - last.y, dist = Math.sqrt(dx * dx + dy * dy), gap = R * .16; spd = dist / dt;
        if (dist > gap) { var n = Math.min(24, Math.floor(dist / gap)); for (var j = 1; j <= n; j++) { var q = j / n; addStamp(last.x + dx * q, last.y + dy * q, now, R * (.8 + rnd() * .3), LIFE, false); } last.x = cur.x; last.y = cur.y; }
        if (now - lastDrip > (spd > 1 ? 110 : 1100)) { lastDrip = now; if (spd > .35 || inside) spawnDrip(now, cur.x, cur.y); }
      }
      for (var i = drips.length - 1; i >= 0; i--) { var d = drips[i], age = now - d.t;
        if (age > d.life) { drips.splice(i, 1); continue; }
        d.vy += .00012 * dt; var mv = d.vy * dt; d.y += mv; d.acc += mv;
        if (d.acc > d.r * .3) { d.acc = 0; addStamp(d.x, d.y, now, d.r * (1 - age / d.life * .4), 600, true); } }
      render(now);
      if (stamps.length || drips.length || headR > .8 || auto) { running = true; raf = requestAnimationFrame(step); } else { running = false; lastT = 0; }
    }
    function kick() { if (!running && !raf) { running = true; raf = requestAnimationFrame(step); } }

    function setPtr(e) {
      var r = fx.getBoundingClientRect(); tgt.x = e.clientX - r.left; tgt.y = e.clientY - r.top;
      if (!inside) { cur.x = last.x = tgt.x; cur.y = last.y = tgt.y; }    /* enter where the cursor IS: no streak from the demo */
      inside = true; userUsed = true; kick();
    }
    fx.addEventListener("pointermove", setPtr, { passive: true });
    fx.addEventListener("pointerdown", setPtr, { passive: true });
    function out() { inside = false; kick(); }
    fx.addEventListener("pointerleave", out); fx.addEventListener("pointercancel", out);

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
