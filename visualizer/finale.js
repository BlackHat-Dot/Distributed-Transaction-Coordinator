/* FINALE — interactive closing hero ("Ask a thousand times. Booked exactly once.")
   Presentation only: reads nothing from app state, writes nothing to it.

   Idea (inspired by the cursor-reveal on noth.in): the headline is giant, solid type over a faint field of seats.
   Your cursor drags a liquid blob through it. Where the blob crosses the LETTERS they switch to a live
   backend log (the saga's Redis / PostgreSQL / idempotency lines). Where it crosses SEATS it books them,
   and crossing a booked seat again is refused (409), so the page demonstrates "booked exactly once" itself.

   One <canvas>, redrawn only while something is moving. Idle = zero CPU. Touch devices and idle desktops
   get a gentle auto-pilot cursor so the effect is always discoverable. Respects prefers-reduced-motion. */
(function () {
  "use strict";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var coarse = window.matchMedia && matchMedia("(pointer:coarse)").matches;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };

  var LOG = ["GET /seats/B7 \u2192 200 AVAILABLE", "SET seat:B7:lock NX \u2192 OK", "SELECT \u2026 FOR UPDATE \u2192 row locked",
    "POST /pay key=7c1e80fc", "UNIQUE(tx_id) \u2192 1 row", "UPDATE seat SET status=BOOKED", "EVAL release.lua \u2192 lock released",
    "SagaEvent COMPLETED", "409 DUPLICATE REFUSED", "idempotency hit \u00B7 replay ignored", "mutex won \u00B7 others turned away",
    "CHARGED $500.00 \u00B7 once", "compensation not needed", "celery beat sweep \u2192 0 orphans"];

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;

    var fx = document.createElement("div");
    fx.className = "fx"; fx.id = "fx-finale";
    fx.innerHTML =
      '<canvas class="fx-cv" aria-hidden="true"></canvas>' +
      '<div class="fx-ui">' +
        '<div class="fx-top"><p class="fx-eyebrow"><i></i>Tour complete</p>' +
          '<p class="fx-meter" aria-hidden="true"><span class="fx-hint">' + (coarse ? "Drag across the text" : "Move your cursor across the text") + '</span></p></div>' +
        '<h2 class="fx-h">Ask a thousand times. Booked exactly once.</h2>' +
        '<div class="fx-low"><p class="fx-sub">Double clicks, retries, races and crashes can\u2019t double-book a seat or double-charge a card. Every failure rolls itself back. Proven live, not just promised.</p>' +
          '<div class="fx-cta"><a class="btn btn-primary" href="booking.html">Book a seat \u2794</a><button class="btn btn-outline fx-replay" type="button">Replay tour \u21BA</button></div></div>' +
      '</div>';
    footer.insertBefore(fx, footer.firstChild);

    var replay = fx.querySelector(".fx-replay");
    replay.addEventListener("click", function () { if (window.startScrollTour) window.startScrollTour(); });

    var cv = fx.querySelector(".fx-cv"), ctx = cv.getContext("2d");
    if (!ctx) return;
    var meter = fx.querySelector(".fx-meter"), top = fx.querySelector(".fx-top"), low = fx.querySelector(".fx-low");
    var cs = getComputedStyle(fx);
    var tok = function (n, d) { return (cs.getPropertyValue(n) || "").trim() || d; };
    var C = { ink: tok("--fx-ink", "#07080a"), ivory: tok("--fx-ivory", "#f3f1ec"), acc: tok("--fx-accent", "#3d8bff"), hi: tok("--fx-accent-hi", "#9fceff"), rgb: tok("--fx-accent-rgb", "61,139,255") };

    function mk() { var c = document.createElement("canvas"); return { c: c, x: c.getContext("2d") }; }
    var field = mk(), txt = mk(), mask = mk(), alt = mk(), blob = mk(), strip = mk();
    var W = 0, H = 0, dpr = 1, cell = 48, cols = 0, rows = 0, lines = [], fs = 100, lfs = 12, lh = 18, stripH = 1;
    var FONT = '"Big Shoulders Display","Schibsted Grotesk",Impact,system-ui,sans-serif';
    var MONO = '"JetBrains Mono",ui-monospace,Menlo,monospace';

    /* ---------- state ---------- */
    var pts = [], LIFE = 1050, headR = 0, maxW = 110;
    var cur = { x: 0, y: 0 }, tgt = { x: 0, y: 0 }, hasPointer = false, inside = false, lastMove = 0, lastPush = { x: -99, y: -99 };
    var live = [], bk = {}, booked = 0, dups = 0, tag = null, seenFirst = false;
    var visible = false, running = false, raf = 0, ghostOn = false, T0 = performance.now(), ready = false;

    /* ---------- layout ---------- */
    function build() {
      var r = fx.getBoundingClientRect(); W = Math.round(r.width); H = Math.round(r.height);
      if (W < 50 || H < 50) return;
      dpr = Math.min(window.devicePixelRatio || 1, W * H > 1.6e6 ? 1.5 : 2);
      [cv, field.c, txt.c, mask.c, alt.c, blob.c].forEach(function (c) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); });
      [field.x, txt.x, mask.x, alt.x, blob.x].forEach(function (x) { x.setTransform(dpr, 0, 0, dpr, 0, 0); });
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      maxW = clamp(W * .115, 84, 200);

      /* seat field */
      cols = Math.max(8, Math.round(W / (W < 760 ? 40 : 58))); cell = W / cols; rows = Math.ceil(H / cell);
      var f = field.x, g = 5, rr = Math.min(8, cell * .16), i, j;
      f.clearRect(0, 0, W, H); f.lineWidth = 1; f.strokeStyle = "rgba(255,255,255,.07)"; f.fillStyle = "rgba(255,255,255,.012)";
      for (j = 0; j < rows; j++) for (i = 0; i < cols; i++) {
        f.beginPath(); rrect(f, i * cell + g, j * cell + g, cell - g * 2, cell - g * 2, rr); f.fill(); f.stroke();
      }

      /* headline lines + size */
      var narrow = W < 760;
      lines = narrow ? ["ASK A", "THOUSAND", "TIMES.", "BOOKED", "EXACTLY", "ONCE."] : ["ASK A THOUSAND TIMES.", "BOOKED EXACTLY ONCE."];
      var half = lines.length / 2, x = txt.x;
      x.font = "900 100px " + FONT;
      var widest = 0; lines.forEach(function (l) { widest = Math.max(widest, x.measureText(l).width / 100); });
      var capH100 = (function () { var m = x.measureText("H"); return (m.actualBoundingBoxAscent || 72) / 100; })();
      var padX = clamp(W * .04, 18, 72), topR = top.offsetHeight + 36, lowR = low.offsetHeight + 36;
      var avail = Math.max(120, H - topR - lowR - 24 - (narrow ? 0 : 0)), LH = .92;
      var fsW = (W - padX * 2) / widest;
      var fsH = avail / ((lines.length - 1) * LH + capH100);
      fs = Math.max(24, Math.min(fsW, fsH));
      var blockH = (lines.length - 1) * LH * fs + capH100 * fs, y0 = topR + 12 + (avail - blockH) / 2 + capH100 * fs;

      [txt.x, mask.x].forEach(function (c) { c.clearRect(0, 0, W, H); c.font = "900 " + fs + "px " + FONT; c.textAlign = "center"; c.textBaseline = "alphabetic"; c.lineJoin = "round"; });
      lines.forEach(function (l, n) {
        var y = y0 + n * LH * fs, solid = n < half;
        mask.x.fillStyle = "#fff"; mask.x.fillText(l, W / 2, y);
        if (solid) {
          var gr = txt.x.createLinearGradient(0, y - capH100 * fs, 0, y);
          gr.addColorStop(0, C.ivory); gr.addColorStop(1, "#c4c8ce");
          txt.x.fillStyle = gr; txt.x.fillText(l, W / 2, y);
        } else {
          txt.x.fillStyle = "rgba(243,241,236,.05)"; txt.x.fillText(l, W / 2, y);
          txt.x.lineWidth = Math.max(1.5, fs * .011); txt.x.strokeStyle = "rgba(243,241,236,.92)"; txt.x.strokeText(l, W / 2, y);
        }
      });

      /* scrolling log texture used inside the letters */
      lfs = clamp(W / 95, 11, 15); lh = Math.round(lfs * 1.7);
      var nRows = Math.ceil(H / lh) + 2; stripH = nRows * lh;
      strip.c.width = Math.round(W * dpr); strip.c.height = Math.round(stripH * dpr);
      var s = strip.x; s.setTransform(dpr, 0, 0, dpr, 0, 0); s.clearRect(0, 0, W, stripH);
      s.font = "600 " + lfs + "px " + MONO; s.textBaseline = "middle"; s.fillStyle = "rgba(3,12,28,.78)";
      for (j = 0; j < nRows; j++) {
        var line = "", k = (j * 5) % LOG.length;
        while (s.measureText(line).width < W + 200) { line += LOG[k % LOG.length] + "   \u00B7   "; k += 3; }
        s.fillText(line, -((j * 97) % 260), j * lh + lh / 2);
      }

      cur.x = tgt.x = clamp(cur.x || W / 2, 0, W); cur.y = tgt.y = clamp(cur.y || H / 2, 0, H);
      ready = true; fx.classList.add("fx-ready");
      kick();
    }
    function rrect(c, x, y, w, h, r) { c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

    /* ---------- seats ---------- */
    var BOOK_MS = 3300;
    function seatId(i, j) { return String.fromCharCode(65 + j % 26) + (i + 1); }
    function bookAround(px, py, now) {
      var R = Math.max(headR, maxW * .4) * .78, i0 = Math.max(0, Math.floor((px - R) / cell)), i1 = Math.min(cols - 1, Math.floor((px + R) / cell));
      var j0 = Math.max(0, Math.floor((py - R) / cell)), j1 = Math.min(rows - 1, Math.floor((py + R) / cell)), i, j, changed = false;
      for (j = j0; j <= j1; j++) for (i = i0; i <= i1; i++) {
        var cx = (i + .5) * cell, cy = (j + .5) * cell;
        if ((cx - px) * (cx - px) + (cy - py) * (cy - py) > R * R) continue;
        var id = j * cols + i, b = bk[id];
        if (!b || now - b.t0 > BOOK_MS) {
          if (!b) live.push(id);
          bk[id] = { t0: now, dupT: 0 }; booked++; changed = true; tag = { i: i, j: j, t: now, dup: false };
        } else if (now - b.t0 > 380 && now - b.dupT > 520) {
          b.dupT = now; dups++; changed = true; tag = { i: i, j: j, t: now, dup: true };
        }
      }
      if (changed) updMeter();
    }
    function updMeter() {
      meter.innerHTML = 'Seats booked <b>' + booked + '</b><span class="sep"></span>Duplicates refused <b>' + dups + '</b>';
    }
    function drawSeats(now) {
      var n = live.length; if (!n) return;
      for (var k = n - 1; k >= 0; k--) {
        var id = live[k], b = bk[id], age = now - b.t0;
        if (age > BOOK_MS) { live.splice(k, 1); delete bk[id]; continue; }
        var a = Math.min(1, age / 140) * (age > BOOK_MS - 800 ? (BOOK_MS - age) / 800 : 1);
        var i = id % cols, j = (id / cols) | 0, g = 5, x = i * cell + g, y = j * cell + g, s = cell - g * 2, flash = now - b.dupT < 480 && b.dupT;
        ctx.beginPath(); rrect(ctx, x, y, s, s, Math.min(8, cell * .16));
        ctx.fillStyle = "rgba(" + C.rgb + "," + (.2 * a).toFixed(3) + ")"; ctx.fill();
        ctx.lineWidth = flash ? 1.6 : 1; ctx.strokeStyle = flash ? "rgba(243,241,236," + (.9 * a).toFixed(3) + ")" : "rgba(159,206,255," + (.7 * a).toFixed(3) + ")"; ctx.stroke();
        if (cell > 34) {
          ctx.font = "600 " + Math.round(clamp(cell * .2, 9, 12)) + "px " + MONO; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillStyle = flash ? "rgba(243,241,236," + a.toFixed(3) + ")" : "rgba(159,206,255," + a.toFixed(3) + ")";
          ctx.fillText(flash ? "409" : seatId(i, j), x + s / 2, y + s / 2 + .5);
        }
      }
    }

    /* ---------- liquid blob ---------- */
    function drawBlob(now) {
      var b = blob.x; b.clearRect(0, 0, W, H);
      while (pts.length && now - pts[0].t > LIFE) pts.shift();
      var n = pts.length, i;
      if (!n && headR < .6) return false;
      b.lineCap = b.lineJoin = "round"; b.strokeStyle = b.fillStyle = "#fff";
      for (i = 1; i < n; i++) {
        var f = 1 - (now - (pts[i].t + pts[i - 1].t) / 2) / LIFE; if (f <= 0) continue;
        b.lineWidth = maxW * Math.pow(f, .9) * (1 + .07 * Math.sin(now * .006 + i * .9));
        b.beginPath(); b.moveTo(pts[i - 1].x, pts[i - 1].y); b.lineTo(pts[i].x, pts[i].y); b.stroke();
      }
      if (headR > .6) { b.beginPath(); b.arc(cur.x, cur.y, headR * (1 + .05 * Math.sin(now * .008)), 0, 6.2832); b.fill(); }
      return true;
    }

    /* ---------- frame ---------- */
    function render(now) {
      var any = drawBlob(now), x = alt.x;
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(field.c, 0, 0, W, H);
      if (any) { /* soft aura where the blob passes over the field */
        x.globalCompositeOperation = "source-over"; x.clearRect(0, 0, W, H); x.fillStyle = "rgba(" + C.rgb + ",1)"; x.fillRect(0, 0, W, H);
        x.globalCompositeOperation = "destination-in"; x.drawImage(blob.c, 0, 0, W, H);
        ctx.globalAlpha = .09; ctx.drawImage(alt.c, 0, 0, W, H); ctx.globalAlpha = 1;
      }
      drawSeats(now);
      ctx.drawImage(txt.c, 0, 0, W, H);
      if (any) { /* letters under the blob: live backend log on a signal-blue fill */
        x.globalCompositeOperation = "source-over"; x.clearRect(0, 0, W, H);
        var g = x.createLinearGradient(0, 0, W, H); g.addColorStop(0, C.hi); g.addColorStop(1, C.acc);
        x.fillStyle = g; x.fillRect(0, 0, W, H);
        var off = reduce ? 0 : ((now - T0) * .045) % stripH;
        x.drawImage(strip.c, 0, off - stripH, W, stripH); x.drawImage(strip.c, 0, off, W, stripH);
        x.globalCompositeOperation = "destination-in"; x.drawImage(mask.c, 0, 0, W, H); x.drawImage(blob.c, 0, 0, W, H);
        x.globalCompositeOperation = "source-over";
        ctx.drawImage(alt.c, 0, 0, W, H);
      }
      drawTag(now);
    }
    function drawTag(now) {
      if (!tag || now - tag.t > 900 || headR < 8) return;
      var a = 1 - Math.max(0, (now - tag.t - 600) / 300), txtS = tag.dup ? "409 \u00B7 ALREADY BOOKED" : seatId(tag.i, tag.j) + " \u00B7 BOOKED";
      ctx.font = "600 11px " + MONO; var w = ctx.measureText(txtS).width + 18, h = 24;
      var x = clamp(cur.x + headR * .7 + 10, 8, W - w - 8), y = clamp(cur.y - headR * .7 - h - 6, 8, H - h - 8);
      ctx.globalAlpha = a; ctx.beginPath(); rrect(ctx, x, y, w, h, 12); ctx.fillStyle = tag.dup ? "rgba(12,14,17,.92)" : C.ivory; ctx.fill();
      if (tag.dup) { ctx.lineWidth = 1; ctx.strokeStyle = "rgba(243,241,236,.5)"; ctx.stroke(); }
      ctx.fillStyle = tag.dup ? C.ivory : "#07080a"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(txtS, x + 9, y + h / 2 + .5); ctx.globalAlpha = 1;
    }

    /* ---------- loop: runs only while something is moving ---------- */
    function ghostPos(now) {
      var t = now * .00055, topR = top.offsetHeight + 40, aH = H - topR - low.offsetHeight - 40;
      return { x: W * (.5 + .4 * Math.sin(t * 1.15)), y: topR + aH * (.5 + .36 * Math.sin(t * 1.9 + 1.2)) };
    }
    function step(now) {
      raf = 0;
      if (!ready || !visible) { running = false; return; }
      var wantGhost = !reduce && !(inside && hasPointer) && now - lastMove > (coarse ? 600 : 1500);
      if (wantGhost) { ghostOn = true; var gp = ghostPos(now); tgt.x = gp.x; tgt.y = gp.y; } else ghostOn = false;
      var live_ = (inside && hasPointer) || ghostOn;
      headR += ((live_ ? maxW * .46 : 0) - headR) * (live_ ? .16 : .12);
      if (live_) {
        cur.x += (tgt.x - cur.x) * .38; cur.y += (tgt.y - cur.y) * .38;
        var dx = cur.x - lastPush.x, dy = cur.y - lastPush.y;
        if (dx * dx + dy * dy > 16) { pts.push({ x: cur.x, y: cur.y, t: now }); if (pts.length > 140) pts.shift(); lastPush.x = cur.x; lastPush.y = cur.y; bookAround(cur.x, cur.y, now); }
      }
      render(now);
      if (pts.length || headR > .6 || live.length || ghostOn) { running = true; raf = requestAnimationFrame(step); } else { running = false; render(now); }
    }
    function kick() { if (!running && !raf) { running = true; raf = requestAnimationFrame(step); } }

    /* ---------- input ---------- */
    function setPtr(e) {
      var r = fx.getBoundingClientRect(); tgt.x = e.clientX - r.left; tgt.y = e.clientY - r.top;
      if (!hasPointer || !inside) { cur.x = tgt.x; cur.y = tgt.y; lastPush.x = tgt.x; lastPush.y = tgt.y; }
      hasPointer = true; inside = true; lastMove = performance.now();
      if (!seenFirst) { seenFirst = true; updMeter(); }
      kick();
    }
    fx.addEventListener("pointermove", setPtr, { passive: true });
    fx.addEventListener("pointerdown", setPtr, { passive: true });
    fx.addEventListener("pointerleave", function () { inside = false; lastMove = performance.now(); kick(); });
    fx.addEventListener("pointercancel", function () { inside = false; lastMove = performance.now(); kick(); });

    /* ---------- lifecycle ---------- */
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (en) { visible = en[0].isIntersecting; if (visible) kick(); }, { threshold: 0 }).observe(fx);
    } else visible = true;
    document.addEventListener("visibilitychange", function () { if (!document.hidden) kick(); });

    var rt = 0, lastW = 0, lastH = 0;
    function onSize() {
      clearTimeout(rt);
      rt = setTimeout(function () { var r = fx.getBoundingClientRect(); if (Math.round(r.width) !== lastW || Math.round(r.height) !== lastH) { lastW = Math.round(r.width); lastH = Math.round(r.height); build(); } }, 120);
    }
    if ("ResizeObserver" in window) new ResizeObserver(onSize).observe(fx); else addEventListener("resize", onSize);

    var fontsReady = (document.fonts && document.fonts.load)
      ? Promise.all([document.fonts.load('900 64px "Big Shoulders Display"'), document.fonts.load('600 12px "JetBrains Mono"')]).catch(function () {})
      : Promise.resolve();
    fontsReady.then(function () { var r = fx.getBoundingClientRect(); lastW = Math.round(r.width); lastH = Math.round(r.height); build(); render(performance.now()); });

    /* the footer just got taller: tell scroll-tour.js to re-measure its footer hand-over */
    setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 60);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
