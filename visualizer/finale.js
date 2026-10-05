/* FINALE — interactive closing hero, built on the noth.in idea:
   a giant word on a light page. The cursor drags a blob of plain black liquid (ragged edge, drips).
   Only where the liquid crosses the LETTERS does a rich moving image show through them.
   Presentation only: reads nothing from app state, writes nothing to it. One <canvas>, redrawn only while moving.

   THE IMAGE inside the letters: by default a procedurally painted glass-bubble foam (no assets needed).
   To use your own photo or video instead (exactly like noth.in), set before this script loads:
       <script>window.FINALE_MEDIA = "foam.mp4";</script>      (.mp4 / .webm / .jpg / .png / .webp)
   or add data-media="foam.mp4" on the <footer class="site-footer">. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var coarse = window.matchMedia && matchMedia("(pointer:coarse)").matches;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var WORD = "ONCE.";

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
    var top = fx.querySelector(".fx-top"), low = fx.querySelector(".fx-low");
    var cs = getComputedStyle(fx), tok = function (n, d) { return (cs.getPropertyValue(n) || "").trim() || d; };
    var C = { ink: tok("--fx-ink", "#07080a"), ivory: tok("--fx-ivory", "#f3f1ec") };
    var FONT = '"Montserrat","Schibsted Grotesk",Arial Black,system-ui,sans-serif';

    function mk() { var c = document.createElement("canvas"); return { c: c, x: c.getContext("2d") }; }
    var txt = mk(), mask = mk(), blob = mk(), A = mk(), B = mk(), foamA = mk(), foamB = mk();
    var W = 0, H = 0, dpr = 1, maxW = 160, S1 = 640, S2 = 420, pA = null, pB = null;

    var pts = [], drops = [], LIFE = 1300, headR = 0;
    var cur = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, inside = false, lastMove = 0, lastPush = { x: 0, y: 0 }, userUsed = false, lastDrop = 0;
    var mode = "", visible = false, running = false, raf = 0, ready = false, T0 = performance.now(), seed = 11;
    var rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };

    /* ---------- the image inside the letters ---------- */
    var vid = null, img = null, mediaOK = false;
    if (MEDIA) {
      if (/\.(mp4|webm|mov|m4v)(\?|$)/i.test(MEDIA)) {
        vid = document.createElement("video"); vid.src = MEDIA; vid.muted = true; vid.loop = true; vid.playsInline = true; vid.preload = "auto"; vid.setAttribute("muted", "");
        vid.addEventListener("loadeddata", function () { mediaOK = true; });
        vid.addEventListener("error", function () { mediaOK = false; });
      } else {
        img = new Image(); img.onload = function () { mediaOK = true; }; img.src = MEDIA;
      }
    }
    function drawMedia(x) {
      var el = vid || img, w = vid ? vid.videoWidth : img.naturalWidth, h = vid ? vid.videoHeight : img.naturalHeight;
      if (!w || !h) return false;
      var k = Math.max(W / w, H / h), dw = w * k, dh = h * k;
      x.drawImage(el, (W - dw) / 2, (H - dh) / 2, dw, dh); return true;
    }

    /* procedural glass-bubble foam: seamless tile, so it can scroll forever */
    function makeFoam(t, S, withBase, scale, count) {
      t.c.width = t.c.height = Math.round(S * dpr);
      var x = t.x; x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, S, S);
      if (withBase) {
        x.fillStyle = "#2b2824"; x.fillRect(0, 0, S, S);
        for (var q = 0; q < 70; q++) { /* soft cream foam masses */
          var bx = rnd() * S, by = rnd() * S, br = (40 + rnd() * 110) * scale;
          for (var ox = -S; ox <= S; ox += S) for (var oy = -S; oy <= S; oy += S) {
            var g = x.createRadialGradient(bx + ox, by + oy, 0, bx + ox, by + oy, br);
            g.addColorStop(0, "rgba(246,241,230,.95)"); g.addColorStop(.6, "rgba(226,219,205,.75)"); g.addColorStop(1, "rgba(226,219,205,0)");
            x.fillStyle = g; x.fillRect(bx + ox - br, by + oy - br, br * 2, br * 2);
          }
        }
      }
      var list = [], i;
      for (i = 0; i < count; i++) list.push({ x: rnd() * S, y: rnd() * S, r: (2.5 + 44 * Math.pow(rnd(), 3.1)) * scale, tint: rnd() });
      list.sort(function (a, b) { return b.r - a.r; });
      list.forEach(function (b) {
        for (var ox = -S; ox <= S; ox += S) for (var oy = -S; oy <= S; oy += S) {
          var cx = b.x + ox, cy = b.y + oy, r = b.r;
          if (cx + r < 0 || cx - r > S || cy + r < 0 || cy - r > S) continue;
          var body = x.createRadialGradient(cx - r * .2, cy - r * .25, r * .05, cx, cy, r);
          body.addColorStop(0, "rgba(250,246,238,.10)"); body.addColorStop(.62, "rgba(235,228,214,.16)");
          body.addColorStop(.86, "rgba(30,28,30,.50)"); body.addColorStop(.96, "rgba(255,255,255,.88)"); body.addColorStop(1, "rgba(255,255,255,0)");
          x.fillStyle = body; x.beginPath(); x.arc(cx, cy, r, 0, 6.2832); x.fill();
          if (b.tint > .72 && r > 5) { /* cool refraction in the lower half (palette: one blue accent) */
            var cg = x.createRadialGradient(cx + r * .25, cy + r * .4, 0, cx + r * .25, cy + r * .4, r * .75);
            cg.addColorStop(0, "rgba(110,165,255,.55)"); cg.addColorStop(1, "rgba(110,165,255,0)");
            x.fillStyle = cg; x.beginPath(); x.arc(cx, cy, r * .93, 0, 6.2832); x.fill();
          }
          if (r > 4) { /* specular highlights */
            x.fillStyle = "rgba(255,255,255,.95)"; x.beginPath(); x.ellipse(cx - r * .38, cy - r * .42, r * .2, r * .11, -.6, 0, 6.2832); x.fill();
            x.fillStyle = "rgba(255,255,255,.35)"; x.beginPath(); x.arc(cx + r * .42, cy + r * .46, r * .09, 0, 6.2832); x.fill();
          }
        }
      });
    }

    function build() {
      var r = fx.getBoundingClientRect(); W = Math.round(r.width); H = Math.round(r.height);
      if (W < 50 || H < 50) return;
      dpr = Math.min(window.devicePixelRatio || 1, W * H > 1.6e6 ? 1.5 : 2);
      [cv, txt.c, mask.c, blob.c, A.c, B.c].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
      [txt.x, mask.x, blob.x, A.x, B.x, ctx].forEach(function (x) { x.setTransform(dpr, 0, 0, dpr, 0, 0); });
      maxW = clamp(W * .17, 110, 320);
      seed = 11; var sc = clamp(W / 1440, .7, 1.25);
      makeFoam(foamA, S1, true, sc, 1500); makeFoam(foamB, S2, false, sc * .55, 900);
      pA = B.x.createPattern(foamA.c, "repeat"); pB = B.x.createPattern(foamB.c, "repeat");

      var x = txt.x; x.font = "900 100px " + FONT;
      var em = x.measureText(WORD).width / 100, m = x.measureText("O"), asc = (m.actualBoundingBoxAscent || 72) / 100, capEm = asc + (m.actualBoundingBoxDescent || 0) / 100;
      var topR = top.offsetHeight + 30, lowR = low.offsetHeight + 30, padX = clamp(W * .012, 8, 24);
      var fs = Math.min((W - padX * 2) / em, (H - topR - lowR) / capEm * 1.02);
      var cy = topR + (H - topR - lowR) / 2 + (W < 760 ? 0 : H * .03), base = cy + (asc * fs - capEm * fs / 2);
      [txt.x, mask.x].forEach(function (c) { c.clearRect(0, 0, W, H); c.font = "900 " + fs + "px " + FONT; c.textAlign = "center"; c.textBaseline = "alphabetic"; });
      txt.x.fillStyle = C.ink; txt.x.fillText(WORD, W / 2, base);
      mask.x.fillStyle = "#fff"; mask.x.fillText(WORD, W / 2, base);

      cur.x = tgt.x = clamp(cur.x || W / 2, 0, W); cur.y = tgt.y = clamp(cur.y || H / 2, 0, H);
      ready = true; fx.classList.add("fx-ready"); kick();
    }

    /* ---------- the liquid: lumpy trail + drips ---------- */
    function drawBlob(now) {
      var b = blob.x; b.clearRect(0, 0, W, H);
      while (pts.length && now - pts[0].t > LIFE) pts.shift();
      for (var d = drops.length - 1; d >= 0; d--) if (now - drops[d].t > drops[d].life) drops.splice(d, 1);
      var n = pts.length, i;
      if (!n && headR < .6 && !drops.length) return false;
      b.lineCap = b.lineJoin = "round"; b.strokeStyle = b.fillStyle = "#fff";
      for (i = 1; i < n; i++) {
        if (pts[i].brk) continue;
        var f = 1 - (now - (pts[i].t + pts[i - 1].t) / 2) / LIFE; if (f <= 0) continue;
        var w = maxW * Math.pow(f, .8) * (.86 + .22 * Math.sin(now * .004 + i * .7));
        b.lineWidth = w; b.beginPath(); b.moveTo(pts[i - 1].x, pts[i - 1].y); b.lineTo(pts[i].x, pts[i].y); b.stroke();
        var p = pts[i]; /* lumps make the edge ragged instead of a smooth pipe */
        b.beginPath(); b.arc(p.x + p.ox * w, p.y + p.oy * w, w * p.k * .5, 0, 6.2832); b.fill();
      }
      if (headR > .6) { b.beginPath(); b.arc(cur.x, cur.y, headR * (1 + .07 * Math.sin(now * .007)), 0, 6.2832); b.fill(); }
      drops.forEach(function (o) {
        var a = (now - o.t) / o.life, dt = now - o.t;
        o.y = o.y0 + o.vy * dt + .0000011 * dt * dt * 1000; o.x = o.x0 + o.vx * dt;
        b.beginPath(); b.arc(o.x, o.y, o.r * (1 - a * .7), 0, 6.2832); b.fill();
      });
      return true;
    }
    function render(now) {
      var any = drawBlob(now);
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = C.ivory; ctx.fillRect(0, 0, W, H);
      ctx.drawImage(txt.c, 0, 0, W, H);                    /* ink word on ivory */
      if (!any) return;
      /* 1. the liquid itself: plain black, nothing inside it */
      var a = A.x; a.globalCompositeOperation = "source-over"; a.fillStyle = C.ink; a.fillRect(0, 0, W, H);
      a.globalCompositeOperation = "destination-in"; a.drawImage(blob.c, 0, 0, W, H); a.globalCompositeOperation = "source-over";
      ctx.drawImage(A.c, 0, 0, W, H);
      /* 2. the image, visible ONLY where liquid and letters overlap */
      var t = reduce ? 0 : now - T0, b = B.x; b.globalCompositeOperation = "source-over"; b.clearRect(0, 0, W, H);
      var drew = false;
      if (mediaOK) { if (vid && vid.paused) { var pr = vid.play(); if (pr && pr.catch) pr.catch(function () {}); } drew = drawMedia(b); }
      if (!drew) {
        var px = (W / 2 - cur.x) * .1, py = (H / 2 - cur.y) * .1;
        fill(b, pA, -t * .022 + px, -t * .012 + py); fill(b, pB, t * .03 - px * 1.6, -t * .02 - py * 1.6);
      }
      b.globalCompositeOperation = "destination-in"; b.drawImage(mask.c, 0, 0, W, H); b.drawImage(blob.c, 0, 0, W, H); b.globalCompositeOperation = "source-over";
      ctx.drawImage(B.c, 0, 0, W, H);
    }
    function fill(x, p, ox, oy) { if (p.setTransform && window.DOMMatrix) p.setTransform(new DOMMatrix().translate(ox, oy).scale(1 / dpr)); x.fillStyle = p; x.fillRect(0, 0, W, H); }

    /* ---------- driver: real cursor, or a demo that hands over cleanly ---------- */
    function ghostPos(now) {
      var t = now * .0006, topR = top.offsetHeight + 30, aH = H - topR - low.offsetHeight - 30;
      return { x: W * (.5 + .36 * Math.sin(t * 1.15)), y: topR + aH * (.5 + .3 * Math.sin(t * 1.9 + 1.2)) };
    }
    function switchTo(m, x, y) { if (mode === m) return; mode = m; pts = []; drops = []; cur.x = tgt.x = x; cur.y = tgt.y = y; lastPush.x = x; lastPush.y = y; headR = 0; }
    function step(now) {
      raf = 0;
      if (!ready || !visible) { running = false; return; }
      var real = inside, auto = !reduce && !real && !userUsed && now - lastMove > 400;
      if (real) { if (mode !== "real") switchTo("real", tgt.x, tgt.y); }
      else if (auto) { var gp = ghostPos(now); if (mode !== "auto") switchTo("auto", gp.x, gp.y); tgt.x = gp.x; tgt.y = gp.y; }
      else if (mode) mode = "";
      var active = real || auto;
      headR += ((active ? maxW * .5 : 0) - headR) * (active ? .18 : .12);
      if (active) {
        cur.x += (tgt.x - cur.x) * (real ? .4 : .2); cur.y += (tgt.y - cur.y) * (real ? .4 : .2);
        var dx = cur.x - lastPush.x, dy = cur.y - lastPush.y, d2 = dx * dx + dy * dy;
        if (d2 > 16) {
          pts.push({ x: cur.x, y: cur.y, t: now, brk: d2 > maxW * maxW * 4, ox: (rnd() - .5) * .5, oy: (rnd() - .5) * .5, k: .7 + rnd() * .6 });
          if (pts.length > 160) pts.shift(); lastPush.x = cur.x; lastPush.y = cur.y;
          if (d2 > 90 && now - lastDrop > 70 && drops.length < 24) { /* fast movement flicks off drips */
            lastDrop = now; drops.push({ x0: cur.x + (rnd() - .5) * maxW * .6, y0: cur.y + (rnd() - .2) * maxW * .4, x: 0, y: 0, vx: (rnd() - .5) * .03, vy: .01 + rnd() * .05, r: 3 + rnd() * maxW * .09, t: now, life: 700 + rnd() * 700 });
          }
        }
      }
      render(now);
      if (pts.length || drops.length || headR > .6 || auto) { running = true; raf = requestAnimationFrame(step); } else running = false;
    }
    function kick() { if (!running && !raf) { running = true; raf = requestAnimationFrame(step); } }

    function setPtr(e) {
      var r = fx.getBoundingClientRect(); tgt.x = e.clientX - r.left; tgt.y = e.clientY - r.top;
      if (!inside) switchTo("real", tgt.x, tgt.y);
      inside = true; userUsed = true; lastMove = performance.now(); kick();
    }
    fx.addEventListener("pointermove", setPtr, { passive: true });
    fx.addEventListener("pointerdown", setPtr, { passive: true });
    function out() { inside = false; mode = "real-out"; lastMove = performance.now(); kick(); }
    fx.addEventListener("pointerleave", out); fx.addEventListener("pointercancel", out);

    if ("IntersectionObserver" in window) new IntersectionObserver(function (en) { visible = en[0].isIntersecting; if (visible) kick(); }, { threshold: 0 }).observe(fx); else visible = true;
    document.addEventListener("visibilitychange", function () { if (!document.hidden) kick(); });
    var rt = 0, lw = 0, lh = 0;
    function onSize() { clearTimeout(rt); rt = setTimeout(function () { var r = fx.getBoundingClientRect(); if (Math.round(r.width) !== lw || Math.round(r.height) !== lh) { lw = Math.round(r.width); lh = Math.round(r.height); build(); render(performance.now()); } }, 120); }
    if ("ResizeObserver" in window) new ResizeObserver(onSize).observe(fx); else addEventListener("resize", onSize);
    var fonts = (document.fonts && document.fonts.load) ? Promise.all([document.fonts.load('900 100px "Montserrat"'), document.fonts.load('500 20px "Schibsted Grotesk"')]).catch(function () {}) : Promise.resolve();
    fonts.then(function () { var r = fx.getBoundingClientRect(); lw = Math.round(r.width); lh = Math.round(r.height); build(); render(performance.now()); });
    setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 60);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
