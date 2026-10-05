/* FINALE — interactive closing hero, built on the noth.in idea:
   one giant word on a light page. Your cursor drags a liquid blob through it; the blob is a window onto a
   different layer underneath (a drifting field of seats being taken). Where the blob crosses the letters, the
   letters flip to that layer too. Presentation only: reads nothing from app state, writes nothing to it.
   One <canvas>, redrawn only while something is moving. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var coarse = window.matchMedia && matchMedia("(pointer:coarse)").matches;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var WORD = "ONCE.";

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;

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
    var C = { ink: tok("--fx-ink", "#07080a"), ivory: tok("--fx-ivory", "#f3f1ec"), acc: tok("--fx-accent", "#3d8bff"), hi: tok("--fx-accent-hi", "#9fceff") };
    var FONT = '"Montserrat","Schibsted Grotesk",Arial Black,system-ui,sans-serif', MONO = '"JetBrains Mono",ui-monospace,Menlo,monospace';

    function mk() { var c = document.createElement("canvas"); return { c: c, x: c.getContext("2d") }; }
    var txt = mk(), mask = mk(), blob = mk(), A = mk(), B = mk(), patD = mk(), patL = mk();
    var W = 0, H = 0, dpr = 1, maxW = 140, patW = 1, patH = 1, pD = null, pL = null;

    var pts = [], LIFE = 900, headR = 0;
    var cur = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, inside = false, lastMove = 0, lastPush = { x: 0, y: 0 }, userUsed = false;
    var mode = "", visible = false, running = false, raf = 0, ready = false, T0 = performance.now();

    function rrect(c, x, y, w, h, r) { c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

    /* the layer under the blob: a field of seats, some free, some being taken */
    function makePattern(target, onLetters) {
      var cell = clamp(W / 20, 46, 84), cols = 12, rows = 8, g = Math.max(3, cell * .07), s = cell - g * 2;
      patW = cols * cell; patH = rows * cell;
      target.c.width = Math.round(patW * dpr); target.c.height = Math.round(patH * dpr);
      var x = target.x; x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, patW, patH);
      var seed = 7, rnd = function () { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
      x.textAlign = "center"; x.textBaseline = "middle"; x.font = "600 " + Math.round(clamp(cell * .21, 10, 15)) + "px " + MONO;
      for (var j = 0; j < rows; j++) for (var i = 0; i < cols; i++) {
        var px = i * cell + g, py = j * cell + g, r = rnd(), id = String.fromCharCode(65 + j) + (i + 1);
        x.beginPath(); rrect(x, px, py, s, s, s * .16);
        if (r < .42) { /* free */
          if (!onLetters) { x.lineWidth = 1; x.strokeStyle = "rgba(243,241,236,.16)"; x.stroke(); }
        } else if (r < .8) { /* held / being taken: signal blue */
          x.fillStyle = onLetters ? C.acc : "rgba(61,139,255,.92)"; x.fill();
          x.fillStyle = "#fff"; x.fillText(id, px + s / 2, py + s / 2 + .5);
        } else if (r < .93) { /* committed: ivory */
          x.fillStyle = onLetters ? C.ink : C.ivory; x.fill();
          x.fillStyle = onLetters ? C.ivory : C.ink; x.fillText(id, px + s / 2, py + s / 2 + .5);
        } else { /* duplicate refused */
          x.lineWidth = 1.6; x.strokeStyle = onLetters ? C.ink : C.ivory; x.stroke();
          x.fillStyle = onLetters ? C.ink : C.ivory; x.fillText("409", px + s / 2, py + s / 2 + .5);
        }
      }
    }

    function build() {
      var r = fx.getBoundingClientRect(); W = Math.round(r.width); H = Math.round(r.height);
      if (W < 50 || H < 50) return;
      dpr = Math.min(window.devicePixelRatio || 1, W * H > 1.6e6 ? 1.5 : 2);
      [cv, txt.c, mask.c, blob.c, A.c, B.c].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
      [txt.x, mask.x, blob.x, A.x, B.x, ctx].forEach(function (x) { x.setTransform(dpr, 0, 0, dpr, 0, 0); });
      maxW = clamp(W * .12, 90, 230);
      makePattern(patD, false); makePattern(patL, true);
      pD = A.x.createPattern(patD.c, "repeat"); pL = B.x.createPattern(patL.c, "repeat");

      /* giant word: as wide as the page allows, centred in the room left by the UI */
      var x = txt.x; x.font = "900 100px " + FONT;
      var em = x.measureText(WORD).width / 100, m = x.measureText("O"), capEm = ((m.actualBoundingBoxAscent || 72) + (m.actualBoundingBoxDescent || 0)) / 100;
      var topR = top.offsetHeight + 30, lowR = low.offsetHeight + 30, padX = clamp(W * .012, 8, 24);
      var fs = Math.min((W - padX * 2) / em, (H - topR - lowR) / capEm * 1.02);
      var cy = topR + (H - topR - lowR) / 2 + (W < 760 ? 0 : H * .03), base = cy + (m.actualBoundingBoxAscent / 100 * fs - capEm * fs / 2);
      [txt.x, mask.x].forEach(function (c) { c.clearRect(0, 0, W, H); c.font = "900 " + fs + "px " + FONT; c.textAlign = "center"; c.textBaseline = "alphabetic"; });
      txt.x.fillStyle = C.ink; txt.x.fillText(WORD, W / 2, base);
      mask.x.fillStyle = "#fff"; mask.x.fillText(WORD, W / 2, base);

      cur.x = tgt.x = clamp(cur.x || W / 2, 0, W); cur.y = tgt.y = clamp(cur.y || H / 2, 0, H);
      ready = true; fx.classList.add("fx-ready"); kick();
    }

    function drawBlob(now) {
      var b = blob.x; b.clearRect(0, 0, W, H);
      while (pts.length && now - pts[0].t > LIFE) pts.shift();
      var n = pts.length, i;
      if (!n && headR < .6) return false;
      b.lineCap = b.lineJoin = "round"; b.strokeStyle = b.fillStyle = "#fff";
      for (i = 1; i < n; i++) {
        if (pts[i].brk) continue;                       /* never join two separate strokes */
        var f = 1 - (now - (pts[i].t + pts[i - 1].t) / 2) / LIFE; if (f <= 0) continue;
        b.lineWidth = maxW * Math.pow(f, .85) * (1 + .08 * Math.sin(now * .005 + i * .8));
        b.beginPath(); b.moveTo(pts[i - 1].x, pts[i - 1].y); b.lineTo(pts[i].x, pts[i].y); b.stroke();
      }
      if (headR > .6) { b.beginPath(); b.arc(cur.x, cur.y, headR * (1 + .06 * Math.sin(now * .007)), 0, 6.2832); b.fill(); }
      return true;
    }
    function fillPat(t, p, ox, oy) {
      var x = t.x; x.save(); x.globalCompositeOperation = "source-over";
      if (p.setTransform && window.DOMMatrix) p.setTransform(new DOMMatrix().translate(ox, oy).scale(1 / dpr));
      x.fillStyle = p; x.fillRect(0, 0, W, H); x.restore();
    }
    function render(now) {
      var any = drawBlob(now);
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = C.ivory; ctx.fillRect(0, 0, W, H);
      ctx.drawImage(txt.c, 0, 0, W, H);                                   /* the giant word, ink on ivory */
      if (!any) return;
      var t = reduce ? 0 : now - T0, ox = -(t * .03) + (W / 2 - cur.x) * .12, oy = -(t * .012) + (H / 2 - cur.y) * .12;
      /* blob = ink liquid showing the seat field */
      var a = A.x; a.globalCompositeOperation = "source-over"; a.fillStyle = C.ink; a.fillRect(0, 0, W, H);
      fillPat(A, pD, ox, oy);
      a.globalCompositeOperation = "destination-in"; a.drawImage(blob.c, 0, 0, W, H); a.globalCompositeOperation = "source-over";
      ctx.drawImage(A.c, 0, 0, W, H);
      /* letters inside the blob flip to ivory and carry the same field, offset a little for depth */
      var b = B.x; b.globalCompositeOperation = "source-over"; b.fillStyle = C.ivory; b.fillRect(0, 0, W, H);
      b.globalCompositeOperation = "source-atop"; fillPat(B, pL, ox, oy); b.globalCompositeOperation = "source-over";
      b.globalCompositeOperation = "destination-in"; b.drawImage(mask.c, 0, 0, W, H); b.drawImage(blob.c, 0, 0, W, H); b.globalCompositeOperation = "source-over";
      ctx.drawImage(B.c, 0, 0, W, H);
    }

    function ghostPos(now) {
      var t = now * .0006, topR = top.offsetHeight + 30, aH = H - topR - low.offsetHeight - 30;
      return { x: W * (.5 + .36 * Math.sin(t * 1.15)), y: topR + aH * (.5 + .3 * Math.sin(t * 1.9 + 1.2)) };
    }
    function switchTo(m, x, y) { /* any change of driver = a clean break: no old trail, no streak between two positions */
      if (mode === m) return; mode = m; pts = []; cur.x = tgt.x = x; cur.y = tgt.y = y; lastPush.x = x; lastPush.y = y; headR = 0;
    }
    function step(now) {
      raf = 0;
      if (!ready || !visible) { running = false; return; }
      var real = inside, auto = !reduce && !real && !userUsed && now - lastMove > 400;
      if (real) { if (mode !== "real") switchTo("real", tgt.x, tgt.y); }
      else if (auto) { var gp = ghostPos(now); if (mode !== "auto") switchTo("auto", gp.x, gp.y); tgt.x = gp.x; tgt.y = gp.y; }
      else if (mode) { mode = ""; }
      var active = real || auto;
      headR += ((active ? maxW * .5 : 0) - headR) * (active ? .18 : .12);
      if (active) {
        cur.x += (tgt.x - cur.x) * (real ? .42 : .2); cur.y += (tgt.y - cur.y) * (real ? .42 : .2);
        var dx = cur.x - lastPush.x, dy = cur.y - lastPush.y;
        if (dx * dx + dy * dy > 16) { pts.push({ x: cur.x, y: cur.y, t: now, brk: dx * dx + dy * dy > maxW * maxW * 4 }); if (pts.length > 140) pts.shift(); lastPush.x = cur.x; lastPush.y = cur.y; }
      }
      render(now);
      if (pts.length || headR > .6 || auto) { running = true; raf = requestAnimationFrame(step); } else { running = false; }
    }
    function kick() { if (!running && !raf) { running = true; raf = requestAnimationFrame(step); } }

    function setPtr(e) {
      var r = fx.getBoundingClientRect(); tgt.x = e.clientX - r.left; tgt.y = e.clientY - r.top;
      if (!inside) { switchTo("real", tgt.x, tgt.y); }           /* snap to the real cursor and drop the auto-pilot's trail */
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
    var fonts = (document.fonts && document.fonts.load) ? Promise.all([document.fonts.load('900 100px "Montserrat"'), document.fonts.load('600 12px "JetBrains Mono"'), document.fonts.load('500 20px "Schibsted Grotesk"')]).catch(function () {}) : Promise.resolve();
    fonts.then(function () { var r = fx.getBoundingClientRect(); lw = Math.round(r.width); lh = Math.round(r.height); build(); render(performance.now()); });
    setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 60);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
