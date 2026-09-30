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
      : ["https://reverse-proxy.nexoflow.workers.dev"];
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

      const match = parsedUrl.pathname.match(/^\/verify\/([A-Za-z0-9_-]{16,32})\/?$/);
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
          "X-Gate-Api-Key": gateKey,
        },
        body: JSON.stringify({ ticket_code: ticketCode }),
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
  function unlockScanner() {
    const enteredKey = DOM.gateKeyInput.value.trim();
    if (!enteredKey) {
      DOM.keyEntryError.textContent = "Please enter the gate API key.";
      DOM.keyEntryError.classList.remove("hidden");
      return;
    }

    gateKey = enteredKey;
    DOM.keyEntryError.classList.add("hidden");
    DOM.keyEntryView.classList.add("hidden");
    DOM.scannerView.classList.remove("hidden");

    initCameraScanner();
  }

  function lockScanner() {
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

  // --- 8. Camera & Fallback Initialization ---
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
      ).catch((err) => {
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
