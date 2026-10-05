/* SEAT STORY: one seat followed through the saga while you scroll, then the TICKET it produced, then a REWIND that hands off to the guided tour.
   Presentation only: reads the first completed row of the live transaction table for a real id/amount, writes nothing to app state.
   Exposes SeatStory.endFrame() so scroll-tour.js can lift off this chapter's exact last screen.

   v1.4 EXIT ("the ticket"): everything is scrubbed by scroll, so it plays backwards too.
     1. After COMMITTED the story recedes (dims, sinks back) and the e-ticket for seat B7 is laid onto the stage.
     2. The ticket holds while a scan beam sweeps its QR and the stub flips to "Verified". That is the payoff of the whole chapter.
     3. The ticket lifts away, and REWIND rises into place. REWIND is the exact last frame handed to the tour.
   Every stage begins and ends at rest, so nothing appears or disappears abruptly. */
(function () {
  "use strict";
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var seg = function (x, a, b) { return clamp((x - a) / (b - a), 0, 1); };
  var ease = function (t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  /* decorative QR: deterministic from the transaction id (not a scannable code), with real finder patterns so it reads as one */
  function qrSvg(seed) {
    var n = 25, h = 2166136261, i, x, y, d = "";
    for (i = 0; i < seed.length; i++) { h ^= seed.charCodeAt(i); h = Math.imul(h, 16777619); }
    h = h | 0 || 1;
    var rnd = function () { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; return (h >>> 0) / 4294967296; };
    var ring = function (a, b) { return a === 0 || a === 6 || b === 0 || b === 6 || (a >= 2 && a <= 4 && b >= 2 && b <= 4); };
    var corners = [[0, 0], [n - 7, 0], [0, n - 7]];
    for (y = 0; y < n; y++) for (x = 0; x < n; x++) {
      var on = null;
      for (i = 0; i < 3; i++) {
        var a = x - corners[i][0], b = y - corners[i][1];
        if (a >= -1 && a <= 7 && b >= -1 && b <= 7) { on = a >= 0 && a <= 6 && b >= 0 && b <= 6 && ring(a, b); break; }
      }
      if (on === null) on = rnd() > .5;
      if (on) d += "M" + x + " " + y + "h1v1h-1z";
    }
    return '<svg viewBox="0 0 ' + n + ' ' + n + '" shape-rendering="crispEdges" aria-hidden="true"><path fill="currentColor" d="' + d + '"/></svg>';
  }

  /* chapter timeline (fractions of the pinned scroll distance; the track is 720vh) */
  var T_BEAT0 = .025, T_BEATLEN = .50;  /* the four beats: .025 -> .525 */
  var T_TK0 = .56,  T_TK1 = .68;        /* COMMITTED holds to .56, then the ticket is laid onto the stage */
  var T_SC0 = .70,  T_SC1 = .80;        /* scan beam sweeps the QR, stub flips to "Verified" */
  var T_TO0 = .83,  T_TO1 = .89;        /* ticket lifts away */
  var T_RW0 = .87,  T_RW1 = .99;        /* REWIND rises into place, overlapping the ticket's exit slightly */

  var BEATS = [
    { w: "Available", s: "B7 is free. Any number of people can see it and reach for it at the same instant." },
    { w: "Held", s: "One request wins the Redis mutex and the PostgreSQL row lock. Everyone else is turned away." },
    { w: "Charged", s: "Payment runs behind an idempotency key. Retried, replayed or duplicated, it can only charge once." },
    { w: "Committed", s: "The seat commits, the lock is released atomically, and the saga records it as complete." }
  ];

  /* real id + amount from the live matrix when it is there */
  function liveTx() {
    var out = { id: "7c1e80fc\u2026", amt: "$500.00" };
    try {
      var rows = document.querySelectorAll(".tx-table tbody tr");
      for (var i = 0; i < rows.length; i++) {
        var c = rows[i].querySelectorAll("td");
        if (c.length >= 4 && /COMPLETED/i.test(rows[i].textContent)) {
          out.id = c[0].textContent.trim().replace(/\.+$/, "") + "\u2026";
          out.amt = c[3].textContent.trim() || out.amt; break;
        }
      }
    } catch (e) {}
    return out;
  }
  function lines(tx) {
    return [
      [0, "GET /seats/B7", "200 AVAILABLE"],
      [1, "SET seat:B7:lock NX", "OK \u00B7 mutex won"], [1, "SELECT \u2026 FOR UPDATE", "row locked"],
      [2, "POST /pay key=" + tx.id, "CHARGED " + tx.amt], [2, "UNIQUE(tx_id)", "1 row \u00B7 replays refused"],
      [3, "UPDATE seat \u2192 BOOKED", "committed"], [3, "EVAL release.lua", "lock released"], [3, "SagaEvent", "COMPLETED"]
    ];
  }

  function makeStage() {
    var el = document.createElement("div"); el.className = "ss-stage";
    var tx = liveTx();
    el.innerHTML =
      '<div class="ss-top"><span>Chapter 01<i></i>The life of one seat</span><span class="ss-tx">tx ' + tx.id + '</span></div>' +
      '<div class="ss-main"><div class="ss-story">' +
        '<div class="ss-left"><div class="ss-words">' + BEATS.map(function (b, i) { return '<span class="ss-w" data-i="' + i + '">' + b.w + '</span>'; }).join("") + '</div>' +
          '<div class="ss-sents">' + BEATS.map(function (b) { return '<p class="ss-s">' + b.s + '</p>'; }).join("") + '</div></div>' +
        '<div class="ss-right"><div class="ss-tile s0">' +
          '<svg class="ss-ring" viewBox="0 0 100 100" preserveAspectRatio="none"><rect x="0" y="0" width="100" height="100" rx="14" pathLength="1"/></svg>' +
          '<span class="ss-tl">SEAT</span><span class="ss-num">B7</span>' +
          '<svg class="ss-tick" viewBox="0 0 24 24"><path d="M3 12.5l6 6L21 6" pathLength="1"/></svg></div>' +
          '<div class="ss-log">' + lines(tx).map(function (l) { return '<div class="ss-ln" data-b="' + l[0] + '"><span>' + l[1] + '</span><em>' + l[2] + '</em></div>'; }).join("") + '</div></div></div>' +
        /* the ticket this seat produced: same fields as the e-ticket on booking.html */
        '<div class="ss-tkwrap"><div class="ss-ticket">' +
          '<div class="ss-tk-main">' +
            '<div class="ss-tk-head"><span>\u2726 DTC official e-ticket</span><b>Confirmed</b></div>' +
            '<h3 class="ss-tk-title">Coldplay: Music of the Spheres</h3>' +
            '<p class="ss-tk-venue">Grand Arena Stadium \u2022 Tomorrow, 8:00 PM</p>' +
            '<div class="ss-tk-seat"><div class="ss-tk-big"><small>Seat</small><strong>B7</strong></div>' +
              '<dl><div><dt>Row</dt><dd>B</dd></div><div><dt>Tier</dt><dd>Premium Balcony</dd></div><div><dt>Paid</dt><dd class="ss-tk-amt">' + tx.amt + '</dd></div></dl></div>' +
            '<div class="ss-tk-ref"><span>Transaction ref</span><code class="ss-tk-id">' + tx.id + '</code></div>' +
          '</div>' +
          '<div class="ss-tk-stub"><div class="ss-qr"><div class="ss-qr-code">' + qrSvg(tx.id) + '</div><i class="ss-scan"></i>' +
            '<svg class="ss-qr-ok" viewBox="0 0 24 24"><path d="M3 12.5l6 6L21 6" pathLength="1"/></svg></div>' +
            '<span class="ss-tk-cap"><em>Scan at gate</em><em>Verified \u00B7 admit 1</em></span></div>' +
        '</div><p class="ss-tk-note">One seat, one charge, one ticket. However many times the request was sent.</p></div>' +
        '<div class="ss-rew"><svg viewBox="0 0 48 24"><path d="M24 0v24L8 12zM48 0v24L32 12z"/></svg><div class="ss-rw">Rewind</div><p>Replaying from the start</p></div>' +
      '</div>' +
      '<div class="ss-rail"><i class="ss-track"></i><i class="ss-fill"></i><i class="ss-head"></i><span>AVAILABLE</span><span>HELD</span><span>CHARGED</span><span>COMMITTED</span></div>';
    var q = function (s) { return el.querySelector(s); }, qa = function (s) { return [].slice.call(el.querySelectorAll(s)); };
    return { el: el, bi: -1, top: q(".ss-top"), story: q(".ss-story"), left: q(".ss-left"), right: q(".ss-right"), tile: q(".ss-tile"), ring: q(".ss-ring rect"), tick: q(".ss-tick path"),
      words: qa(".ss-w"), sents: qa(".ss-s"), lns: qa(".ss-ln"),
      tk: q(".ss-tkwrap"), ticket: q(".ss-ticket"), tkId: q(".ss-tk-id"), tkAmt: q(".ss-tk-amt"), qr: q(".ss-qr-code"), scan: q(".ss-scan"), qrOk: q(".ss-qr-ok path"), note: q(".ss-tk-note"),
      rail: qa(".ss-rail span"), fill: q(".ss-fill"), head: q(".ss-head"),
      rew: q(".ss-rew"), rp: q(".ss-rew p"), tx: q(".ss-tx") };
  }

  /* pure function of progress p (0..1 through the chapter) and entrance e (0..1 as it scrolls into view) */
  function paint(R, p, e) {
    var bp = clamp((p - T_BEAT0) / T_BEATLEN, 0, 1) * 4, bi = Math.min(3, Math.floor(bp)), t = bp - bi;
    var ent = ease(seg(e, .2, 1));
    var tk = ease(seg(p, T_TK0, T_TK1)), sc = seg(p, T_SC0, T_SC1), to = ease(seg(p, T_TO0, T_TO1)), rw = seg(p, T_RW0, T_RW1);
    if (R.bi !== bi) {
      R.bi = bi;
      R.words.forEach(function (w, i) { w.className = "ss-w" + (i === bi ? " now" : i < bi ? " past" : ""); });
      R.sents.forEach(function (s, i) { s.className = "ss-s" + (i === bi ? " now" : ""); });
      R.tile.className = "ss-tile s" + bi;
      R.lns.forEach(function (l) { var b = +l.dataset.b; l.className = "ss-ln" + (b === bi ? " on cur" : b === bi - 1 ? " on" : ""); });
      var tx = liveTx(); R.tx.textContent = "tx " + tx.id;
      var l2 = R.lns[3].querySelector("span"), l2e = R.lns[3].querySelector("em");
      l2.textContent = "POST /pay key=" + tx.id; l2e.textContent = "CHARGED " + tx.amt;
      R.tkId.textContent = tx.id; R.tkAmt.textContent = tx.amt;
      if (R.qrKey !== tx.id) { R.qrKey = tx.id; R.qr.innerHTML = qrSvg(tx.id); }
    }
    R.ring.style.strokeDashoffset = bi === 1 ? (t * .6).toFixed(3) : bi > 1 ? .6 : 0;
    R.tick.style.strokeDashoffset = (1 - ease(seg(bp, 3.05, 3.6))).toFixed(3);

    /* ---- the story recedes while the ticket arrives, and is gone before REWIND ---- */
    R.top.style.opacity = ent;
    var sOp = ent * (1 - seg(tk, 0, .65));
    R.story.style.opacity = sOp.toFixed(3);
    R.story.style.transform = "translateY(" + ((1 - ent) * 28 - tk * 2.5 * innerHeight / 100).toFixed(1) + "px) scale(" + (1 - .05 * tk).toFixed(4) + ")";
    R.story.style.visibility = sOp < .005 ? "hidden" : "";

    /* ---- TICKET: laid onto the stage (tilts flat as it lands), holds, then lifts away ---- */
    var tOp = seg(tk, .3, 1) * (1 - to);
    R.tk.style.opacity = tOp.toFixed(3);
    R.tk.style.visibility = tOp < .005 ? "hidden" : "visible";
    R.tk.style.transform = "perspective(1600px) translateY(" + (((1 - tk) * 14 - to * 9)).toFixed(2) + "vh) rotateX(" + ((1 - tk) * 16).toFixed(2) + "deg) scale(" + (.94 + .06 * tk - .03 * to).toFixed(4) + ")";
    /* scan beam sweeps down then settles; the QR then confirms */
    var sw = ease(seg(sc, 0, .62)), ok = seg(sc, .62, 1);
    R.scan.style.top = (sw * 100).toFixed(2) + "%";
    R.scan.style.opacity = (sc > 0 && sc < .66 ? Math.min(1, sc * 14) : 0).toFixed(3);
    R.qrOk.style.strokeDashoffset = (1 - ease(seg(ok, .15, 1))).toFixed(3);
    var verified = sc >= .62;
    if (R.verified !== verified) { R.verified = verified; R.ticket.classList.toggle("ok", verified); }
    R.note.style.opacity = ease(seg(p, T_TK0 + .05, T_TK1)).toFixed(3);

    /* ---- REWIND: rises into place and settles; this is the exact last frame the tour lifts off ---- */
    var rf = ease(seg(rw, 0, .8));
    R.rew.style.opacity = ease(seg(rw, 0, .4)).toFixed(3);
    R.rew.style.transform = rf < 1 ? "translateY(" + ((1 - rf) * 7).toFixed(2) + "vh) scale(" + (.96 + .04 * rf).toFixed(4) + ")" : "";
    R.rp.style.opacity = ease(seg(rw, .6, 1));

    var fill = p < T_RW0 ? bp / 4 : 1 - ease(seg(p, T_RW0, 1));
    R.fill.style.transform = "scaleX(" + fill.toFixed(4) + ")"; R.head.style.left = (fill * 100).toFixed(2) + "%";
    var on = p < T_RW0 ? bi + 1 : Math.ceil(fill * 4 - .001);
    if (R.on !== on) { R.on = on; R.rail.forEach(function (s, i) { s.className = i < on ? "on" : ""; }); }
  }

  function init() {
    var host = document.getElementById("st-tour"), main = document.querySelector("main");
    var sec = document.createElement("section"); sec.className = "ss"; sec.setAttribute("aria-label", "One seat, start to finish");
    var R = makeStage(); sec.appendChild(R.el);
    if (host) host.parentNode.insertBefore(sec, host); else if (main) main.insertAdjacentElement("afterend", sec); else return;
    var raf = 0;
    function upd() {
      raf = 0;
      var r = sec.getBoundingClientRect(), vh = innerHeight, span = r.height - vh;
      /* pinned by JS (same method as the tour): position:sticky is unreliable here because the page's body clips overflow */
      var mode = r.top > 0 ? "before" : r.bottom >= vh ? "fixed" : "after";
      if (R.mode !== mode) {
        R.mode = mode; var s = R.el.style;
        s.position = mode === "fixed" ? "fixed" : "absolute"; s.left = "0"; s.right = "0"; s.width = "auto";
        s.top = mode === "after" ? "auto" : "0"; s.bottom = mode === "after" ? "0" : "auto";
      }
      if (r.top > vh * 1.2 || r.bottom < -vh * .2) return;
      paint(R, clamp(-r.top / span, 0, 1), clamp(1 - r.top / vh, 0, 1));
    }
    var go = function () { if (!raf) raf = requestAnimationFrame(upd); };
    addEventListener("scroll", go, { passive: true }); addEventListener("resize", go); go();
    setTimeout(function () { R.bi = -1; go(); }, 2500);   /* table rows load async: pick up the real id once they exist */
    window.SeatStory = { endFrame: function () { var E = makeStage(); paint(E, 1, 1); return E.el; } };
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
