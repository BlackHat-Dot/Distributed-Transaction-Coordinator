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
  var CURL_STRENGTH = 6.0;
  var SPLAT_RADIUS = 0.00018;
  var SPLAT_FORCE = 5900.0;
  var REVEAL_SIZE = 3.9;
  var EDGE_SOFTNESS = 0.50;
  var EDGE_WIDTH = 0.01;

  function init() {
    var footer = document.querySelector(".site-footer");
    if (!footer || footer.querySelector(".fx")) return;

    var MEDIA = window.FINALE_MEDIA || footer.getAttribute("data-media") || "";
    if (typeof MEDIA === "string") MEDIA = MEDIA ? MEDIA.split(",")[0].trim() : "";

    var fx = document.createElement("div");
    fx.className = "fx";
    fx.id = "fx-finale";
    fx.innerHTML =
      '<canvas class="fx-cv" aria-hidden="true"></canvas>' +
      '<div class="fx-ui">' +
        '<div class="fx-top"><h2 class="fx-h">Ask a thousand times.<br>Booked exactly once.</h2>' +
          '<div class="fx-cta"><a class="fx-pill" href="booking.html">Book a seat <span aria-hidden="true">\u2192</span></a></div></div>' +
        '<div class="fx-low"><p class="fx-sub">Double clicks, retries, races and crashes can\u2019t double-book a seat or double-charge a card. Proven live, not just promised.</p>' +
          '<div class="fx-links"><button type="button" class="fx-replay">Replay tour</button><a href="https://github.com/BlackHat-Dot/Distributed-transaction-coordinator" target="_blank" rel="noopener noreferrer">GitHub</a></div></div>' +
      '</div>';
    footer.insertBefore(fx, footer.firstChild);

    var replayBtn = fx.querySelector(".fx-replay");
    if (replayBtn) {
      replayBtn.addEventListener("click", function () {
        if (window.startScrollTour) window.startScrollTour();
      });
    }

    var cv = fx.querySelector(".fx-cv");
    var tp = fx.querySelector(".fx-top");
    var low = fx.querySelector(".fx-low");

    var cs = getComputedStyle(fx);
    var tok = function (n, d) { return (cs.getPropertyValue(n) || "").trim() || d; };
    var INK = tok("--fx-ink", "#07080a");
    var IVORY = tok("--fx-ivory", "#f6ecda");

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
      fallback2D(cv, WORD, INK, IVORY, tp, low);
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
      "  vec2 coord = vUv - uDt * texture2D(uVelocity, vUv).xy * uTexelSize;",
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
      "void main() {",
      "  float dye = texture2D(uDye, vUv).r;",
      "  vec4 baseColor = texture2D(uBaseTexture, vUv);",
      "  vec4 revealColor = texture2D(uRevealTexture, vUv);",
      "  float raw = dye * uRevealSize;",
      "  float mask = smoothstep(uEdgeSoftness, uEdgeSoftness + uEdgeWidth, raw);",
      "  mask = clamp(mask, 0.0, 1.0);",
      "  float isText = 1.0 - smoothstep(0.12, 0.48, baseColor.r);",
      "  vec4 fluidLayer = mix(vec4(uInkColor, 1.0), revealColor, isText);",
      "  gl_FragColor = mix(baseColor, fluidLayer, mask);",
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

    function createFBO(w, h) {
      gl.activeTexture(gl.TEXTURE0);
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
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

    function createDoubleFBO(w, h) {
      var fbo1 = createFBO(w, h);
      var fbo2 = createFBO(w, h);
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
    var pressure = createDoubleFBO(SIM_RES, SIM_RES);
    var divergence = createFBO(SIM_RES, SIM_RES);
    var curl = createFBO(SIM_RES, SIM_RES);

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
    if (MEDIA) {
      videoEl = document.createElement("video");
      videoEl.src = MEDIA;
      videoEl.muted = true;
      videoEl.loop = true;
      videoEl.playsInline = true;
      videoEl.setAttribute("playsinline", "");
      videoEl.setAttribute("muted", "");
      videoEl.addEventListener("canplay", function () {
        videoReady = true;
        var p = videoEl.play();
        if (p && p.catch) p.catch(function () {});
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

    // Rasterize crisp vector text for Base Texture
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
      baseCtx.fillStyle = IVORY;
      baseCtx.fillRect(0, 0, W, H);

      var FONT = '"Montserrat","Schibsted Grotesk",Arial Black,system-ui,sans-serif';
      var topR = (tp ? tp.offsetHeight : 0) + 30;
      var lowR = (low ? low.offsetHeight : 0) + 30;
      var padX = Math.max(16, W * 0.02);

      baseCtx.font = "900 100px " + FONT;
      var em = baseCtx.measureText(WORD).width / 100;
      var fs = Math.min((W - padX * 2) / em, (H - topR - lowR) * 0.95);
      var cy = topR + (H - topR - lowR) / 2 + (W < 760 ? 0 : H * 0.03);
      var base = cy + fs * 0.35;

      baseCtx.font = "900 " + fs + "px " + FONT;
      baseCtx.textAlign = "center";
      baseCtx.textBaseline = "alphabetic";
      baseCtx.fillStyle = INK;
      baseCtx.fillText(WORD, W / 2, base);

      gl.bindTexture(gl.TEXTURE_2D, baseTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, baseCanvas);
    }

    // Fluid Simulation Splat
    function splat(x, y, dx, dy) {
      var aspect = W / H;

      // Velocity splat
      gl.useProgram(splatProg.program);
      gl.uniform1i(splatProg.uniforms.uTarget, velocity.read.attach(0));
      gl.uniform1f(splatProg.uniforms.uAspectRatio, aspect);
      gl.uniform2f(splatProg.uniforms.uPoint, x, y);
      gl.uniform3f(splatProg.uniforms.uColor, dx * SPLAT_FORCE, dy * SPLAT_FORCE, 0.0);
      gl.uniform1f(splatProg.uniforms.uRadius, SPLAT_RADIUS);
      blit(velocity.write);
      velocity.swap();

      // Dye splat
      gl.uniform1i(splatProg.uniforms.uTarget, dye.read.attach(0));
      gl.uniform3f(splatProg.uniforms.uColor, 1.0, 1.0, 1.0);
      gl.uniform1f(splatProg.uniforms.uRadius, SPLAT_RADIUS);
      blit(dye.write);
      dye.swap();
    }

    // Pointer Events
    var lastX = 0, lastY = 0, hasMoved = false, inside = false;
    function onPointerMove(e) {
      var r = fx.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width;
      var y = 1.0 - (e.clientY - r.top) / r.height;

      if (!inside) {
        lastX = x;
        lastY = y;
        inside = true;
      }

      var dx = x - lastX;
      var dy = y - lastY;
      if (Math.sqrt(dx * dx + dy * dy) > 0.001) {
        splat(x, y, dx, dy);
        lastX = x;
        lastY = y;
        hasMoved = true;
      }
    }

    fx.addEventListener("pointermove", onPointerMove, { passive: true });
    fx.addEventListener("pointerdown", function (e) {
      onPointerMove(e);
      var r = fx.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width;
      var y = 1.0 - (e.clientY - r.top) / r.height;
      splat(x, y, 0.05, 0.05);
    }, { passive: true });
    fx.addEventListener("pointerleave", function () { inside = false; });

    // Step Simulation
    var visible = true;
    function step() {
      if (!visible) return;

      // 1. Curl & Vorticity
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

      // 3. Dye Advection
      gl.uniform1i(advectionProg.uniforms.uVelocity, velocity.read.attach(0));
      gl.uniform1i(advectionProg.uniforms.uSource, dye.read.attach(1));
      gl.uniform2f(advectionProg.uniforms.uTexelSize, dye.texelSizeX, dye.texelSizeY);
      gl.uniform1f(advectionProg.uniforms.uDissipation, DYE_DISSIPATION);
      blit(dye.write);
      dye.swap();

      // 4. Divergence
      gl.useProgram(divergenceProg.program);
      gl.uniform1i(divergenceProg.uniforms.uVelocity, velocity.read.attach(0));
      gl.uniform2f(divergenceProg.uniforms.uTexelSize, velocity.texelSizeX, velocity.texelSizeY);
      blit(divergence);

      // 5. Pressure Poisson Relaxation (Jacobi)
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

      // 8. Mask Composite (noth.in threshold render)
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
    function draw() {
      var r = cv.parentElement.getBoundingClientRect();
      var w = Math.round(r.width), h = Math.round(r.height);
      var dpr = window.devicePixelRatio || 1;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = ivory;
      ctx.fillRect(0, 0, w, h);
      var topR = (tp ? tp.offsetHeight : 0) + 30;
      var lowR = (low ? low.offsetHeight : 0) + 30;
      var fs = Math.min(w * 0.22, (h - topR - lowR) * 0.95);
      ctx.font = "900 " + fs + "px sans-serif";
      ctx.fillStyle = ink;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(word, w / 2, topR + (h - topR - lowR) / 2);
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
