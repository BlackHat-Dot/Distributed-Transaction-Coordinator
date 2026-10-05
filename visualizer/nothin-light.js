/* NOTHIN' LIGHT FIELD  v1.0
   A WebGL cursor light layer: one soft radial gradient that follows the pointer with lag (the "glow"), plus a fluid-ish trail
   (low-res ping-pong simulation: diffuses, drifts and decays) that the glow drags behind it. The display pass adds colour drift
   between two tints, a tiny chromatic offset along the trail and dithering so the gradient never bands.
   It sits above the page with pointer-events:none and mix-blend-mode:screen (multiply on the light theme), so it only ever adds light.
   Presentation only: reads nothing from app state, writes nothing to it. Falls back to a CSS radial gradient when WebGL is missing.

   Tuning (all optional): set before this script  window.NOTHIN_LIGHT = { strength:1, radius:1, trail:1, scale:.6 }
   Tints come from CSS:  --nl-a: r,g,b   --nl-b: r,g,b   (see nothin.css). The hero in finale.js (.fx) fades the light out on purpose. */
(function () {
  "use strict";
  if (window.__nlLoaded) return; window.__nlLoaded = true;
  var O = window.NOTHIN_LIGHT || {};
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var fine = window.matchMedia && matchMedia("(hover:hover) and (pointer:fine)").matches;
  var root = document.documentElement;
  var clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  var STRENGTH = O.strength == null ? 1 : O.strength, RADIUS = O.radius == null ? 1 : O.radius, TRAIL = O.trail == null ? 1 : O.trail, SCALE = O.scale || .6;

  function tint(name, def) {
    var v = (getComputedStyle(root).getPropertyValue(name) || "").trim() || def;
    var p = v.split(",").map(function (n) { return (parseFloat(n) || 0) / 255; });
    return p.length === 3 ? p : tint("--none", def);
  }
  var isLight = function () { return root.getAttribute("data-theme") === "light" || (document.body && document.body.getAttribute("data-theme") === "light"); };

  /* ---------------- shared pointer state (works for both the GL and the CSS fallback) ---------------- */
  var W = 1, H = 1, tgt = { x: -1e4, y: -1e4 }, head = { x: -1e4, y: -1e4 }, glow = { x: -1e4, y: -1e4 }, prev = { x: -1e4, y: -1e4 };
  var inside = false, over = false, touching = false, inten = 0, spd = 0, lastMove = 0, started = false;
  var layer, gl, run = false, raf = 0, lastT = 0, ember = 0, emberCheck = 0, T0 = performance.now();

  function setTarget(e) {
    tgt.x = e.clientX; tgt.y = e.clientY;
    if (!started || !inside) { head.x = glow.x = prev.x = tgt.x; head.y = glow.y = prev.y = tgt.y; started = true; }
    inside = true; lastMove = performance.now();
    over = !!(e.target && e.target.closest && e.target.closest(".fx"));   /* the goo hero has its own light */
    kick();
  }
  function leave() { inside = false; kick(); }

  function place(el) {   /* inline so the layer is correct even if nothin.css is not loaded */
    var st = el.style; st.position = "fixed"; st.left = st.top = "0"; st.width = "100%"; st.height = "100%"; st.pointerEvents = "none"; st.zIndex = "30"; st.opacity = "0"; st.transition = "opacity .35s";
    document.body.appendChild(el);
  }

  /* ---------------- CSS fallback (no WebGL): same lagging radial gradient, just flat ---------------- */
  function cssFallback() {
    layer = document.createElement("div"); layer.id = "nl-css"; layer.setAttribute("aria-hidden", "true");
    place(layer);
    layer.style.mixBlendMode = isLight() ? "multiply" : "screen";
    var a = tint("--nl-a", "159,206,255").map(function (c) { return Math.round(c * 255); }).join(",");
    layer.style.background = "radial-gradient(circle 360px at var(--nl-x,-999px) var(--nl-y,-999px),rgba(" + a + ",.12),transparent 70%)";
    run = true;
    return function (dt) {
      glow.x += (tgt.x - glow.x) * (1 - Math.exp(-dt / 200)); glow.y += (tgt.y - glow.y) * (1 - Math.exp(-dt / 200));
      inten += ((inside && !over ? 1 : 0) - inten) * (1 - Math.exp(-dt / 260));
      layer.style.setProperty("--nl-x", glow.x.toFixed(1) + "px"); layer.style.setProperty("--nl-y", glow.y.toFixed(1) + "px");
      layer.style.opacity = inten.toFixed(3);
    };
  }

  /* ---------------- WebGL ---------------- */
  var VS = "attribute vec2 aP;varying vec2 vUv;void main(){vUv=aP*.5+.5;gl_Position=vec4(aP,0.,1.);}";
  var FS_SIM =
    "precision mediump float;varying vec2 vUv;uniform sampler2D uPrev;uniform vec2 uTexel,uP0,uP1;uniform float uAspect,uRad,uAmt,uDecay,uTime;uniform vec3 uCol;" +
    "void main(){" +
      "vec2 drift=vec2(sin(uTime*.7+vUv.y*6.)*.35,.55)*uTexel;" +                          /* the smoke slowly rises and sways */
      "vec2 uv=vUv-drift;" +
      "vec3 c=texture2D(uPrev,uv).rgb*.36;" +
      "c+=(texture2D(uPrev,uv+vec2(uTexel.x,0.)).rgb+texture2D(uPrev,uv-vec2(uTexel.x,0.)).rgb+texture2D(uPrev,uv+vec2(0.,uTexel.y)).rgb+texture2D(uPrev,uv-vec2(0.,uTexel.y)).rgb)*.16;" +
      "c=max(c*uDecay-.0035,0.);" +                                                        /* subtractive tail: 8-bit buffers never get stuck at a dim value */
      "vec2 a=(vUv-uP0)*vec2(uAspect,1.),b=(uP1-uP0)*vec2(uAspect,1.);" +
      "float h=clamp(dot(a,b)/max(dot(b,b),1e-6),0.,1.);" +
      "float d=length(a-b*h);" +
      "c+=uCol*exp(-d*d/(uRad*uRad))*uAmt;" +                                              /* splat along the whole segment, so fast moves leave no gaps */
      "gl_FragColor=vec4(c,1.);}";
  var FS_SHOW =
    "precision mediump float;varying vec2 vUv;uniform sampler2D uState;uniform vec2 uCur;uniform float uAspect,uTime,uInt,uStr,uTrail,uInv,uRad;uniform vec3 uA,uB;" +
    "void main(){" +
      "vec2 d=(vUv-uCur)*vec2(uAspect,1.);float r=length(d);" +
      "float core=exp(-r*r*(26./(uRad*uRad)));float halo=exp(-r*(2.4/uRad));" +            /* the radial gradient itself: tight core + long soft falloff */
      "float ring=exp(-pow((r-.21*uRad)*34./uRad,2.))*.10;" +                             /* faint lens ring, like light through glass */
      "vec2 dir=d/(r+1e-4);float k=.010*uInt;" +
      "vec3 t=vec3(texture2D(uState,vUv+dir*k).r,texture2D(uState,vUv).g,texture2D(uState,vUv-dir*k).b);" +   /* chromatic offset along the trail */
      "float m=.5+.5*sin(uTime*.32+r*4.5/uRad+dir.x*.7+dir.y*.5);" +                       /* colour transition */
      "vec3 tn=mix(uA,uB,m);" +
      "vec3 col=tn*(core*.85+halo*.5+ring)*uInt*uStr+t*uTrail;" +
      "float g=fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233))+uTime)*43758.5453)-.5;" +
      "col+=g*(3./255.);" +                                                                /* dither: no banding in the long falloff */
      "col=max(col,0.);" +
      "gl_FragColor=vec4(uInv>.5?1.-col*.55:col,1.);}";

  var P = {}, S = [], sw = 2, sh = 2, flip = 0;
  function sh_(type, src) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; }
  function prog(fs) {
    var p = gl.createProgram(); gl.attachShader(p, sh_(gl.VERTEX_SHADER, VS)); gl.attachShader(p, sh_(gl.FRAGMENT_SHADER, fs)); gl.bindAttribLocation(p, 0, "aP"); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS), i;
    for (i = 0; i < n; i++) { var nm = gl.getActiveUniform(p, i).name; u[nm] = gl.getUniformLocation(p, nm); }
    return { p: p, u: u };
  }
  function mkTex(w, h) {
    var t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var f = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, f); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT); return { t: t, f: f };
  }
  function size() {
    W = innerWidth; H = innerHeight;
    var dpr = Math.min(devicePixelRatio || 1, 2) * SCALE;
    layer.width = Math.max(2, Math.round(W * dpr)); layer.height = Math.max(2, Math.round(H * dpr));
    sw = Math.max(2, Math.ceil(W / 6)); sh = Math.max(2, Math.ceil(H / 6));
    if (S.length) { S.forEach(function (s) { gl.deleteTexture(s.t); gl.deleteFramebuffer(s.f); }); }
    S = [mkTex(sw, sh), mkTex(sw, sh)];
  }
  function glInit() {
    layer = document.createElement("canvas"); layer.id = "nl-canvas"; layer.setAttribute("aria-hidden", "true");
    gl = layer.getContext("webgl", { alpha: false, antialias: false, depth: false, stencil: false, preserveDrawingBuffer: false, powerPreference: "low-power" });
    if (!gl) return null;
    try {
      P.sim = prog(FS_SIM); P.show = prog(FS_SHOW);
      var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    } catch (err) { if (window.console) console.warn("[nothin-light] shader failed, using CSS fallback:", err.message); return null; }
    place(layer);
    layer.style.mixBlendMode = isLight() ? "multiply" : "screen";
    size();
    layer.addEventListener("webglcontextlost", function (e) { e.preventDefault(); run = false; }, false);
    return function (dt, now) {
      var t = (now - T0) / 1000, k;
      /* followers: head is quick (drives the trail), glow is slow (drives the radial gradient) */
      k = 1 - Math.exp(-dt / 55); head.x += (tgt.x - head.x) * k; head.y += (tgt.y - head.y) * k;
      k = 1 - Math.exp(-dt / 230); glow.x += (tgt.x - glow.x) * k; glow.y += (tgt.y - glow.y) * k;
      var want = inside && !over ? 1 : 0;
      inten += (want - inten) * (1 - Math.exp(-dt / (want ? 200 : 450)));
      var mv = Math.hypot(head.x - prev.x, head.y - prev.y) / Math.max(dt / 16.7, .5);
      spd += (mv - spd) * .25;
      /* ember: the last 60s of a seat hold (.pm-urgent) warms the light, same cue the old spotlight used */
      if (now - emberCheck > 600) { emberCheck = now; ember = document.querySelector(".pm-urgent") ? 1 : 0; }
      var ea = tint("--nl-a", "159,206,255"), eb = tint("--nl-b", "255,196,150"), em = [1, .42, .32];
      if (ember) { ea = [ea[0] * .3 + em[0] * .7, ea[1] * .3 + em[1] * .7, ea[2] * .3 + em[2] * .7]; eb = em; }
      var asp = W / H, amt = want ? clamp(.06 + spd / 26, 0, 1) * .85 * TRAIL : 0;
      var rad = (.034 + .03 * clamp(spd / 40, 0, 1)) * RADIUS;
      var tcol = [ea[0] * .8 + eb[0] * .2, ea[1] * .8 + eb[1] * .2, ea[2] * .8 + eb[2] * .2];
      /* pass 1: advance the trail */
      var src = S[flip], dst = S[1 - flip];
      gl.bindFramebuffer(gl.FRAMEBUFFER, dst.f); gl.viewport(0, 0, sw, sh);
      gl.useProgram(P.sim.p); gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, src.t);
      var u = P.sim.u;
      gl.uniform1i(u.uPrev, 0); gl.uniform2f(u.uTexel, 1 / sw, 1 / sh);
      gl.uniform2f(u.uP0, prev.x / W, 1 - prev.y / H); gl.uniform2f(u.uP1, head.x / W, 1 - head.y / H);
      gl.uniform1f(u.uAspect, asp); gl.uniform1f(u.uRad, rad); gl.uniform1f(u.uAmt, amt);
      gl.uniform1f(u.uDecay, Math.pow(.968, dt / 16.7)); gl.uniform1f(u.uTime, t); gl.uniform3f(u.uCol, tcol[0] * .8, tcol[1] * .8, tcol[2] * .8);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      flip = 1 - flip; prev.x = head.x; prev.y = head.y;
      /* pass 2: draw glow + trail to the screen */
      gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, layer.width, layer.height);
      gl.useProgram(P.show.p); gl.bindTexture(gl.TEXTURE_2D, S[flip].t); u = P.show.u;
      gl.uniform1i(u.uState, 0); gl.uniform2f(u.uCur, glow.x / W, 1 - glow.y / H);
      gl.uniform1f(u.uAspect, asp); gl.uniform1f(u.uTime, t); gl.uniform1f(u.uInt, inten);
      gl.uniform1f(u.uStr, .5 * STRENGTH); gl.uniform1f(u.uTrail, 1); gl.uniform1f(u.uInv, isLight() ? 1 : 0); gl.uniform1f(u.uRad, .5 * RADIUS);
      gl.uniform3f(u.uA, ea[0], ea[1], ea[2]); gl.uniform3f(u.uB, eb[0], eb[1], eb[2]);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      layer.style.opacity = clamp(inten * 4, 0, 1).toFixed(3);   /* hard off when idle, no GPU compositing for an invisible layer */
    };
  }

  /* ---------------- driver ---------------- */
  var draw;
  function frame(now) {
    raf = 0; if (!run || !draw) return;
    var dt = clamp(now - (lastT || now - 16), 1, 50); lastT = now;
    draw(dt, now);
    /* keep going while the light is visible or the trail still has energy; sleep once everything has faded */
    if (inten > .004 || now - lastMove < 2600) raf = requestAnimationFrame(frame); else { run = false; lastT = 0; if (layer) layer.style.opacity = 0; }
  }
  function kick() { if (!draw) return; if (!run) { run = true; lastT = 0; } if (!raf) raf = requestAnimationFrame(frame); }

  function boot() {
    if (reduce) return;                                    /* reduced motion: no moving light at all */
    draw = glInit() || cssFallback();
    var opts = { passive: true };
    addEventListener("pointermove", function (e) { if (e.pointerType === "touch" && !touching) return; setTarget(e); }, opts);
    addEventListener("pointerdown", function (e) { if (e.pointerType === "touch") touching = true; setTarget(e); }, opts);
    addEventListener("pointerup", function (e) { if (e.pointerType === "touch") { touching = false; leave(); } }, opts);
    addEventListener("pointercancel", function () { touching = false; leave(); }, opts);
    document.documentElement.addEventListener("pointerleave", leave);
    document.addEventListener("mouseleave", leave);
    addEventListener("blur", leave);
    var rt = 0;
    addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { W = innerWidth; H = innerHeight; if (gl && layer.tagName === "CANVAS") size(); kick(); }, 120); });
    new MutationObserver(function () { if (layer) layer.style.mixBlendMode = isLight() ? "multiply" : "screen"; }).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    W = innerWidth; H = innerHeight;
    if (!fine) { /* touch-only devices: light only while a finger is down */ }
  }
  if (document.body) boot(); else document.addEventListener("DOMContentLoaded", boot);
})();
