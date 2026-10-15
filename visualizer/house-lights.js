/* HOUSE LIGHTS — motion layer. Presentation only: reads nothing from app state,
   writes nothing to it. Animates transform/opacity exclusively. */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var KEY = "dtc_hl_from";
  function el(id) { var existing = document.getElementById(id); if (existing) return existing; var d = document.createElement("div"); d.id = id; d.setAttribute("aria-hidden", "true"); document.body.appendChild(d); return d; }
  function go() { root.classList.add("hl-go"); }

  /* 1. Hero reveal — fires once the preloader (index) has left, or at once (booking). */
  if (!reduce) {
    root.classList.add("hl-pre");
    var pre = document.getElementById("dtc-entry-screen");
    var gone = function () { return !pre || !pre.isConnected || getComputedStyle(pre).display === "none" || getComputedStyle(pre).visibility === "hidden" || +getComputedStyle(pre).opacity < 0.05; };
    if (gone()) { requestAnimationFrame(go); }
    else {
      var mo = new MutationObserver(function () { if (gone()) { mo.disconnect(); go(); } });
      mo.observe(document.body, { childList: true, subtree: true });
      mo.observe(pre, { attributes: true });
      setTimeout(go, 6000); /* never leave the hero hidden */
    }
  }

  /* 2. Curtain between Console and Arena. */
  if (!reduce) {
    var curtain = el("hl-curtain");
    var arrived = false;
    try { arrived = sessionStorage.getItem(KEY) === "1"; sessionStorage.removeItem(KEY); } catch (e) {}
    if (arrived) {
      curtain.className = "lift";
      root.classList.remove("hl-arrived");
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          curtain.classList.add("go");
          setTimeout(function () { curtain.className = ""; }, 750);
        });
      });
    } else {
      root.classList.remove("hl-arrived");
      curtain.className = "";
    }
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest("a[href]");
      if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.hasAttribute("download")) return;
      var href = a.getAttribute("href");
      if (!href || href.startsWith("#") || href.startsWith("javascript:")) return;
      var u; try { u = new URL(a.href, location.href); } catch (x) { return; }
      if (u.origin !== location.origin) return;

      var curPath = location.pathname.replace(/\/index\.html$/, "/").replace(/\/+$/, "") || "/";
      var targetPath = u.pathname.replace(/\/index\.html$/, "/").replace(/\/+$/, "") || "/";
      if (curPath === targetPath && location.search === u.search) return;

      var isNavigation =
        (curPath === "/" && (targetPath === "/booking" || targetPath.endsWith("/booking.html"))) ||
        (targetPath === "/" && (curPath === "/booking" || curPath.endsWith("/booking.html"))) ||
        (targetPath.endsWith("/booking.html") && curPath.endsWith("/index.html")) ||
        (targetPath.endsWith("/index.html") && curPath.endsWith("/booking.html")) ||
        /booking(\.html)?$/.test(u.pathname) ||
        /index(\.html)?$/.test(u.pathname);

      if (!isNavigation) return;

      e.preventDefault();
      try { sessionStorage.setItem(KEY, "1"); } catch (x) {}
      curtain.className = "drop";
      setTimeout(function () { location.href = a.href; }, 470);
    });
    window.addEventListener("pageshow", function (ev) {
      if (ev.persisted) {
        curtain.className = "";
        root.classList.remove("hl-arrived");
      }
    }); /* bfcache back */
  }

  /* 3. Scroll-linked: progress rule + hero drift (first viewport only). */
  if (!reduce) {
    var rule = el("hl-rule"), h2 = document.querySelector(".proof-title-area h2"), ticking = false;
    var upd = function () {
      var max = root.scrollHeight - innerHeight, p = max > 0 ? scrollY / max : 0;
      rule.style.transform = "scaleX(" + p.toFixed(4) + ")";
      if (h2 && scrollY < innerHeight) h2.style.translate = "0 " + (-scrollY * 0.12).toFixed(1) + "px";
      ticking = false;
    };
    addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(upd); } }, { passive: true });
    upd();
  }
})();
