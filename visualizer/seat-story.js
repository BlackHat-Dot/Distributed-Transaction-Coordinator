/* SEAT STORY: one seat followed through the saga while you scroll, then a REWIND that hands off to the guided tour.
   Presentation only: reads the first completed row of the live transaction table for a real id/amount, writes nothing to app state.
   Exposes SeatStory.endFrame() so scroll-tour.js can lift off this chapter's exact last screen.

   v1.3 EXIT ("tip-up seat"): one continuous hinge motion, scrubbed by scroll (so it plays backwards too).
     1. The B7 tile gets a small push toward you (wind-up), then glides up on its top edge like a cinema seat when someone stands.
     2. The whole COMMITTED window follows on the same kind of hinge, shading out as it turns edge-on.
     3. Both land on one glowing hinge line along the top of the stage.
     4. REWIND swings down from that same line, settles with a small bounce, and is the exact last frame handed to the tour.
   Every stage begins at identity (rotation 0) and ends at rest, so nothing appears or disappears abruptly. */
(function () {
  "use strict";
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var seg = function (x, a, b) { return clamp((x - a) / (b - a), 0, 1); };
  var ease = function (t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  /* swing: a small wind-up the "wrong way" (smooth bump, zero slope at both ends), then an ease-in-out glide that
     decelerates into the hinge. 0 -> 90 degrees with no overshoot, so it never slams or flips past edge-on. */
  var swing = function (t, wind) { var u = seg(t, 0, .35), b = Math.sin(Math.PI * u); return 90 * ease(t) - wind * b * b; };
  /* back-out: overshoots then settles (used for REWIND dropping onto its hinge). */
  var backOut = function (t, c) { var u = t - 1; return 1 + (c + 1) * u * u * u + c * u * u; };
  var fold = function (deg, persp) { return "perspective(" + persp + "px) rotateX(" + (-deg).toFixed(2) + "deg)"; };

  /* chapter timeline (fractions of the pinned scroll distance) */
  var T_BEAT0 = .03, T_BEATLEN = .60;   /* the four beats: .03 -> .63 */
  var T_EXIT0 = .66, T_EXIT1 = .88;     /* COMMITTED holds .63 -> .66, then the folds play .66 -> .88 */
  var T_RW1 = .99;                      /* REWIND swings down .88 -> .99 */

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
        '<i class="ss-hinge"></i>' +
        '<div class="ss-rew"><svg viewBox="0 0 48 24"><path d="M24 0v24L8 12zM48 0v24L32 12z"/></svg><div class="ss-rw">Rewind</div><p>Replaying from the start</p></div>' +
      '</div>' +
      '<div class="ss-rail"><i class="ss-track"></i><i class="ss-fill"></i><i class="ss-head"></i><span>AVAILABLE</span><span>HELD</span><span>CHARGED</span><span>COMMITTED</span></div>';
    var q = function (s) { return el.querySelector(s); }, qa = function (s) { return [].slice.call(el.querySelectorAll(s)); };
    return { el: el, bi: -1, top: q(".ss-top"), story: q(".ss-story"), left: q(".ss-left"), right: q(".ss-right"), tile: q(".ss-tile"), ring: q(".ss-ring rect"), tick: q(".ss-tick path"),
      words: qa(".ss-w"), sents: qa(".ss-s"), lns: qa(".ss-ln"), hinge: q(".ss-hinge"),
      rail: qa(".ss-rail span"), fill: q(".ss-fill"), head: q(".ss-head"),
      rew: q(".ss-rew"), rp: q(".ss-rew p"), tx: q(".ss-tx") };
  }

  /* pure function of progress p (0..1 through the chapter) and entrance e (0..1 as it scrolls into view) */
  function paint(R, p, e) {
    var bp = clamp((p - T_BEAT0) / T_BEATLEN, 0, 1) * 4, bi = Math.min(3, Math.floor(bp)), t = bp - bi;
    var q = seg(p, T_EXIT0, T_EXIT1), rw = seg(p, T_EXIT1, T_RW1), ent = ease(seg(e, .2, 1));
    if (R.bi !== bi) {
      R.bi = bi;
      R.words.forEach(function (w, i) { w.className = "ss-w" + (i === bi ? " now" : i < bi ? " past" : ""); });
      R.sents.forEach(function (s, i) { s.className = "ss-s" + (i === bi ? " now" : ""); });
      R.tile.className = "ss-tile s" + bi;
      R.lns.forEach(function (l) { var b = +l.dataset.b; l.className = "ss-ln" + (b === bi ? " on cur" : b === bi - 1 ? " on" : ""); });
      var tx = liveTx(); R.tx.textContent = "tx " + tx.id;
      var l2 = R.lns[3].querySelector("span"), l2e = R.lns[3].querySelector("em");
      l2.textContent = "POST /pay key=" + tx.id; l2e.textContent = "CHARGED " + tx.amt;
    }
    R.ring.style.strokeDashoffset = bi === 1 ? (t * .6).toFixed(3) : bi > 1 ? .6 : 0;
    R.tick.style.strokeDashoffset = (1 - ease(seg(bp, 3.05, 3.6))).toFixed(3);

    /* ---- EXIT: two nested folds on a shared hinge ---- */
    var fT = seg(q, 0, .5);     /* the seat tile folds first  (0 = flat, 1 = edge-on) */
    var fW = seg(q, .4, 1);     /* then the whole window, starting before the tile has finished so it flows */
    var aT = swing(fT, 10), aW = swing(fW, 5);   /* the small negative start is the wind-up toward you */
    R.tile.style.transformOrigin = "50% 0";
    R.tile.style.transform = fT > 0 ? fold(aT, 1000) : "";
    R.tile.style.opacity = fT > .8 ? (1 - seg(fT, .8, 1)).toFixed(3) : "";

    R.top.style.opacity = ent;
    R.story.style.opacity = q >= 1 ? 0 : (ent * (1 - seg(fW, .75, 1))).toFixed(3);   /* shades out as it turns edge-on */
    R.story.style.transform = "translateY(" + ((1 - ent) * 28).toFixed(1) + "px)" + (fW > 0 ? " " + fold(aW, 1600) : "");

    /* the hinge line: the folded window's edge, which REWIND then hangs from */
    R.hinge.style.opacity = (seg(q, .8, 1) * (1 - seg(rw, .15, .6))).toFixed(3);
    R.hinge.style.transform = "scaleX(" + ease(seg(q, .72, 1)).toFixed(4) + ")";

    /* ---- REWIND: swings down from the hinge and settles with a small bounce ---- */
    var rf = seg(rw, 0, .8), aR = 90 * (1 - backOut(rf, 1.2));
    R.rew.style.opacity = rw > 0 ? ease(seg(rw, 0, .3)).toFixed(3) : 0;
    R.rew.style.transform = rf < 1 ? fold(aR, 1400) : "";
    R.rp.style.opacity = ease(seg(rw, .6, 1));

    var fill = p < T_EXIT1 ? bp / 4 : 1 - ease(seg(p, T_EXIT1, 1));
    R.fill.style.transform = "scaleX(" + fill.toFixed(4) + ")"; R.head.style.left = (fill * 100).toFixed(2) + "%";
    var on = p < T_EXIT1 ? bi + 1 : Math.ceil(fill * 4 - .001);
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
