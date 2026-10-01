/**
 * DTC Staff Gate Scanner - gate.js
 * Single-use ticket redemption interface for event gate staff.
 */

(function () {
  "use strict";

  // --- 1. System Configuration & URLs ---
  const isLocal = typeof window !== "undefined" && (
    window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1" ||
    window.location.hostname === "::1" ||
    window.location.hostname === "[::1]" ||
    window.location.protocol === "file:"
  );

  const localHost = (typeof window !== "undefined" && window.location.hostname && window.location.protocol !== "file:" && window.location.hostname !== "::1" && window.location.hostname !== "[::1]")
    ? window.location.hostname
    : "localhost";

  // NOTE: COORDINATOR_URL is NEVER read from localStorage to eliminate key redirection vectors.
  const COORDINATOR_URL = isLocal
    ? `http://${localHost}:8002`
    : "https://reverse-proxy.nexoflow.workers.dev/coordinator";

  // ACCEPTED_VERIFY_ORIGINS: Validated against scanned QR URLs (default: PUBLIC_BASE_URL origin)
  function getAcceptedVerifyOrigins() {
    if (window.__GATE_CONFIG__ && Array.isArray(window.__GATE_CONFIG__.acceptedVerifyOrigins)) {
      return window.__GATE_CONFIG__.acceptedVerifyOrigins;
    }
    return isLocal
      ? [`http://${localHost}:8002`, "http://localhost:8002", "http://127.0.0.1:8002"]
      : [
          "https://reverse-proxy.nexoflow.workers.dev",
          "https://distributed-transaction-coordinator-production.up.railway.app",
        ];
  }

  // --- 2. In-Memory State (Never persisted to storage, URLs, or cookies) ---
  let gateKey = "";
  let admittedCount = 0;
  let inFlight = false;
  let lastScannedCode = null;
  let lastScannedTime = 0;
  let resetTimer = null;
  let html5QrScanner = null;
  let dummyFileScanner = null;

  // --- 3. DOM Elements ---
  const DOM = {
    keyEntryView: document.getElementById("key-entry-view"),
    gateKeyInput: document.getElementById("gate-key-input"),
    unlockGateBtn: document.getElementById("unlock-gate-btn"),
    keyEntryError: document.getElementById("key-entry-error"),
    scannerView: document.getElementById("scanner-view"),
    sessionCounter: document.getElementById("session-counter"),
    lockGateBtn: document.getElementById("lock-gate-btn"),
    cameraFallback: document.getElementById("camera-fallback"),
    cameraFallbackMsg: document.getElementById("camera-fallback-msg"),
    qrFileInput: document.getElementById("qr-file-input"),
    uploadPhotoBtn: document.getElementById("upload-photo-btn"),
    manualTicketCode: document.getElementById("manual-ticket-code"),
    manualSubmitBtn: document.getElementById("manual-submit-btn"),
    resultOverlay: document.getElementById("result-overlay"),
    resultStatus: document.getElementById("result-status"),
    resultSeatRow: document.getElementById("result-seat-row"),
    resultSeat: document.getElementById("result-seat"),
    resultEventRow: document.getElementById("result-event-row"),
    resultEvent: document.getElementById("result-event"),
    resultUsedRow: document.getElementById("result-used-row"),
    resultUsedTime: document.getElementById("result-used-time"),
    resultMessage: document.getElementById("result-message"),
    zoomBadge: document.getElementById("zoom-badge"),
    zoomBadgeText: document.getElementById("zoom-badge-text"),
    zoomControls: document.getElementById("zoom-controls"),
  };

  // --- 4. Helper: Format IST Time ---
  function formatIST(isoString) {
    if (!isoString) return "";
    try {
      const d = new Date(isoString);
      return d.toLocaleString("en-IN", {
        timeZone: "Asia/Kolkata",
        day: "numeric",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: true,
      });
    } catch (e) {
      return isoString;
    }
  }

  // --- 5. Result Display & Auto-Reset ---
  function showResult(type, details = {}) {
    resetCameraZoom();
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }

    // Reset overlay classes
    DOM.resultOverlay.className = "result-overlay active";
    DOM.resultSeatRow.classList.remove("hidden");
    DOM.resultEventRow.classList.remove("hidden");
    DOM.resultUsedRow.classList.add("hidden");
    DOM.resultMessage.textContent = "";

    // Vibrate device if supported
    if (typeof navigator !== "undefined" && typeof navigator.vibrate === "function") {
      try {
        if (type === "ADMIT") {
          navigator.vibrate([100, 50, 100]);
        } else {
          navigator.vibrate(300);
        }
      } catch (err) {}
    }

    switch (type) {
      case "ADMIT":
        DOM.resultOverlay.classList.add("result-admit");
        DOM.resultStatus.textContent = "ADMIT";
        DOM.resultSeat.textContent = details.seat_label || "Unknown Seat";
        DOM.resultEvent.textContent = details.event_name || "DTC Event";
        break;

      case "ALREADY_USED":
        DOM.resultOverlay.classList.add("result-already-used");
        DOM.resultStatus.textContent = "ALREADY USED";
        DOM.resultSeat.textContent = details.seat_label || "-";
        DOM.resultEventRow.classList.add("hidden");
        DOM.resultUsedRow.classList.remove("hidden");
        DOM.resultUsedTime.textContent = formatIST(details.used_at);
        break;

      case "NOT_VALID":
        DOM.resultOverlay.classList.add("result-not-valid");
        DOM.resultStatus.textContent = "NOT VALID";
        DOM.resultSeatRow.classList.add("hidden");
        DOM.resultEventRow.classList.add("hidden");
        DOM.resultMessage.textContent = details.message || "Invalid ticket code or QR origin.";
        break;

      case "KEY_REJECTED":
        DOM.resultOverlay.classList.add("result-key-rejected");
        DOM.resultStatus.textContent = "KEY REJECTED";
        DOM.resultSeatRow.classList.add("hidden");
        DOM.resultEventRow.classList.add("hidden");
        DOM.resultMessage.textContent = details.message || "Gate key was rejected by server. Clearing session.";
        // Clear key from memory & input immediately
        gateKey = "";
        DOM.gateKeyInput.value = "";
        break;

      case "SLOW_DOWN":
        DOM.resultOverlay.classList.add("result-slow-down");
        DOM.resultStatus.textContent = "SLOW DOWN";
        DOM.resultSeatRow.classList.add("hidden");
        DOM.resultEventRow.classList.add("hidden");
        DOM.resultMessage.textContent = details.message || "Rate limit reached. Please wait before retrying.";
        break;

      case "NETWORK_ERROR":
      default:
        DOM.resultOverlay.classList.add("result-network-error");
        DOM.resultStatus.textContent = "NETWORK ERROR";
        DOM.resultSeatRow.classList.add("hidden");
        DOM.resultEventRow.classList.add("hidden");
        DOM.resultMessage.textContent = details.message || "Network request failed. Please retry.";
        break;
    }

    // Auto-reset after 3 seconds
    resetTimer = setTimeout(() => {
      dismissResult();
    }, 3000);
  }

  function dismissResult() {
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
    DOM.resultOverlay.className = "result-overlay hidden";

    // If key was rejected or cleared, return to key entry view
    if (!gateKey) {
      lockScanner();
    }
  }

  DOM.resultOverlay.addEventListener("click", dismissResult);

  // --- 6. Core Scan Handler (Shared by Camera, Photo Upload, and Manual Entry) ---
  async function handleScan(rawText) {
    if (!rawText || typeof rawText !== "string") {
      showResult("NOT_VALID", { message: "Empty scan content" });
      return;
    }

    // Client-side Origin & URL format verification
    let ticketCode = null;
    try {
      const parsedUrl = new URL(rawText.trim());
      const acceptedOrigins = getAcceptedVerifyOrigins();

      if (!acceptedOrigins.includes(parsedUrl.origin)) {
        showResult("NOT_VALID", { message: "Untrusted QR origin: " + parsedUrl.origin });
        return;
      }

      // Supports both direct root path (/verify/<code>/) and subpaths (/coordinator/verify/<code>/)
      const match = parsedUrl.pathname.match(/(?:^|\/)verify\/([A-Za-z0-9_-]{16,32})\/?$/);
      if (!match) {
        showResult("NOT_VALID", { message: "Invalid ticket URL format" });
        return;
      }
      ticketCode = match[1];
    } catch (e) {
      showResult("NOT_VALID", { message: "QR does not contain a valid URL" });
      return;
    }

    // Concurrency Lock: ignore if already in-flight
    if (inFlight) {
      return;
    }

    // Debounce: ignore same code scanned within 3 seconds
    const now = Date.now();
    if (ticketCode === lastScannedCode && (now - lastScannedTime) < 3000) {
      return;
    }
    lastScannedCode = ticketCode;
    lastScannedTime = now;

    inFlight = true;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);

    try {
      const response = await fetch(`${COORDINATOR_URL}/gate/redeem/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Staff-Key": gateKey,
        },
        body: JSON.stringify({ ticket_code: ticketCode, gate_key: gateKey }),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      let data = null;
      try {
        data = await response.json();
      } catch (jsonErr) {
        showResult("NETWORK_ERROR", { message: "Unparsable response from server" });
        return;
      }

      // Response Mapping by `code` field and HTTP status
      if (response.status === 200 && data && data.status === "REDEEMED") {
        admittedCount++;
        DOM.sessionCounter.textContent = `Admitted this session: ${admittedCount}`;
        showResult("ADMIT", {
          seat_label: data.seat_label,
          event_name: data.event_name,
        });
      } else if (response.status === 409 && data && data.code === "ALREADY_USED") {
        showResult("ALREADY_USED", {
          seat_label: data.seat_label,
          used_at: data.used_at,
        });
      } else if (
        (response.status === 409 && data && data.code === "TRANSACTION_NOT_COMPLETED") ||
        (response.status === 404 && data && data.code === "NOT_FOUND") ||
        (response.status === 400 && data && data.code === "BAD_REQUEST")
      ) {
        showResult("NOT_VALID", {
          message: data.error || "Ticket not valid",
        });
      } else if (response.status === 401 && data && data.code === "UNAUTHORIZED") {
        showResult("KEY_REJECTED", {
          message: data.error || "Gate API key rejected",
        });
      } else if (response.status === 429 && data && data.code === "RATE_LIMITED") {
        showResult("SLOW_DOWN", {
          message: data.error || "Rate limit reached. Please wait before retrying.",
        });
      } else {
        showResult("NETWORK_ERROR", {
          message: (data && data.error) ? data.error : "Unexpected server response",
        });
      }
    } catch (err) {
      clearTimeout(timeoutId);
      showResult("NETWORK_ERROR", {
        message: err.name === "AbortError" ? "Request timed out" : "Network connection failed",
      });
    } finally {
      inFlight = false;
    }
  }

  // Expose global handler for testing and external integrations
  window.__gateHandleScan = handleScan;

  // --- 7. Key Management & View Transitions ---
  async function unlockScanner() {
    const enteredKey = DOM.gateKeyInput.value.trim();
    if (!enteredKey) {
      DOM.keyEntryError.textContent = "Please enter the gate API key.";
      DOM.keyEntryError.classList.remove("hidden");
      return;
    }

    DOM.unlockGateBtn.disabled = true;
    DOM.unlockGateBtn.textContent = "Verifying...";
    DOM.keyEntryError.classList.add("hidden");

    try {
      const response = await fetch(`${COORDINATOR_URL}/gate/redeem/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Staff-Key": enteredKey,
        },
        body: JSON.stringify({ action: "verify_key", gate_key: enteredKey }),
      });

      if (response.status === 401) {
        DOM.keyEntryError.textContent = "Invalid Gate API Key. Access denied.";
        DOM.keyEntryError.classList.remove("hidden");
        return;
      }

      if (response.status === 429) {
        DOM.keyEntryError.textContent = "Rate limit reached. Please wait a minute.";
        DOM.keyEntryError.classList.remove("hidden");
        return;
      }

      let data = null;
      try { data = await response.json(); } catch (_) {}

      // Accept if 200 (new coordinator) or 400 with BAD_REQUEST (older coordinator where key was accepted but ticket_code missing)
      if (response.status === 200 || (response.status === 400 && data && data.code === "BAD_REQUEST")) {
        gateKey = enteredKey;
        DOM.keyEntryError.classList.add("hidden");
        DOM.keyEntryView.classList.add("hidden");
        DOM.scannerView.classList.remove("hidden");
        initCameraScanner();
      } else {
        DOM.keyEntryError.textContent = (data && data.error) ? data.error : "Key verification failed.";
        DOM.keyEntryError.classList.remove("hidden");
      }
    } catch (err) {
      DOM.keyEntryError.textContent = "Unable to connect to Coordinator service.";
      DOM.keyEntryError.classList.remove("hidden");
    } finally {
      DOM.unlockGateBtn.disabled = false;
      DOM.unlockGateBtn.textContent = "Unlock Scanner";
    }
  }

  function lockScanner() {
    stopAutoZoomEngine();
    gateKey = "";
    DOM.gateKeyInput.value = "";
    DOM.scannerView.classList.add("hidden");
    DOM.keyEntryView.classList.remove("hidden");

    if (html5QrScanner) {
      try {
        html5QrScanner.stop().catch(() => {});
      } catch (e) {}
      html5QrScanner = null;
    }
  }

  DOM.unlockGateBtn.addEventListener("click", unlockScanner);
  DOM.lockGateBtn.addEventListener("click", lockScanner);

  // --- 8a. Google Pay (GPay) Style Auto-Zoom Engine ---
  let autoZoomDetectionLoopId = null;
  let autoZoomBadgeTimer = null;
  let zoomAnimationId = null;
  let currentZoomLevel = 1.0;
  let cameraZoomCaps = null;
  let barcodeDetectorInstance = null;
  let lastQrDetectTime = 0;
  let autoZoomScanCanvas = null;
  let autoZoomScanCtx = null;

  function initBarcodeDetector() {
    if (typeof window !== "undefined" && "BarcodeDetector" in window) {
      try {
        barcodeDetectorInstance = new window.BarcodeDetector({ formats: ["qr_code"] });
      } catch (e) {
        barcodeDetectorInstance = null;
      }
    }
  }

  function inspectCameraZoomCapabilities(video) {
    if (!video || !video.srcObject) return null;
    const stream = video.srcObject;
    const track = (typeof stream.getVideoTracks === "function") ? stream.getVideoTracks()[0] : null;
    if (!track) return null;

    const caps = (typeof track.getCapabilities === "function") ? track.getCapabilities() : {};
    const settings = (typeof track.getSettings === "function") ? track.getSettings() : {};
    const hasHardwareZoom = Boolean(caps && "zoom" in caps && caps.zoom && caps.zoom.max > caps.zoom.min);

    return {
      track,
      hasHardwareZoom,
      min: hasHardwareZoom ? (caps.zoom.min || 1) : 1,
      max: hasHardwareZoom ? (caps.zoom.max || 5) : 3,
      step: hasHardwareZoom ? (caps.zoom.step || 0.1) : 0.1,
      current: hasHardwareZoom ? (settings.zoom || 1) : 1,
    };
  }

  async function applyCameraZoom(zoomLevel, { isAuto = false } = {}) {
    if (!cameraZoomCaps) return;
    const min = cameraZoomCaps.min;
    const max = cameraZoomCaps.max;
    const step = cameraZoomCaps.step;

    let clamped = Math.max(min, Math.min(max, zoomLevel));
    if (step > 0) {
      clamped = Math.round((clamped - min) / step) * step + min;
      clamped = Math.max(min, Math.min(max, clamped));
    }

    currentZoomLevel = clamped;

    if (cameraZoomCaps.hasHardwareZoom && cameraZoomCaps.track) {
      try {
        await cameraZoomCaps.track.applyConstraints({
          advanced: [{ zoom: clamped }],
        });
      } catch (err) {
        const video = document.querySelector("#qr-reader video");
        if (video) {
          video.style.transform = clamped > 1.05 ? `scale(${clamped})` : "";
          video.style.transformOrigin = "center center";
        }
      }
    } else {
      const video = document.querySelector("#qr-reader video");
      if (video) {
        video.style.transform = clamped > 1.05 ? `scale(${clamped})` : "";
        video.style.transformOrigin = "center center";
      }
    }

    updateZoomChipsUI(clamped);

    if (isAuto && clamped > 1.15) {
      showZoomBadge(`🔍 Auto-Zoom ${clamped.toFixed(1)}x`);
    } else if (clamped <= 1.05) {
      hideZoomBadge();
    }
  }

  function smoothZoomTo(targetZoom, isAuto = false) {
    if (zoomAnimationId) {
      cancelAnimationFrame(zoomAnimationId);
      zoomAnimationId = null;
    }

    const startZoom = currentZoomLevel;
    const diff = targetZoom - startZoom;
    if (Math.abs(diff) < 0.05) {
      applyCameraZoom(targetZoom, { isAuto });
      return;
    }

    const startTime = performance.now();
    const duration = Math.min(260, Math.max(120, Math.abs(diff) * 80));

    function step(now) {
      const progress = Math.min(1, (now - startTime) / duration);
      const ease = 1 - Math.pow(1 - progress, 3);
      const val = startZoom + diff * ease;
      applyCameraZoom(val, { isAuto });

      if (progress < 1) {
        zoomAnimationId = requestAnimationFrame(step);
      } else {
        zoomAnimationId = null;
      }
    }

    zoomAnimationId = requestAnimationFrame(step);
  }

  function resetCameraZoom() {
    if (currentZoomLevel > 1.05) {
      smoothZoomTo(1.0, false);
    }
  }

  function showZoomBadge(text) {
    if (!DOM.zoomBadge || !DOM.zoomBadgeText) return;
    DOM.zoomBadgeText.textContent = text;
    DOM.zoomBadge.classList.remove("hidden");
    if (autoZoomBadgeTimer) clearTimeout(autoZoomBadgeTimer);
    autoZoomBadgeTimer = setTimeout(() => {
      hideZoomBadge();
    }, 2000);
  }

  function hideZoomBadge() {
    if (DOM.zoomBadge) DOM.zoomBadge.classList.add("hidden");
    if (autoZoomBadgeTimer) {
      clearTimeout(autoZoomBadgeTimer);
      autoZoomBadgeTimer = null;
    }
  }

  function updateZoomChipsUI(activeZoom) {
    if (!DOM.zoomControls) return;
    const chips = DOM.zoomControls.querySelectorAll(".zoom-chip");
    chips.forEach((chip) => {
      const z = parseFloat(chip.dataset.zoom) || 1.0;
      if (Math.abs(z - activeZoom) < 0.45) {
        chip.classList.add("active");
      } else {
        chip.classList.remove("active");
      }
    });
  }

  function setupZoomControls() {
    if (!DOM.zoomControls) return;
    DOM.zoomControls.classList.remove("hidden");
    const chips = DOM.zoomControls.querySelectorAll(".zoom-chip");
    chips.forEach((chip) => {
      chip.onclick = (e) => {
        e.preventDefault();
        const target = parseFloat(chip.dataset.zoom) || 1.0;
        smoothZoomTo(target, false);
      };
    });
  }

  function checkFinderPatternRatio(counts) {
    const total = counts[0] + counts[1] + counts[2] + counts[3] + counts[4];
    if (total < 7) return false;
    const moduleSize = total / 7;
    const maxVariance = moduleSize / 2;
    return (
      Math.abs(moduleSize - counts[0]) < maxVariance &&
      Math.abs(moduleSize - counts[1]) < maxVariance &&
      Math.abs(3 * moduleSize - counts[2]) < 3 * maxVariance &&
      Math.abs(moduleSize - counts[3]) < maxVariance &&
      Math.abs(moduleSize - counts[4]) < maxVariance
    );
  }

  function detectFinderPatternRatio(video) {
    if (!autoZoomScanCanvas) {
      autoZoomScanCanvas = document.createElement("canvas");
      autoZoomScanCanvas.width = 160;
      autoZoomScanCanvas.height = 160;
      autoZoomScanCtx = autoZoomScanCanvas.getContext("2d", { willReadFrequently: true });
    }
    if (!autoZoomScanCtx) return null;

    try {
      autoZoomScanCtx.drawImage(video, 0, 0, 160, 160);
      const imgData = autoZoomScanCtx.getImageData(0, 0, 160, 160);
      const data = imgData.data;
      const width = 160;
      const height = 160;
      const centers = [];

      for (let y = 10; y < height - 10; y += 6) {
        let state = 0;
        const counts = [0, 0, 0, 0, 0];
        let lastIsBlack = false;

        for (let x = 0; x < width; x++) {
          const idx = (y * width + x) * 4;
          const isBlack = (data[idx] * 299 + data[idx + 1] * 587 + data[idx + 2] * 114) < 128000;

          if (x === 0) {
            lastIsBlack = isBlack;
            counts[0] = 1;
            continue;
          }

          if (isBlack === lastIsBlack) {
            counts[state]++;
          } else {
            if (state < 4) {
              state++;
              counts[state] = 1;
            } else {
              if (checkFinderPatternRatio(counts)) {
                const centerX = x - counts[4] - counts[3] - (counts[2] / 2);
                centers.push({ x: centerX, y });
              }
              counts[0] = counts[2];
              counts[1] = counts[3];
              counts[2] = counts[4];
              counts[3] = 1;
              counts[4] = 0;
              state = 3;
            }
            lastIsBlack = isBlack;
          }
        }
      }

      if (centers.length >= 2) {
        let maxDist = 0;
        for (let i = 0; i < centers.length; i++) {
          for (let j = i + 1; j < centers.length; j++) {
            const d = Math.hypot(centers[i].x - centers[j].x, centers[i].y - centers[j].y);
            if (d > maxDist) maxDist = d;
          }
        }
        if (maxDist > 12) {
          const estimatedQrSize = maxDist / 0.8;
          return estimatedQrSize / 160;
        }
      }
    } catch (_) {}
    return null;
  }

  async function autoZoomDetectionTick(video) {
    if (!video || video.readyState < 2 || inFlight) return;

    let detectedRatio = null;
    let detectedRawValue = null;

    if (barcodeDetectorInstance) {
      try {
        const barcodes = await barcodeDetectorInstance.detect(video);
        if (barcodes && barcodes.length > 0) {
          const bc = barcodes[0];
          const box = bc.boundingBox;
          const qrDim = Math.max(box.width, box.height);
          const viewDim = Math.min(video.videoWidth || 640, video.videoHeight || 480);
          detectedRatio = qrDim / viewDim;
          if (bc.rawValue) {
            detectedRawValue = bc.rawValue;
          }
        }
      } catch (_) {}
    }

    if (detectedRatio === null) {
      detectedRatio = detectFinderPatternRatio(video);
    }

    const now = Date.now();
    if (detectedRatio !== null && detectedRatio > 0.05) {
      lastQrDetectTime = now;

      const targetRatio = 0.58;
      const maxAllowedZoom = cameraZoomCaps ? cameraZoomCaps.max : 4.0;

      if (detectedRatio < 0.35 && currentZoomLevel < maxAllowedZoom) {
        const neededMultiplier = targetRatio / detectedRatio;
        const desiredZoom = currentZoomLevel * neededMultiplier;
        const targetZoom = Math.min(maxAllowedZoom, Math.max(1, desiredZoom));

        if (targetZoom > currentZoomLevel + 0.25) {
          smoothZoomTo(targetZoom, true);
        }
      }

      if (detectedRawValue) {
        handleScan(detectedRawValue);
      }
    } else {
      if (currentZoomLevel > 1.05 && (now - lastQrDetectTime) > 2000) {
        smoothZoomTo(1.0, false);
      }
    }
  }

  function startAutoZoomEngine(video) {
    stopAutoZoomEngine();
    initBarcodeDetector();
    cameraZoomCaps = inspectCameraZoomCapabilities(video);
    currentZoomLevel = cameraZoomCaps ? cameraZoomCaps.current : 1.0;
    setupZoomControls();

    const intervalMs = 120;
    autoZoomDetectionLoopId = setInterval(() => {
      autoZoomDetectionTick(video);
    }, intervalMs);
  }

  function stopAutoZoomEngine() {
    if (autoZoomDetectionLoopId) {
      clearInterval(autoZoomDetectionLoopId);
      autoZoomDetectionLoopId = null;
    }
    if (zoomAnimationId) {
      cancelAnimationFrame(zoomAnimationId);
      zoomAnimationId = null;
    }
    hideZoomBadge();
    resetCameraZoom();
    if (DOM.zoomControls) {
      DOM.zoomControls.classList.add("hidden");
    }
    cameraZoomCaps = null;
  }

  // --- 8b. Camera & Fallback Initialization ---
  function initCameraScanner() {
    const isSecure = window.isSecureContext || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1";
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !isSecure) {
      showCameraFallback("Camera scanning requires HTTPS or localhost. Use photo capture or manual code entry below.");
      return;
    }

    if (typeof Html5Qrcode === "undefined") {
      showCameraFallback("Scanner library not loaded. Use photo capture or manual code entry below.");
      return;
    }

    try {
      html5QrScanner = new Html5Qrcode("qr-reader");
      html5QrScanner.start(
        { facingMode: "environment" },
        { fps: 10, qrbox: { width: 250, height: 250 } },
        (decodedText) => {
          handleScan(decodedText);
        },
        () => {
          // Frame error ignored
        }
      ).then(() => {
        let attempts = 0;
        const checkVideo = setInterval(() => {
          attempts++;
          const video = document.querySelector("#qr-reader video");
          if (video && video.readyState >= 2) {
            clearInterval(checkVideo);
            startAutoZoomEngine(video);
          } else if (attempts > 30) {
            clearInterval(checkVideo);
          }
        }, 100);
      }).catch((err) => {
        showCameraFallback("Camera access was denied or is unavailable. Use photo capture or manual code entry below.");
      });
    } catch (err) {
      showCameraFallback("Could not initialize camera scanner. Use photo capture or manual code entry below.");
    }
  }

  function showCameraFallback(msg) {
    DOM.cameraFallbackMsg.textContent = msg;
    DOM.cameraFallback.classList.remove("hidden");
  }

  // --- 9. Photo Upload Fallback ---
  DOM.uploadPhotoBtn.addEventListener("click", () => {
    DOM.qrFileInput.click();
  });

  DOM.qrFileInput.addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;

    if (typeof Html5Qrcode === "undefined") {
      showResult("NETWORK_ERROR", { message: "Scanner library unavailable" });
      return;
    }

    if (!dummyFileScanner) {
      dummyFileScanner = new Html5Qrcode("qr-reader-dummy");
    }

    dummyFileScanner.scanFile(file, true)
      .then((decodedText) => {
        handleScan(decodedText);
      })
      .catch(() => {
        showResult("NOT_VALID", { message: "No readable QR code found in photo." });
      })
      .finally(() => {
        DOM.qrFileInput.value = "";
      });
  });

  // --- 10. Manual Code Fallback ---
  DOM.manualSubmitBtn.addEventListener("click", () => {
    const val = DOM.manualTicketCode.value.trim();
    if (!val) return;

    // If user enters code directly without URL scheme, wrap it using the accepted verify origin
    let scanTarget = val;
    if (!val.startsWith("http://") && !val.startsWith("https://")) {
      const origin = getAcceptedVerifyOrigins()[0];
      scanTarget = `${origin}/verify/${val}/`;
    }

    handleScan(scanTarget);
    DOM.manualTicketCode.value = "";
  });

  DOM.manualTicketCode.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      DOM.manualSubmitBtn.click();
    }
  });

})();
