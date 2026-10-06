/* ==========================================================================
   FINALE v4 — Native WebGL Shader Fluid Dynamics (noth.in implementation)
   Replicates https://www.noth.in/ exact GPU Navier-Stokes fluid pipeline:
   - GPU-accelerated Eulerian Fluid Simulation (Double FBO ping-pong)
   - Advection with bilinear interpolation, vorticity confinement, pressure Poisson
   - High-contrast non-linear mask composite shader with smoothstep thresholding
   - Video / metallic reveal layer beneath bold text
   - Presentation only; reads/writes no external application state
   ========================================================================== */
(function () {
  "use strict";

  var WORD = window.FINALE_WORD || "ONCE.";
  var SIM_RES = 256;
  var DYE_RES = 512;
  var VEL_DISSIPATION = 0.962;
  var DYE_DISSIPATION = 0.988;
  var PRESSURE_ITER = 20;
  var CURL_STRENGTH = 0.0;
  var SPLAT_RADIUS = 0.00018;
  var SPLAT_FORCE = 5900.0;
  var REVEAL_SIZE = 3.9;
  var EDGE_SOFTNESS = 0.50;
  var EDGE_WIDTH = 0.01;

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;

    var MEDIA = window.FINALE_MEDIA || footer.getAttribute("data-media") || "once.mp4";
    if (typeof MEDIA === "string") MEDIA = MEDIA ? MEDIA.split(",")[0].trim() : "";

    var fx = document.createElement("div");
    fx.className = "fx";
    fx.id = "fx-finale";
    var TICK = '<span>POST /hold <em><b>201</b> seat A-12 held \u00b7 ttl 600s</em></span><span>POST /pay <em>idempotency-key <em>7f3a\u2026c1</em> \u2192 <b>replayed 200</b></em></span><span>POST /pay <em>duplicate in flight \u2192 <u>409 rejected</u></em></span><span>SAGA <em>charge \u2192 reserve \u2192 issue \u00b7 <b>committed</b></em></span><span>SAGA <em>payment timeout \u2192 <u>compensating tx</u> \u2192 released</em></span><span>LOCK <em>seat A-12 \u00b7 optimistic version <em>v7</em> \u00b7 <b>no conflict</b></em></span>';
    fx.innerHTML =
      '<canvas class="fx-cv" aria-hidden="true"></canvas>' +
      '<div class="fx-stage" aria-hidden="true"><i class="fx-c tl"></i><i class="fx-c tr"></i><i class="fx-c bl"></i><i class="fx-c br"></i><span class="fx-hint"><i></i>Move through the word</span></div>' +
      '<div class="fx-ui">' +
        '<div class="fx-foot">' +
          '<div class="fx-copy">' +
            '<h2 class="fx-h fx-r" style="--d:.65s"><span class="l1">Ask a thousand times.</span><span class="l2">Booked exactly once.</span></h2>' +
            '<p class="fx-sub fx-r" style="--d:.8s">Double clicks, retries, races and crashes can\u2019t double-book a seat or double-charge a card. Not promised \u2014 demonstrated.</p></div>' +
          '<div class="fx-panel">' +
            '<a class="fx-btn fx-r" style="--d:.95s" href="booking.html"><span>Book a seat</span><span class="fx-btn-a" aria-hidden="true"><span>\u2192</span><span>\u2192</span></span></a></div>' +
        '</div>' +
        '<div class="fx-ticker fx-r" style="--d:1.1s" aria-hidden="true"><div class="fx-track">' + TICK + TICK + '</div></div>' +
      '</div>';
    footer.insertBefore(fx, footer.firstChild);

    var replayBtn = fx.querySelector(".fx-replay");
    if (replayBtn) {
      replayBtn.addEventListener("click", function () {
        if (window.startScrollTour) window.startScrollTour();
      });
    }

    fx.addEventListener("pointermove", function () { fx.classList.add("fx-touched"); }, { passive: true, once: true });
    var btn = fx.querySelector(".fx-btn");
    if (btn && !matchMedia("(prefers-reduced-motion: reduce)").matches && matchMedia("(hover:hover)").matches) {
      btn.addEventListener("pointermove", function (e) {
        var r = btn.getBoundingClientRect();
        btn.style.transform = "translate(" + ((e.clientX - r.left - r.width / 2) * 0.12).toFixed(1) + "px," + ((e.clientY - r.top - r.height / 2) * 0.22).toFixed(1) + "px)";
      });
      btn.addEventListener("pointerleave", function () { btn.style.transform = ""; });
    }
    var cv = fx.querySelector(".fx-cv");
    var tp = fx.querySelector(".fx-nav");
    var low = fx.querySelector(".fx-foot");

    var cs = getComputedStyle(fx);
    var tok = function (n, d) { return (cs.getPropertyValue(n) || "").trim() || d; };
    var INK = tok("--fx-ink", "#07080a");
    var IVORY = "#14100c"; /* fallback paper */

    var inkRgb = [
      (parseInt(INK.slice(1, 3), 16) || 7) / 255,
      (parseInt(INK.slice(3, 5), 16) || 8) / 255,
      (parseInt(INK.slice(5, 7), 16) || 10) / 255
    ];

    // Try WebGL2 first, then WebGL1
    var gl = cv.getContext("webgl2", { alpha: false, depth: false, antialias: false, preserveDrawingBuffer: false }) ||
             cv.getContext("webgl", { alpha: false, depth: false, antialias: false, preserveDrawingBuffer: false }) ||
             cv.getContext("experimental-webgl", { alpha: false, depth: false, antialias: false, preserveDrawingBuffer: false });

    if (!gl) {
      fallback2D(cv, WORD, "#f1e9dc", IVORY, tp, low);
      return;
    }

    // Determine texture format capabilities
    var isWebGL2 = !!(window.WebGL2RenderingContext && gl instanceof WebGL2RenderingContext);
    var halfFloatExt = isWebGL2 ? gl.getExtension("EXT_color_buffer_float") : (gl.getExtension("OES_texture_half_float") && gl.getExtension("OES_texture_half_float_linear"));
    var floatExt = isWebGL2 ? gl.getExtension("EXT_color_buffer_float") : (gl.getExtension("OES_texture_float") && gl.getExtension("OES_texture_float_linear"));

    var textureType = gl.UNSIGNED_BYTE;
    var internalFormatRGBA = gl.RGBA;
    if (isWebGL2) {
      if (halfFloatExt) {
        internalFormatRGBA = gl.RGBA16F;
        textureType = gl.HALF_FLOAT;
      } else if (floatExt) {
        internalFormatRGBA = gl.RGBA32F;
        textureType = gl.FLOAT;
      }
    } else {
      if (halfFloatExt) {
        textureType = halfFloatExt.HALF_FLOAT_OES || 0x8D61;
      } else if (floatExt) {
        textureType = gl.FLOAT;
      }
    }

    // GLSL Shaders matching noth.in
    var vertShaderSrc = [
      "precision highp float;",
      "attribute vec2 aPosition;",
      "varying vec2 vUv;",
      "void main() {",
      "  vUv = aPosition * 0.5 + 0.5;",
      "  gl_Position = vec4(aPosition, 0.0, 1.0);",
      "}"
    ].join("\n");

    var splatShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uTarget;",
      "uniform float uAspectRatio;",
      "uniform vec2 uPoint;",
      "uniform vec3 uColor;",
      "uniform float uRadius;",
      "void main() {",
      "  vec2 p = vUv - uPoint;",
      "  p.x *= uAspectRatio;",
      "  vec3 splat = exp(-dot(p, p) / uRadius) * uColor;",
      "  vec3 base = texture2D(uTarget, vUv).xyz;",
      "  gl_FragColor = vec4(base + splat, 1.0);",
      "}"
    ].join("\n");

    var curlShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uVelocity;",
      "uniform vec2 uTexelSize;",
      "void main() {",
      "  float L = texture2D(uVelocity, vUv - vec2(uTexelSize.x, 0.0)).y;",
      "  float R = texture2D(uVelocity, vUv + vec2(uTexelSize.x, 0.0)).y;",
      "  float T = texture2D(uVelocity, vUv + vec2(0.0, uTexelSize.y)).x;",
      "  float B = texture2D(uVelocity, vUv - vec2(0.0, uTexelSize.y)).x;",
      "  float vorticity = R - L - T + B;",
      "  gl_FragColor = vec4(0.5 * vorticity, 0.0, 0.0, 1.0);",
      "}"
    ].join("\n");

    var vorticityShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uVelocity;",
      "uniform sampler2D uCurl;",
      "uniform vec2 uTexelSize;",
      "uniform float uCurlStrength;",
      "uniform float uDt;",
      "void main() {",
      "  float L = texture2D(uCurl, vUv - vec2(uTexelSize.x, 0.0)).x;",
      "  float R = texture2D(uCurl, vUv + vec2(uTexelSize.x, 0.0)).x;",
      "  float T = texture2D(uCurl, vUv + vec2(0.0, uTexelSize.y)).x;",
      "  float B = texture2D(uCurl, vUv - vec2(0.0, uTexelSize.y)).x;",
      "  float C = texture2D(uCurl, vUv).x;",
      "  vec2 force = 0.5 * vec2(abs(T) - abs(B), abs(R) - abs(L));",
      "  force /= length(force) + 0.0001;",
      "  force *= uCurlStrength * C * vec2(1.0, -1.0);",
      "  vec2 vel = texture2D(uVelocity, vUv).xy;",
      "  gl_FragColor = vec4(vel + force * uDt, 0.0, 1.0);",
      "}"
    ].join("\n");

    var divergenceShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uVelocity;",
      "uniform vec2 uTexelSize;",
      "void main() {",
      "  float L = texture2D(uVelocity, vUv - vec2(uTexelSize.x, 0.0)).x;",
      "  float R = texture2D(uVelocity, vUv + vec2(uTexelSize.x, 0.0)).x;",
      "  float T = texture2D(uVelocity, vUv + vec2(0.0, uTexelSize.y)).y;",
      "  float B = texture2D(uVelocity, vUv - vec2(0.0, uTexelSize.y)).y;",
      "  float div = 0.5 * (R - L + T - B);",
      "  gl_FragColor = vec4(div, 0.0, 0.0, 1.0);",
      "}"
    ].join("\n");

    var pressureShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uPressure;",
      "uniform sampler2D uDivergence;",
      "uniform vec2 uTexelSize;",
      "void main() {",
      "  float L = texture2D(uPressure, vUv - vec2(uTexelSize.x, 0.0)).x;",
      "  float R = texture2D(uPressure, vUv + vec2(uTexelSize.x, 0.0)).x;",
      "  float T = texture2D(uPressure, vUv + vec2(0.0, uTexelSize.y)).x;",
      "  float B = texture2D(uPressure, vUv - vec2(0.0, uTexelSize.y)).x;",
      "  float C = texture2D(uDivergence, vUv).x;",
      "  float pressure = (L + R + B + T - C) * 0.25;",
      "  gl_FragColor = vec4(pressure, 0.0, 0.0, 1.0);",
      "}"
    ].join("\n");

    var gradientSubShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uPressure;",
      "uniform sampler2D uVelocity;",
      "uniform vec2 uTexelSize;",
      "void main() {",
      "  float L = texture2D(uPressure, vUv - vec2(uTexelSize.x, 0.0)).x;",
      "  float R = texture2D(uPressure, vUv + vec2(uTexelSize.x, 0.0)).x;",
      "  float T = texture2D(uPressure, vUv + vec2(0.0, uTexelSize.y)).x;",
      "  float B = texture2D(uPressure, vUv - vec2(0.0, uTexelSize.y)).x;",
      "  vec2 vel = texture2D(uVelocity, vUv).xy;",
      "  vel -= vec2(R - L, T - B) * 0.5;",
      "  gl_FragColor = vec4(vel, 0.0, 1.0);",
      "}"
    ].join("\n");

    var advectionShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uVelocity;",
      "uniform sampler2D uSource;",
      "uniform vec2 uTexelSize;",
      "uniform float uDt;",
      "uniform float uDissipation;",
      "vec4 bilerp(sampler2D sam, vec2 uv, vec2 tsize) {",
      "  vec2 st = uv / tsize - 0.5;",
      "  vec2 iuv = floor(st);",
      "  vec2 fuv = fract(st);",
      "  vec4 a = texture2D(sam, (iuv + vec2(0.5, 0.5)) * tsize);",
      "  vec4 b = texture2D(sam, (iuv + vec2(1.5, 0.5)) * tsize);",
      "  vec4 c = texture2D(sam, (iuv + vec2(0.5, 1.5)) * tsize);",
      "  vec4 d = texture2D(sam, (iuv + vec2(1.5, 1.5)) * tsize);",
      "  return mix(mix(a, b, fuv.x), mix(c, d, fuv.x), fuv.y);",
      "}",
      "void main() {",
      "  vec2 vel = texture2D(uVelocity, vUv).xy;",
      "  vec2 coord = vUv - uDt * vel * uTexelSize;",
      "  coord = clamp(coord, uTexelSize * 0.5, 1.0 - uTexelSize * 0.5);",
      "  vec4 result = uDissipation * bilerp(uSource, coord, uTexelSize);",
      "  gl_FragColor = result;",
      "}"
    ].join("\n");

    var compositeShaderSrc = [
      "precision highp float;",
      "varying vec2 vUv;",
      "uniform sampler2D uBaseTexture;",
      "uniform sampler2D uRevealTexture;",
      "uniform sampler2D uDye;",
      "uniform float uRevealSize;",
      "uniform float uEdgeSoftness;",
      "uniform float uEdgeWidth;",
      "uniform vec3 uInkColor;",
      "uniform float uScreenAspect;",
      "uniform float uVideoAspect;",
      "uniform float uTime;",
      "vec2 coverUv(vec2 uv, float vAsp, float sAsp) {",
      "  vec2 ratio = vec2(min(sAsp / vAsp, 1.0), min(vAsp / sAsp, 1.0));",
      "  return clamp(uv * ratio + (1.0 - ratio) * 0.5, 0.001, 0.999);",
      "}",
      "void main() {",
      "  vec2 texUv = vec2(vUv.x, 1.0 - vUv.y);",
      "  float dye = texture2D(uDye, vUv).r;",
      "  vec4 baseColor = texture2D(uBaseTexture, texUv);",
      "  vec2 revUv = coverUv(texUv, uVideoAspect, uScreenAspect);",
      "  vec4 revealColor = texture2D(uRevealTexture, revUv);",
      "  float raw = dye * uRevealSize;",
      "  float mask = smoothstep(uEdgeSoftness, uEdgeSoftness + uEdgeWidth, raw);",
      "  mask = clamp(mask, 0.0, 1.0);",
      "  vec3 col = mix(baseColor.rgb, revealColor.rgb, mask);",
      "  float halo = smoothstep(uEdgeSoftness - 1.2, uEdgeSoftness, raw) * (1.0 - mask);",
      "  float rim = mask * (1.0 - smoothstep(uEdgeSoftness + uEdgeWidth, uEdgeSoftness + uEdgeWidth + 0.8, raw));",
      "  col += vec3(1.0, 0.69, 0.13) * (halo * 0.09 + rim * 0.34);",
      "  float g = fract(sin(dot(gl_FragCoord.xy + uTime * 61.0, vec2(12.9898, 78.233))) * 43758.5453);",
      "  col += (g - 0.5) * 0.026;",
      "  gl_FragColor = vec4(col, 1.0);",
      "}"
    ].join("\n");

    function createShader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return s;
    }

    function createProgram(vSrc, fSrc) {
      var p = gl.createProgram();
      var vs = createShader(gl.VERTEX_SHADER, vSrc);
      var fs = createShader(gl.FRAGMENT_SHADER, fSrc);
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      var uniforms = {};
      var count = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (var i = 0; i < count; i++) {
        var name = gl.getActiveUniform(p, i).name;
        uniforms[name] = gl.getUniformLocation(p, name);
      }
      return { program: p, uniforms: uniforms };
    }

    // Compile pipelines
    var splatProg = createProgram(vertShaderSrc, splatShaderSrc);
    var curlProg = createProgram(vertShaderSrc, curlShaderSrc);
    var vorticityProg = createProgram(vertShaderSrc, vorticityShaderSrc);
    var divergenceProg = createProgram(vertShaderSrc, divergenceShaderSrc);
    var pressureProg = createProgram(vertShaderSrc, pressureShaderSrc);
    var gradSubProg = createProgram(vertShaderSrc, gradientSubShaderSrc);
    var advectionProg = createProgram(vertShaderSrc, advectionShaderSrc);
    var compositeProg = createProgram(vertShaderSrc, compositeShaderSrc);

    // Quad geometry
    var quadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);

    function createFBO(w, h, filt) {
      filt = filt || gl.LINEAR;
      gl.activeTexture(gl.TEXTURE0);
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filt);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filt);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, internalFormatRGBA, w, h, 0, gl.RGBA, textureType, null);

      var fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.viewport(0, 0, w, h);
      gl.clear(gl.COLOR_BUFFER_BIT);

      return {
        texture: tex,
        framebuffer: fb,
        width: w,
        height: h,
        texelSizeX: 1.0 / w,
        texelSizeY: 1.0 / h,
        attach: function (id) {
          gl.activeTexture(gl.TEXTURE0 + id);
          gl.bindTexture(gl.TEXTURE_2D, tex);
          return id;
        }
      };
    }

    function createDoubleFBO(w, h, filt) {
      var fbo1 = createFBO(w, h, filt);
      var fbo2 = createFBO(w, h, filt);
      return {
        width: w,
        height: h,
        texelSizeX: 1.0 / w,
        texelSizeY: 1.0 / h,
        read: fbo1,
        write: fbo2,
        swap: function () {
          var tmp = this.read;
          this.read = this.write;
          this.write = tmp;
        }
      };
    }

    var velocity = createDoubleFBO(SIM_RES, SIM_RES);
    var dye = createDoubleFBO(DYE_RES, DYE_RES);
    var pressure = createDoubleFBO(SIM_RES, SIM_RES, gl.NEAREST);
    var divergence = createFBO(SIM_RES, SIM_RES, gl.NEAREST);
    var curl = createFBO(SIM_RES, SIM_RES, gl.NEAREST);

    function blit(dest) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, dest ? dest.framebuffer : null);
      if (dest) {
        gl.viewport(0, 0, dest.width, dest.height);
      } else {
        gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.enableVertexAttribArray(0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    // Dynamic Base & Reveal Textures
    var baseCanvas = document.createElement("canvas");
    var baseCtx = baseCanvas.getContext("2d");
    var baseTexture = gl.createTexture();

    var revealCanvas = document.createElement("canvas");
    var revealCtx = revealCanvas.getContext("2d");
    var revealTexture = gl.createTexture();

    // Video Layer Support
    var videoEl = null;
    var videoReady = false;
    var videoAspect = 1890 / 1080;
    var videoSrc = MEDIA || "once.mp4";
    if (videoSrc) {
      videoEl = document.createElement("video");
      if (/^https?:\/\//i.test(videoSrc)) {
        videoEl.crossOrigin = "anonymous";
      }
      videoEl.src = videoSrc;
      videoEl.muted = true;
      videoEl.defaultMuted = true;
      videoEl.autoplay = true;
      videoEl.loop = true;
      videoEl.playsInline = true;
      videoEl.setAttribute("playsinline", "");
      videoEl.setAttribute("muted", "");

      var startPlay = function () {
        videoReady = true;
        if (videoEl.videoWidth && videoEl.videoHeight) {
          videoAspect = videoEl.videoWidth / videoEl.videoHeight;
        }
        var p = videoEl.play();
        if (p && p.catch) p.catch(function () {});
      };
      startPlay();
      videoEl.addEventListener("loadeddata", startPlay, { once: true });
      videoEl.addEventListener("canplay", startPlay, { once: true });
      videoEl.addEventListener("loadedmetadata", function () {
        if (videoEl.videoWidth && videoEl.videoHeight) {
          videoAspect = videoEl.videoWidth / videoEl.videoHeight;
          updateBaseTexture();
        }
      });
      videoEl.load();
    }

    // Build procedural 3D chrome balloon/foil texture matching noth.in
    function buildProceduralChrome(c, ctx) {
      c.width = 1024;
      c.height = 1024;
      var id = ctx.createImageData(1024, 1024);
      var d = id.data;
      for (var y = 0; y < 1024; y++) {
        for (var x = 0; x < 1024; x++) {
          var u = x / 1024.0, v = y / 1024.0;
          var n1 = Math.sin(u * 14.0 + Math.cos(v * 10.0) * 3.0);
          var n2 = Math.cos(v * 16.0 + Math.sin(u * 12.0) * 2.5);
          var n3 = Math.sin((u + v) * 20.0 + n1 * 1.5);
          var bump = (n1 * 0.4 + n2 * 0.4 + n3 * 0.2);
          var spec = Math.pow(Math.max(0.0, bump * 0.5 + 0.5), 7.0);
          var fresnel = Math.pow(Math.abs(bump), 2.2);

          var lum = 0.18 + 0.45 * (bump * 0.5 + 0.5) + 0.8 * spec + 0.25 * fresnel;
          var r = Math.min(255, Math.max(0, Math.round(lum * 255)));
          var g = Math.min(255, Math.max(0, Math.round((lum * 0.98 + 0.02) * 255)));
          var b = Math.min(255, Math.max(0, Math.round(lum * 1.05 * 255)));

          var idx = (y * 1024 + x) * 4;
          d[idx] = r;
          d[idx + 1] = g;
          d[idx + 2] = b;
          d[idx + 3] = 255;
        }
      }
      ctx.putImageData(id, 0, 0);
    }
    buildProceduralChrome(revealCanvas, revealCtx);

    gl.bindTexture(gl.TEXTURE_2D, revealTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, revealCanvas);

    // Mask layer matching once.mp4 3D text geometry (crisp vector SVG with full stop)
    var maskImg = new Image();
    var maskReady = false;
    maskImg.onload = function () {
      maskReady = true;
      updateBaseTexture();
    };
    maskImg.onerror = function () {
      if (maskImg.src.indexOf(".png") === -1) {
        maskImg.src = "once_mask.png";
      }
    };
    maskImg.src = "once_mask.svg";
    if (maskImg.complete && maskImg.naturalWidth) {
      maskReady = true;
    }

    // Rasterize base texture (ivory background + inky letters matching video geometry)
    var tmpCanvas = document.createElement("canvas"), tctx = tmpCanvas.getContext("2d");
    var W = 0, H = 0, dpr = 1;
    function updateBaseTexture() {
      var r = fx.getBoundingClientRect();
      W = Math.max(10, Math.round(r.width));
      H = Math.max(10, Math.round(r.height));
      dpr = Math.min(window.devicePixelRatio || 1, 2);

      cv.width = Math.round(W * dpr);
      cv.height = Math.round(H * dpr);
      baseCanvas.width = cv.width;
      baseCanvas.height = cv.height;

      baseCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var pg = baseCtx.createRadialGradient(W * 0.5, H * 0.46, 0, W * 0.5, H * 0.46, Math.max(W, H) * 0.78);
      pg.addColorStop(0, "#261f17"); pg.addColorStop(0.5, "#16120e"); pg.addColorStop(1, "#0a0807");
      baseCtx.fillStyle = pg;
      baseCtx.fillRect(0, 0, W, H);
      baseCtx.imageSmoothingEnabled = true;
      baseCtx.imageSmoothingQuality = "high";

      var sAsp = W / H;
      var vAsp = videoAspect;
      var covW = (sAsp > vAsp) ? W : (H * vAsp);
      var covH = (sAsp > vAsp) ? (W / vAsp) : H;
      var offsetX = (W - covW) * 0.5;
      var offsetY = (H - covH) * 0.5;

      /* letters are drawn on their own layer, then tinted ivory (silk gradient) so the dark svg fill never shows */
      tmpCanvas.width = cv.width; tmpCanvas.height = cv.height;
      tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      tctx.clearRect(0, 0, W, H);
      tctx.imageSmoothingEnabled = true; tctx.imageSmoothingQuality = "high";
      if (maskReady && maskImg.naturalWidth) {
        tctx.drawImage(maskImg, offsetX, offsetY, covW, covH);
      } else {
        var FONT = '"Montserrat","Schibsted Grotesk",Arial Black,system-ui,sans-serif';
        tctx.font = "900 " + (covH * 0.369 * 1.05) + "px " + FONT;
        tctx.textAlign = "center"; tctx.textBaseline = "middle";
        tctx.fillStyle = "#000";
        tctx.fillText(WORD, W / 2, H / 2);
      }
      tctx.globalCompositeOperation = "source-in";
      var lg = tctx.createLinearGradient(0, H * 0.25, 0, H * 0.78);
      lg.addColorStop(0, "#fbf3e3"); lg.addColorStop(1, "#cfc3ab");
      tctx.fillStyle = lg;
      tctx.fillRect(0, 0, W, H);
      tctx.globalCompositeOperation = "source-over";
      baseCtx.drawImage(tmpCanvas, 0, 0, W, H);

      gl.bindTexture(gl.TEXTURE_2D, baseTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, baseCanvas);
    }

    // Fluid Simulation Splat (matching noth.in GPU Navier-Stokes pipeline)
    function splat(x, y, dx, dy, s) {
      s = (s === undefined ? 1.0 : s) * intensity;
      var aspect = W / H;
      if (s === undefined) s = 1.0;


      // Velocity splat (matching noth.in: u * SPLAT_FORCE * s, f * SPLAT_FORCE * s, 0)
      gl.useProgram(splatProg.program);
      gl.uniform1i(splatProg.uniforms.uTarget, velocity.read.attach(0));
      gl.uniform1f(splatProg.uniforms.uAspectRatio, aspect);
      gl.uniform2f(splatProg.uniforms.uPoint, x, y);
      gl.uniform3f(splatProg.uniforms.uColor, dx * SPLAT_FORCE * s, dy * SPLAT_FORCE * s, 0.0);
      gl.uniform1f(splatProg.uniforms.uRadius, SPLAT_RADIUS);
      blit(velocity.write);
      velocity.swap();

      // Dye splat (matching noth.in: s, s, s)
      gl.uniform1i(splatProg.uniforms.uTarget, dye.read.attach(0));
      gl.uniform3f(splatProg.uniforms.uColor, s, s, s);
      gl.uniform1f(splatProg.uniforms.uRadius, SPLAT_RADIUS);
      blit(dye.write);
      dye.swap();
    }

    // Pointer Events (matching noth.in: decoupled from raw event, executed once per RAF frame)
    var mouse = { x: 0.5, y: 0.5 };
    var prevMouse = { x: 0.5, y: 0.5 };
    var mouseHasMoved = false;
    var inside = false;

    function resetPointer() {
      inside = false;
      mouseHasMoved = false;
    }

    function onPointerMove(e) {
      var r = fx.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) {
        inside = false;
        return;
      }

      var x = (e.clientX - r.left) / r.width;
      var y = 1.0 - (e.clientY - r.top) / r.height;
      x = Math.max(0.001, Math.min(0.999, x));
      y = Math.max(0.001, Math.min(0.999, y));

      if (!inside) {
        prevMouse.x = x;
        prevMouse.y = y;
        inside = true;
      }

      mouse.x = x;
      mouse.y = y;
      mouseHasMoved = true;
      if (videoEl && videoEl.paused) {
        videoEl.play().catch(function () {});
      }
    }

    fx.addEventListener("pointermove", onPointerMove, { passive: true });
    fx.addEventListener("pointerdown", function (e) {
      inside = false;
      onPointerMove(e);
      if (videoEl && videoEl.paused) {
        videoEl.play().catch(function () {});
      }
      if (inside) {
        splat(mouse.x, mouse.y, 0.0, 0.0, 1.0);
      }
    }, { passive: true });
    fx.addEventListener("pointerleave", resetPointer);
    document.addEventListener("mouseleave", resetPointer);
    window.addEventListener("blur", resetPointer);

    // Step Simulation
    var visible = true;
    var intensity = 1, fadeSq = 0;
    function updateFade() {
      var r = fx.getBoundingClientRect();
      var n = Math.max(0, Math.min(1, -r.top / (r.height || 1)));
      fadeSq = n * n; intensity = 1 - fadeSq;
    }
    function step() {
      if (!visible) return;
      updateFade();

      // Handle pointer motion strictly once per RAF frame matching noth.in
      if (mouseHasMoved && inside) {
        var u = mouse.x - prevMouse.x;
        var f = mouse.y - prevMouse.y;
        var dist = Math.sqrt(u * u + f * f);

        // Reject massive teleports/hitches across window boundary
        if (dist > 0 && dist < 0.2 && intensity > 0.001) {
          splat(mouse.x, mouse.y, u, f, 1.0);
        }
        prevMouse.x = mouse.x;
        prevMouse.y = mouse.y;
        mouseHasMoved = false;
      }

      // 1. Curl & Vorticity (noth.in has curlStrength = 0)
      if (CURL_STRENGTH > 0) {
        gl.useProgram(curlProg.program);
        gl.uniform1i(curlProg.uniforms.uVelocity, velocity.read.attach(0));
        gl.uniform2f(curlProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
        blit(curl);

        gl.useProgram(vorticityProg.program);
        gl.uniform1i(vorticityProg.uniforms.uVelocity, velocity.read.attach(0));
        gl.uniform1i(vorticityProg.uniforms.uCurl, curl.attach(1));
        gl.uniform2f(vorticityProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
        gl.uniform1f(vorticityProg.uniforms.uCurlStrength, CURL_STRENGTH);
        gl.uniform1f(vorticityProg.uniforms.uDt, 0.016);
        blit(velocity.write);
        velocity.swap();
      }

      // 2. Velocity Advection
      gl.useProgram(advectionProg.program);
      gl.uniform1i(advectionProg.uniforms.uVelocity, velocity.read.attach(0));
      gl.uniform1i(advectionProg.uniforms.uSource, velocity.read.attach(0));
      gl.uniform2f(advectionProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
      gl.uniform1f(advectionProg.uniforms.uDt, 1.0);
      gl.uniform1f(advectionProg.uniforms.uDissipation, VEL_DISSIPATION);
      blit(velocity.write);
      velocity.swap();

      // 3. Dye Advection (dissipation matching noth.in)
      gl.uniform1i(advectionProg.uniforms.uVelocity, velocity.read.attach(0));
      gl.uniform1i(advectionProg.uniforms.uSource, dye.read.attach(1));
      gl.uniform2f(advectionProg.uniforms.uTexelSize, dye.texelSizeX, dye.texelSizeY);
      gl.uniform1f(advectionProg.uniforms.uDissipation, DYE_DISSIPATION + (0.97 - DYE_DISSIPATION) * fadeSq);
      blit(dye.write);
      dye.swap();

      // 4. Divergence
      gl.useProgram(divergenceProg.program);
      gl.uniform1i(divergenceProg.uniforms.uVelocity, velocity.read.attach(0));
      gl.uniform2f(divergenceProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
      blit(divergence);

      // 5. Pressure Poisson Relaxation (Jacobi) - cleared each frame like noth.in
      gl.bindFramebuffer(gl.FRAMEBUFFER, pressure.read.framebuffer);
      gl.viewport(0, 0, pressure.read.width, pressure.read.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(pressureProg.program);
      gl.uniform1i(pressureProg.uniforms.uDivergence, divergence.attach(1));
      gl.uniform2f(pressureProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
      for (var i = 0; i < PRESSURE_ITER; i++) {
        gl.uniform1i(pressureProg.uniforms.uPressure, pressure.read.attach(0));
        blit(pressure.write);
        pressure.swap();
      }

      // 6. Gradient Subtraction
      gl.useProgram(gradSubProg.program);
      gl.uniform1i(gradSubProg.uniforms.uPressure, pressure.read.attach(0));
      gl.uniform1i(gradSubProg.uniforms.uVelocity, velocity.read.attach(1));
      gl.uniform2f(gradSubProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
      blit(velocity.write);
      velocity.swap();

      // 7. Video frame update (if active)
      if (videoReady && videoEl && !videoEl.paused) {
        gl.bindTexture(gl.TEXTURE_2D, revealTexture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, videoEl);
      }

      // 8. Mask Composite (noth.in threshold render with cover UV)
      gl.useProgram(compositeProg.program);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, baseTexture);
      gl.uniform1i(compositeProg.uniforms.uBaseTexture, 0);

      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, revealTexture);
      gl.uniform1i(compositeProg.uniforms.uRevealTexture, 1);

      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, dye.read.texture);
      gl.uniform1i(compositeProg.uniforms.uDye, 2);

      gl.uniform1f(compositeProg.uniforms.uRevealSize, REVEAL_SIZE);
      gl.uniform1f(compositeProg.uniforms.uEdgeSoftness, EDGE_SOFTNESS);
      gl.uniform1f(compositeProg.uniforms.uEdgeWidth, EDGE_WIDTH);
      gl.uniform3f(compositeProg.uniforms.uInkColor, inkRgb[0], inkRgb[1], inkRgb[2]);
      gl.uniform1f(compositeProg.uniforms.uScreenAspect, W / H);
      gl.uniform1f(compositeProg.uniforms.uVideoAspect, videoAspect);
      gl.uniform1f(compositeProg.uniforms.uTime, (performance.now() * 0.001) % 50.0);

      blit(null); // Draw directly to canvas
    }

    var raf = 0;
    function loop() {
      raf = requestAnimationFrame(loop);
      step();
    }
    loop();

    // Resize handling
    var resizeTimer = 0;
    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        updateBaseTexture();
      }, 80);
    }
    if ("ResizeObserver" in window) {
      new ResizeObserver(onResize).observe(fx);
    } else {
      window.addEventListener("resize", onResize);
    }

    // Visibility handling
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
      }, { threshold: 0 }).observe(fx);
    }
    document.addEventListener("visibilitychange", function () {
      visible = !document.hidden;
    });

    // Font load trigger
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(updateBaseTexture);
    }
    updateBaseTexture();
    fx.classList.add("fx-ready");
  }

  // Graceful 2D fallback for devices with no WebGL
  function fallback2D(cv, word, ink, ivory, tp, low) {
    var ctx = cv.getContext("2d");
    if (!ctx) return;
    var mask = new Image();
    var maskLoaded = false;
    mask.onload = function () {
      maskLoaded = true;
      draw();
    };
    mask.onerror = function () {
      if (mask.src.indexOf(".png") === -1) {
        mask.src = "once_mask.png";
      }
    };
    mask.src = "once_mask.svg";
    if (mask.complete && mask.naturalWidth) {
      maskLoaded = true;
    }

    function draw() {
      var r = cv.parentElement.getBoundingClientRect();
      var w = Math.round(r.width), h = Math.round(r.height);
      var dpr = window.devicePixelRatio || 1;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = ivory;
      ctx.fillRect(0, 0, w, h);
      var vAsp = 1890 / 1080;
      var sAsp = w / h;
      var covW = (sAsp > vAsp) ? w : (h * vAsp);
      var covH = (sAsp > vAsp) ? (w / vAsp) : h;
      var offsetX = (w - covW) * 0.5;
      var offsetY = (h - covH) * 0.5;

      if (maskLoaded && mask.naturalWidth) {
        try { ctx.filter = "invert(1) brightness(1.7)"; } catch (e) {}
        ctx.drawImage(mask, offsetX, offsetY, covW, covH);
        try { ctx.filter = "none"; } catch (e) {}
      } else {
        var topR = (tp ? tp.offsetHeight : 0) + 30;
        var lowR = (low ? low.offsetHeight : 0) + 30;
        var fs = Math.min(w * 0.22, (h - topR - lowR) * 0.95);
        ctx.font = "900 " + fs + "px sans-serif";
        ctx.fillStyle = ink;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(word, w / 2, topR + (h - topR - lowR) / 2);
      }
    }
    window.addEventListener("resize", draw);
    draw();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
