/**
 * DTC ARENA • REAL-TIME SEAT BOOKING & SAGA CONTROLLER
 * =====================================================
 * Production-quality interactive seat map & checkout controller.
 * Features:
 * - Two-phase CAS seat holds with optimistic UI & 10-minute countdown timer.
 * - Non-blocking Saga Coordinator execution with 202 Accepted polling.
 * - Best-effort hold release via navigator.sendBeacon on tab close/unload.
 * - Zero-jank background differential reconciliation engine.
 */

const isLocal = typeof window !== "undefined" && (
  window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1" ||
  window.location.hostname === "0.0.0.0" ||
  window.location.hostname === "::1" ||
  window.location.hostname === "[::1]" ||
  window.location.protocol === "file:" ||
  !window.location.hostname
);

if (isLocal) {
  try {
    localStorage.removeItem("dtc_coordinator_url");
    localStorage.removeItem("dtc_inventory_url");
    localStorage.removeItem("dtc_payment_url");
  } catch (e) {}
}

const localHost = (typeof window !== "undefined" && window.location.hostname && window.location.protocol !== "file:" && window.location.hostname !== "::1" && window.location.hostname !== "[::1]")
  ? window.location.hostname
  : "localhost";
const PROXY_BASE = "https://reverse-proxy.nexoflow.workers.dev";
const defaultCoord = isLocal ? `http://${localHost}:8002` : `${PROXY_BASE}/coordinator`;
const defaultInv = isLocal ? `http://${localHost}:8000` : `${PROXY_BASE}/inventory`;
const defaultPay = isLocal ? `http://${localHost}:8001` : `${PROXY_BASE}/payment`;

// Service Configuration (Local Microservices in Docker / Localhost, Railway in Cloud)
const CONFIG = {
  coordinatorUrl: isLocal ? defaultCoord : (localStorage.getItem("dtc_coordinator_url") || defaultCoord),
  inventoryUrl: isLocal ? defaultInv : (localStorage.getItem("dtc_inventory_url") || defaultInv),
  paymentUrl: isLocal ? defaultPay : (localStorage.getItem("dtc_payment_url") || defaultPay),
  pollIntervalMs: 4000,
};

// Global Reactive Application State
const STATE = {
  eventId: 1,
  eventData: null,
  seatsMap: new Map(), // seatId -> { id, row, seat_number, price, status, held_until, held_by_transaction_id }

  // Authenticated Fan Identity
  user: {
    email: localStorage.getItem("dtc_user_email") || null,
    token: localStorage.getItem("dtc_auth_token") || null,
    userId: localStorage.getItem("dtc_user_id") || null,
  },

  // In-flight action set: Set of seat IDs currently undergoing reserve/release API requests
  inFlightSeatIds: new Set(),

  // Global hold mutex: True whenever a reserve or release network request is in-flight
  isActionInProgress: false,

  // Synchronously tracked pending hold seat ID (set BEFORE any network call)
  pendingHoldSeatId: null,

  // Active user hold: { seatId, txId, heldUntil, timerInterval }
  myHold: null,

  // Active saga checkout: { txId, isPolling, amount }
  activeSaga: null,

  pollTimer: null,
};

// DOM References
const DOM = {
  toastContainer: document.getElementById("toast-container"),
  healthIndicator: document.getElementById("health-indicator"),
  healthText: document.getElementById("health-text"),
  btnRestock: document.getElementById("btn-restock"),

  // User Auth Elements
  btnOpenAuth: document.getElementById("btn-open-auth"),
  userAvatarIcon: document.getElementById("user-avatar-icon"),
  userEmailLabel: document.getElementById("user-email-label"),
  modalAuth: document.getElementById("modal-auth"),
  btnCloseAuth: document.getElementById("btn-close-auth"),
  stepperStep1: document.getElementById("stepper-step-1"),
  stepperStep2: document.getElementById("stepper-step-2"),
  stepperLine1: document.getElementById("stepper-line-1"),
  authStep1: document.getElementById("auth-step-1"),
  authStep2: document.getElementById("auth-step-2"),
  authStep3: document.getElementById("auth-step-3"),
  authEmailInput: document.getElementById("auth-email-input"),
  btnSendOtp: document.getElementById("btn-send-otp"),
  sendOtpBtnText: document.getElementById("send-otp-btn-text"),
  authTargetEmail: document.getElementById("auth-target-email"),
  btnChangeEmail: document.getElementById("btn-change-email"),
  authOtpInput: document.getElementById("auth-otp-input"),
  otpTimerText: document.getElementById("otp-timer-text"),
  otpCountdown: document.getElementById("otp-countdown"),
  btnResendOtp: document.getElementById("btn-resend-otp"),
  btnVerifyOtp: document.getElementById("btn-verify-otp"),
  verifyOtpBtnText: document.getElementById("verify-otp-btn-text"),
  profileEmailDisplay: document.getElementById("profile-email-display"),
  profileUserId: document.getElementById("profile-user-id") || document.getElementById("profile-actor-id"),
  btnCloseAuthDone: document.getElementById("btn-close-auth-done"),
  btnLogout: document.getElementById("btn-logout"),

  eventTitle: document.getElementById("event-title"),
  eventMeta: document.getElementById("event-meta"),
  statAvailable: document.getElementById("stat-available"),
  statHeld: document.getElementById("stat-held"),
  statBooked: document.getElementById("stat-booked"),

  seatSkeleton: document.getElementById("seat-skeleton"),
  seatMatrix: document.getElementById("seat-matrix"),

  holdTimerCard: document.getElementById("hold-timer-card"),
  holdClock: document.getElementById("hold-clock"),
  timerProgressFill: document.getElementById("timer-progress-fill"),
  btnCancelHold: document.getElementById("btn-cancel-hold"),

  summaryEmpty: document.getElementById("summary-empty"),
  summaryActive: document.getElementById("summary-active"),
  summaryTier: document.getElementById("summary-tier"),
  summarySeatLabel: document.getElementById("summary-seat-label"),
  summarySeatTier: document.getElementById("summary-seat-tier"),
  summaryTxId: document.getElementById("summary-tx-id"),
  summaryBasePrice: document.getElementById("summary-base-price"),
  summaryTotalPrice: document.getElementById("summary-total-price"),
  btnProceedPay: document.getElementById("btn-proceed-pay"),

  optHappy: document.getElementById("opt-happy"),
  optDecline: document.getElementById("opt-decline"),

  sagaModal: document.getElementById("saga-modal"),
  modalTitle: document.getElementById("modal-title"),
  modalSubtitle: document.getElementById("modal-subtitle"),
  modalSagaBadge: document.getElementById("modal-saga-badge"),
  step1: document.getElementById("step-1"),
  step1Status: document.getElementById("step-1-status"),
  step2: document.getElementById("step-2"),
  step2Status: document.getElementById("step-2-status"),
  step3: document.getElementById("step-3"),
  step3Title: document.getElementById("step-3-title"),
  step3Desc: document.getElementById("step-3-desc"),
  step3Status: document.getElementById("step-3-status"),
  modalResult: document.getElementById("modal-result"),
  eticketCard: document.getElementById("eticket-card"),
  ticketEventName: document.getElementById("ticket-event-name"),
  ticketSeatNumber: document.getElementById("ticket-seat-number"),
  ticketTxId: document.getElementById("ticket-tx-id"),
  compensationCard: document.getElementById("compensation-card"),
  compReason: document.getElementById("comp-reason"),
  btnModalClose: document.getElementById("btn-modal-close"),
  btnViewForensics: document.getElementById("btn-view-forensics"),

  bottomPayBar: document.getElementById("bottom-pay-bar"),
  btnBottomPay: document.getElementById("btn-bottom-pay"),
  bpQtyText: document.getElementById("bp-qty-text"),
  bpPriceText: document.getElementById("bp-price-text"),
  raceConditionModal: document.getElementById("race-condition-modal"),
  btnRaceRefresh: document.getElementById("btn-race-refresh"),
};

// Tier Metadata Definition
const TIERS = {
  A: { name: "VIP Recliner Club", price: "750.00", basePrice: 750 },
  B: { name: "Premium Balcony", price: "500.00", basePrice: 500 },
  C: { name: "Executive Stalls", price: "350.00", basePrice: 350 },
};

function updateBottomPayBar() {
  if (!DOM.bottomPayBar) DOM.bottomPayBar = document.getElementById("bottom-pay-bar");
  if (!DOM.bpQtyText) DOM.bpQtyText = document.getElementById("bp-qty-text");
  if (!DOM.bpPriceText) DOM.bpPriceText = document.getElementById("bp-price-text");

  if (STATE.myHold) {
    if (DOM.bottomPayBar) {
      DOM.bottomPayBar.classList.remove("show");
      DOM.bottomPayBar.classList.add("hidden");
    }
    return;
  }

  const quantityEl = document.getElementById("seat-quantity");
  const requestedQuantity = quantityEl ? parseInt(quantityEl.value, 10) : 1;
  const draftCount = STATE.draftSeatIds ? STATE.draftSeatIds.length : 0;

  let totalPrice = 0;
  if (STATE.draftSeatIds && STATE.draftSeatIds.length > 0) {
    STATE.draftSeatIds.forEach(id => {
      const s = STATE.seatsMap.get(id);
      if (s) {
        const tierMeta = TIERS[s.row] || { basePrice: 500 };
        totalPrice += s.price ? parseFloat(s.price) : tierMeta.basePrice;
      }
    });
  }

  if (DOM.bpQtyText) {
    DOM.bpQtyText.textContent = `${draftCount}/${requestedQuantity} Seats`;
  }
  if (DOM.bpPriceText) {
    DOM.bpPriceText.textContent = `$${totalPrice % 1 === 0 ? totalPrice.toFixed(0) : totalPrice.toFixed(2)}`;
  }

  if (DOM.bottomPayBar) {
    if (draftCount > 0 && draftCount === requestedQuantity) {
      DOM.bottomPayBar.classList.remove("hidden");
      DOM.bottomPayBar.classList.add("show");
    } else {
      DOM.bottomPayBar.classList.remove("show");
      DOM.bottomPayBar.classList.add("hidden");
    }
  }
}

/* ==========================================================================
   NETWORK & API UTILITIES
   ========================================================================== */

const HOLD_STORAGE_KEY = "dtc_active_seat_hold";

function saveHoldToStorage(hold) {
  if (!hold) {
    clearHoldFromStorage();
    return;
  }
  try {
    const data = {
      seatId: hold.seatId,
      seatIds: hold.seatIds || [hold.seatId],
      seatNumbers: hold.seatNumbers || "",
      txId: hold.txId,
      heldUntil: hold.heldUntil,
      userId: (STATE.user && STATE.user.userId) || null,
      userEmail: (STATE.user && STATE.user.email) || null,
    };
    localStorage.setItem(HOLD_STORAGE_KEY, JSON.stringify(data));
  } catch (e) {}
}

function clearHoldFromStorage() {
  try {
    localStorage.removeItem(HOLD_STORAGE_KEY);
    localStorage.removeItem("dtc_active_seat_hold");
    localStorage.removeItem("dtc_hold_data");
  } catch (e) {}
}

function restoreHoldFromStorage() {
  try {
    const raw = localStorage.getItem(HOLD_STORAGE_KEY) || localStorage.getItem("dtc_hold_data");
    if (!raw) return;
    const data = JSON.parse(raw);
    if (!data || !data.seatId || !data.heldUntil) {
      clearHoldFromStorage();
      return;
    }
    const expiryTime = new Date(data.heldUntil).getTime();
    if (Date.now() >= expiryTime) {
      clearHoldFromStorage();
      return;
    }

    STATE.myHold = {
      seatId: data.seatId,
      seatIds: data.seatIds || [data.seatId],
      seatNumbers: data.seatNumbers || "",
      txId: data.txId,
      heldUntil: data.heldUntil,
      timerInterval: null,
    };

    // Note: Do NOT call startHoldTimer here. loadCatalog() verifies with the database in <100ms.
    // If the seat is genuinely still held by this user, loadCatalog() starts the timer and updates the UI.
    // If the seat was swept or released on exit, loadCatalog() clears STATE.myHold and keeps the timer hidden.
  } catch (e) {
    clearHoldFromStorage();
  }
}

function isTokenExpired(token) {
  if (!token || typeof token !== "string" || !token.includes(".")) return true;
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return true;
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const jsonPayload = decodeURIComponent(
      atob(base64)
        .split("")
        .map((c) => "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2))
        .join("")
    );
    const payload = JSON.parse(jsonPayload);
    if (payload.exp && Date.now() >= payload.exp * 1000) {
      return true;
    }
    return false;
  } catch (e) {
    return false;
  }
}

function isUserAuthenticated(promptIfNeeded = true) {
  if (!STATE.user || !STATE.user.token) {
    if (promptIfNeeded) {
      showAuthModal(1);
      showToast({
        title: "Sign In Required",
        message: "Please sign in with your email OTP to place real-time seat holds.",
        type: "warning",
        duration: 6000,
      });
    }
    return false;
  }
  if (isTokenExpired(STATE.user.token)) {
    handleLogout();
    if (promptIfNeeded) {
      showToast({
        title: "Session Expired",
        message: "Your session has expired. Please sign in again with your email.",
        type: "warning",
        duration: 6000,
      });
    }
    return false;
  }
  return true;
}

async function fetchJson(url, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  const isBodyMethod = ["POST", "PUT", "PATCH"].includes((options.method || "GET").toUpperCase());
  const headers = {
    Accept: "application/json",
    ...(isBodyMethod ? { "Content-Type": "application/json" } : {}),
    ...(options.headers || {}),
  };

  // Inject JWT & User ID / Email footprints if authenticated
  if (!options.skipAuth) {
    if (STATE.user && STATE.user.token) {
      if (isTokenExpired(STATE.user.token)) {
        handleLogout();
      } else {
        headers["Authorization"] = `Bearer ${STATE.user.token}`;
      }
    }
    if (STATE.user && STATE.user.userId) {
      headers["X-User-ID"] = STATE.user.userId;
    }
    if (STATE.user && STATE.user.email) {
      headers["X-User-Email"] = STATE.user.email;
    }
  }

  try {
    const res = await fetch(url, { ...options, signal: controller.signal, headers });
    clearTimeout(timeoutId);
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    clearTimeout(timeoutId);
    return { ok: false, status: 0, error: err.message };
  }
}

/* ==========================================================================
   TOAST SYSTEM (DEDUPLICATED / AGGREGATED WITH ×N COUNTER)
   ========================================================================== */

const activeToasts = new Map();

function showToast({ title, message, type = "normal", icon = "ℹ", duration = 4000 }) {
  if (!DOM.toastContainer) return;

  // Use title + type composite key to aggregate identical prompt categories
  const toastKey = `${type}::${title}`;

  if (activeToasts.has(toastKey)) {
    const existing = activeToasts.get(toastKey);
    if (existing && existing.element && existing.element.parentNode) {
      existing.count += 1;

      // Update message text to the latest seat/context details
      if (existing.descEl) {
        existing.descEl.textContent = message;
      }

      // Update or create multiplier badge (e.g. x2, x3, x4...)
      if (!existing.countBadge) {
        const badge = document.createElement("span");
        badge.className = "toast-count-badge";
        badge.textContent = `x${existing.count}`;
        existing.titleEl.appendChild(badge);
        existing.countBadge = badge;
      } else {
        existing.countBadge.textContent = `x${existing.count}`;
        existing.countBadge.style.animation = "none";
        void existing.countBadge.offsetWidth;
        existing.countBadge.style.animation = "toast-badge-pop 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275)";
      }

      // Re-trigger card pulse bump
      existing.element.style.animation = "none";
      void existing.element.offsetWidth;
      existing.element.style.animation = "toast-bump 0.2s cubic-bezier(0.16, 1, 0.3, 1)";

      // Reset and extend dismissal timer
      clearTimeout(existing.timeoutId);
      existing.timeoutId = setTimeout(() => {
        existing.element.style.opacity = "0";
        existing.element.style.transform = "translateY(10px)";
        existing.element.style.transition = "all 0.3s ease";
        setTimeout(() => {
          if (existing.element.parentNode) existing.element.remove();
          activeToasts.delete(toastKey);
        }, 300);
      }, duration);

      return;
    }
  }

  // Create brand new toast
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icon}</div>
    <div class="toast-content">
      <strong class="toast-title-text">${title}</strong>
      <p class="toast-desc-text">${message}</p>
    </div>
  `;
  DOM.toastContainer.appendChild(toast);

  const strongEl = toast.querySelector(".toast-title-text");
  const descEl = toast.querySelector(".toast-desc-text");

  const entry = {
    element: toast,
    count: 1,
    countBadge: null,
    titleEl: strongEl,
    descEl: descEl,
    timeoutId: null
  };

  activeToasts.set(toastKey, entry);

  entry.timeoutId = setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => {
      if (toast.parentNode) toast.remove();
      activeToasts.delete(toastKey);
    }, 300);
  }, duration);
}

/* ==========================================================================
   FAN IDENTITY & EMAIL OTP AUTHENTICATION
   ========================================================================== */

let otpCountdownTimer = null;

function updateAuthWidgetUI() {
  const token = localStorage.getItem("dtc_auth_token");
  const email = localStorage.getItem("dtc_user_email");
  const userId = localStorage.getItem("dtc_user_id");

  STATE.user = { token, email, userId };

  if (token && email) {
    if (DOM.userEmailLabel) DOM.userEmailLabel.textContent = email;
    if (DOM.userAvatarIcon) DOM.userAvatarIcon.textContent = "🟢";
    if (DOM.btnOpenAuth) {
      DOM.btnOpenAuth.style.borderColor = "rgba(34, 197, 94, 0.4)";
      DOM.btnOpenAuth.style.background = "rgba(34, 197, 94, 0.1)";
      DOM.btnOpenAuth.style.color = "#22c55e";
    }
  } else {
    if (DOM.userEmailLabel) DOM.userEmailLabel.textContent = "Email Login";
    if (DOM.userAvatarIcon) DOM.userAvatarIcon.textContent = "✦︎";
    if (DOM.btnOpenAuth) {
      DOM.btnOpenAuth.style.borderColor = "rgba(56, 189, 248, 0.4)";
      DOM.btnOpenAuth.style.background = "rgba(56, 189, 248, 0.1)";
      DOM.btnOpenAuth.style.color = "#38bdf8";
    }
  }
}

function showAuthModal(step = 1) {
  if (!DOM.modalAuth) return;
  DOM.modalAuth.classList.remove("hidden");
  if (STATE.user && STATE.user.token) {
    renderAuthStep(3);
  } else {
    renderAuthStep(step);
  }
}

function closeAuthModal() {
  if (!DOM.modalAuth) return;
  DOM.modalAuth.classList.add("hidden");
  if (otpCountdownTimer) clearInterval(otpCountdownTimer);
}

function renderAuthStep(step) {
  const s1 = DOM.stepperStep1;
  const s2 = DOM.stepperStep2;
  const l1 = DOM.stepperLine1;

  if (s1) s1.className = step === 1 ? "step-item active" : step > 1 ? "step-item completed" : "step-item";
  if (s2) s2.className = step === 2 ? "step-item active" : step > 2 ? "step-item completed" : "step-item";
  if (l1) l1.className = step >= 2 ? "step-line active" : "step-line";

  [DOM.authStep1, DOM.authStep2, DOM.authStep3].forEach(p => {
    if (p) p.classList.add("hidden");
  });

  if (step === 1 && DOM.authStep1) {
    DOM.authStep1.classList.remove("hidden");
    if (DOM.authEmailInput) setTimeout(() => DOM.authEmailInput.focus(), 100);
  } else if (step === 2 && DOM.authStep2) {
    DOM.authStep2.classList.remove("hidden");
    if (DOM.authOtpInput) {
      if (!isLocal) {
        DOM.authOtpInput.value = "";
      }
      setTimeout(() => DOM.authOtpInput.focus(), 100);
    }
  } else if (step === 3 && DOM.authStep3) {
    DOM.authStep3.classList.remove("hidden");
    if (DOM.profileEmailDisplay && STATE.user) {
      DOM.profileEmailDisplay.textContent = STATE.user.email;
    }
    if (DOM.profileUserId && STATE.user) {
      DOM.profileUserId.textContent = STATE.user.userId || `usr_${STATE.user.email}`;
    }
  }
}

function startOtpCountdown(seconds = 30) {
  if (isLocal) {
    // LOCAL DEV: Infinite immediate resends with no countdown wait
    if (DOM.otpTimerText) DOM.otpTimerText.classList.add("hidden");
    if (DOM.btnResendOtp) {
      DOM.btnResendOtp.classList.remove("hidden");
      DOM.btnResendOtp.disabled = false;
      DOM.btnResendOtp.textContent = "Resend Code ↺ (Infinite Dev Mode)";
    }
    return;
  }

  let remaining = seconds;
  if (DOM.otpTimerText) DOM.otpTimerText.classList.remove("hidden");
  if (DOM.btnResendOtp) DOM.btnResendOtp.classList.add("hidden");
  if (DOM.otpCountdown) DOM.otpCountdown.textContent = `${remaining}s`;

  if (otpCountdownTimer) clearInterval(otpCountdownTimer);
  otpCountdownTimer = setInterval(() => {
    remaining -= 1;
    if (DOM.otpCountdown) DOM.otpCountdown.textContent = `${remaining}s`;
    if (remaining <= 0) {
      clearInterval(otpCountdownTimer);
      if (DOM.otpTimerText) DOM.otpTimerText.classList.add("hidden");
      if (DOM.btnResendOtp) DOM.btnResendOtp.classList.remove("hidden");
    }
  }, 1000);
}

function setupAuthEventListeners() {
  if (DOM.btnOpenAuth) DOM.btnOpenAuth.addEventListener("click", () => showAuthModal(1));
  if (DOM.btnCloseAuth) DOM.btnCloseAuth.addEventListener("click", closeAuthModal);
  if (DOM.btnCloseAuthDone) DOM.btnCloseAuthDone.addEventListener("click", closeAuthModal);
  if (DOM.modalAuth) {
    DOM.modalAuth.addEventListener("click", (e) => {
      if (e.target === DOM.modalAuth) closeAuthModal();
    });
  }

  // Step 1: Send OTP
  if (DOM.btnSendOtp) DOM.btnSendOtp.addEventListener("click", handleSendOtp);
  if (DOM.authEmailInput) {
    DOM.authEmailInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleSendOtp();
    });
  }

  // Step 2: Verify OTP
  if (DOM.btnVerifyOtp) DOM.btnVerifyOtp.addEventListener("click", handleVerifyOtp);
  if (DOM.authOtpInput) {
    DOM.authOtpInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleVerifyOtp();
    });
  }
  if (DOM.btnChangeEmail) DOM.btnChangeEmail.addEventListener("click", () => renderAuthStep(1));
  if (DOM.btnResendOtp) DOM.btnResendOtp.addEventListener("click", handleSendOtp);

  // Logout
  if (DOM.btnLogout) DOM.btnLogout.addEventListener("click", handleLogout);
}

async function handleSendOtp() {
  const email = (DOM.authEmailInput?.value || "").trim();
  if (!email || !email.includes("@")) {
    showToast({ title: "Invalid Email", message: "Please enter a valid email address.", type: "warning" });
    return;
  }

  if (DOM.btnSendOtp) DOM.btnSendOtp.disabled = true;
  if (DOM.sendOtpBtnText) DOM.sendOtpBtnText.textContent = "Sending Verification Code...";

  try {
    const res = await fetchJson(`${CONFIG.coordinatorUrl}/auth/send-otp/`, {
      method: "POST",
      skipAuth: true,
      body: JSON.stringify({ email }),
    });

    if (res.ok && res.data && res.data.success) {
      renderAuthStep(2);
      startOtpCountdown(res.data.cooldown_remaining || 30);

      const chancesText = (isLocal || res.data.chances_remaining === "∞")
        ? "\n(Local Dev: Infinite Resends Available)"
        : (res.data.chances_remaining !== undefined ? `\n(${10 - res.data.chances_remaining}/10 daily requests used — ${res.data.chances_remaining} remaining today)` : "");

      const resolvedEmail = (res.data && res.data.user_email) || email;

      if (isLocal && res.data.otp) {
        // LOCAL DEVELOPMENT ONLY: Display badge & auto-fill code
        if (DOM.authTargetEmail) {
          DOM.authTargetEmail.innerHTML = `${resolvedEmail}<br><span style="display:inline-block;margin-top:8px;padding:6px 12px;background:rgba(56,189,248,0.15);border:1px dashed #38bdf8;border-radius:6px;font-size:0.95rem;color:#38bdf8;font-weight:600;">Local Dev OTP: <strong style="letter-spacing:3px;font-size:1.15rem;color:#ffffff;text-shadow:0 0 8px #38bdf8;">${res.data.otp}</strong> <span style="font-size:0.8rem;color:#a5f3fc;">(Infinite Resend Mode)</span></span>`;
        }
        if (DOM.authOtpInput) {
          DOM.authOtpInput.value = res.data.otp;
        }
        showToast({
          title: `Local OTP: ${res.data.otp}`,
          message: `[LOCAL DEV] Simulated verification code: ${res.data.otp} (auto-filled in window)` + chancesText,
          type: "success",
          duration: 9000,
        });
      } else {
        // LIVE PRODUCTION: Normal workflow, no autofill, manual entry required
        if (DOM.authTargetEmail) {
          DOM.authTargetEmail.textContent = resolvedEmail;
        }
        if (DOM.authOtpInput) {
          DOM.authOtpInput.value = "";
        }
        const isCooldown = Boolean(res.data.is_cooldown) || (res.data.message && res.data.message.includes("recently sent"));
        const title = isCooldown ? "Code Already Sent!" : "Verification Code Sent!";
        const messageBody = isCooldown 
          ? `An active verification code was already sent to ${resolvedEmail}. Please check your inbox.`
          : `6-Digit verification code sent to ${resolvedEmail}. Please check your inbox.`;

        showToast({
          title: title,
          message: messageBody + chancesText,
          type: "success",
          duration: 8000,
        });
      }
    } else if (res.status === 409 || (res.data && res.data.code === "CONCURRENT_LOGIN_BLOCKED")) {
      showToast({
        title: "Device Already Active",
        message: (res.data && res.data.message) || "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
        type: "warning",
        duration: 9000,
      });
    } else if (res.status === 429) {
      showToast({
        title: "Account Blocked",
        message: (res.data && res.data.error) || "Too many OTP requests. Please try again later.",
        type: "danger",
      });
    } else {
      showToast({
        title: "Failed to Send OTP",
        message: (res.data && res.data.error) || "Could not dispatch verification email.",
        type: "danger",
      });
    }
  } catch (err) {
    showToast({ title: "Network Error", message: err.message, type: "danger" });
  } finally {
    if (DOM.btnSendOtp) DOM.btnSendOtp.disabled = false;
    if (DOM.sendOtpBtnText) DOM.sendOtpBtnText.textContent = "Send Verification Code (OTP) ➔";
  }
}

async function handleVerifyOtp() {
  const email = (DOM.authEmailInput?.value || STATE.user.email || "").trim();
  const otp = (DOM.authOtpInput?.value || "").trim();

  if (!otp || otp.length < 6) {
    showToast({ title: "Invalid Code", message: "Please enter the 6-digit code sent to your email.", type: "warning" });
    return;
  }

  if (DOM.btnVerifyOtp) DOM.btnVerifyOtp.disabled = true;
  if (DOM.verifyOtpBtnText) DOM.verifyOtpBtnText.textContent = "Verifying Code...";

  try {
    const res = await fetchJson(`${CONFIG.coordinatorUrl}/auth/verify-otp/`, {
      method: "POST",
      skipAuth: true,
      body: JSON.stringify({ email, otp }),
    });

    if (res.ok && res.data.success) {
      const { token, user_id, user_email, email: resEmail } = res.data;
      const finalUserId = user_id;
      const finalEmail = user_email || resEmail || email;
      localStorage.setItem("dtc_auth_token", token);
      localStorage.setItem("dtc_user_email", finalEmail);
      localStorage.setItem("dtc_user_id", finalUserId);

      STATE.user = { token, email: finalEmail, userId: finalUserId };
      updateAuthWidgetUI();

      showToast({
        title: "Signed In Successfully",
        message: `Welcome! User ID: ${finalUserId}. Hold cap: Max 10 seats.`,
        type: "success",
        duration: 5000,
      });

      renderAuthStep(3);
    } else if (res.status === 409 || (res.data && res.data.code === "CONCURRENT_LOGIN_BLOCKED")) {
      showToast({
        title: "Device Already Active",
        message: (res.data && res.data.message) || "Another device is currently logged in with this email. Please sign out from that device first or wait for the session to expire.",
        type: "warning",
        duration: 9000,
      });
    } else {
      showToast({
        title: "Verification Failed",
        message: (res.data && res.data.error) || "Invalid or expired verification code.",
        type: "danger",
      });
    }
  } catch (err) {
    showToast({ title: "Network Error", message: err.message, type: "danger" });
  } finally {
    if (DOM.btnVerifyOtp) DOM.btnVerifyOtp.disabled = false;
    if (DOM.verifyOtpBtnText) DOM.verifyOtpBtnText.textContent = "Verify & Sign In ➔";
  }
}

function handleLogout(opts = {}) {
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    fetch(`${CONFIG.coordinatorUrl}/auth/logout/`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` }
    }).catch(() => {});
  }
  localStorage.removeItem("dtc_auth_token");
  localStorage.removeItem("dtc_user_email");
  localStorage.removeItem("dtc_user_id");

  STATE.user = { token: null, email: null, userId: null };
  updateAuthWidgetUI();
  closeAuthModal();

  // Clear unreserved draft seat selection
  if (STATE.draftSeatIds && STATE.draftSeatIds.length > 0) {
    const drafted = [...STATE.draftSeatIds];
    STATE.draftSeatIds = [];
    STATE.draftSeatLabels = [];
    drafted.forEach(id => {
      const seat = STATE.seatsMap.get(id);
      applySeatTileDOMState(id, seat ? seat.status : "AVAILABLE");
    });
    updateBottomPayBar();
  }

  // Release active hold on inventory service immediately if user was holding seats
  const holdDataRaw = localStorage.getItem(HOLD_STORAGE_KEY) || localStorage.getItem("dtc_hold_data");
  let holdData = STATE.myHold;
  if (!holdData && holdDataRaw) {
    try { holdData = JSON.parse(holdDataRaw); } catch(e) {}
  }
  if (holdData && (holdData.seatIds || holdData.seatId) && holdData.txId) {
    const targetIds = holdData.seatIds || [holdData.seatId];
    fetch(`${CONFIG.inventoryUrl}/seats/release/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transaction_id: holdData.txId, seat_ids: targetIds }),
      keepalive: true,
    }).catch(() => {});
    targetIds.forEach(id => applySeatTileDOMState(id, "AVAILABLE"));
  }

  if (STATE.myHold) {
    if (STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
    STATE.myHold = null;
  }
  clearHoldFromStorage();
  hideHoldTimer();
  updateCheckoutSummary();
  updateMatrixHoldState();
  loadCatalog(false);

  showToast({
    title: opts.title || "Signed Out",
    message: opts.message || "Identity detached. Live seat reservations will require signing in again.",
    type: opts.type || "info",
    duration: 6000,
  });

  // Prompt the user to sign back in
  setTimeout(() => {
    showAuthModal(1);
  }, 400);
}

/* ==========================================================================
   INITIALIZATION & BOOTSTRAP
   ========================================================================== */

// Session Heartbeat: executed once on page refresh/boot (no background polling/spam)
function initSessionHeartbeat() {
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    if (isTokenExpired(token)) {
      handleLogout();
      return;
    }
    fetch(`${CONFIG.coordinatorUrl}/auth/heartbeat/`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` }
    }).then(async res => {
      if (res.status === 401 || res.status === 403) {
        let isSuperseded = false;
        try {
          const errData = await res.json();
          if (errData && errData.code === "SESSION_SUPERSEDED") isSuperseded = true;
        } catch(e) {}
        handleLogout({
          title: isSuperseded ? "Signed Out on Another Device" : "Session Expired",
          message: isSuperseded
            ? "Your account was logged into on another device. You have been signed out on this device."
            : "Your session has expired. Please sign in again.",
          type: "warning"
        });
      }
    }).catch(() => {});
  }
}
initSessionHeartbeat();

// Tab Close / Browser Exit Cleanup (Releases Held Seats & Session Immediately)
function handleTabExitCleanup() {
  // 1. Release active hold on Inventory Service immediately via keepalive
  const holdDataRaw = localStorage.getItem(HOLD_STORAGE_KEY) || localStorage.getItem("dtc_hold_data");
  let holdData = STATE.myHold;
  if (!holdData && holdDataRaw) {
    try { holdData = JSON.parse(holdDataRaw); } catch(e) {}
  }
  if (holdData && (holdData.seatIds || holdData.seatId) && holdData.txId) {
    const targetIds = holdData.seatIds || [holdData.seatId];
    fetch(`${CONFIG.inventoryUrl}/seats/release/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transaction_id: holdData.txId, seat_ids: targetIds }),
      keepalive: true
    }).catch(() => {});
  }
  clearHoldFromStorage();

  // 2. Clear coordinator session
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    fetch(`${CONFIG.coordinatorUrl}/auth/logout/`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` },
      keepalive: true
    }).catch(() => {});
  }
}

window.addEventListener("beforeunload", handleTabExitCleanup);
window.addEventListener("pagehide", handleTabExitCleanup);

document.addEventListener("DOMContentLoaded", () => {
  initEntryScreen();
  if (STATE.user && STATE.user.token && isTokenExpired(STATE.user.token)) {
    handleLogout();
  }
  setupAuthEventListeners();
  updateAuthWidgetUI();
  restoreHoldFromStorage();
  initEventListeners();
  loadCatalog(true);
  setupBeaconUnloadHandler();
});

/* ==========================================================================
   DTC LIQUID ENTRY SCREEN ANIMATION
   ========================================================================== */
let entryAnimId = null;
let entryProgress = 0;
let isDataReady = false;

function initEntryScreen() {
  const overlay = document.getElementById("dtc-entry-screen");
  const canvas = document.getElementById("dtc-entry-canvas");
  const pctLabel = document.getElementById("dtc-entry-pct");
  const skipBtn = document.getElementById("btn-skip-entry");
  if (!overlay || !canvas || !pctLabel) return;

  const ctx = canvas.getContext("2d");
  const offCanvas = document.createElement("canvas");
  const offCtx = offCanvas.getContext("2d");
  let dpr = window.devicePixelRatio || 1;

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    offCanvas.width = canvas.width;
    offCanvas.height = canvas.height;
  }
  window.addEventListener("resize", resizeCanvas);
  resizeCanvas();

  const TOTAL_DURATION_MS = 4200; // Smooth 4.2s mandatory watch window
  const HOLD_DURATION_MS = 500;   // Hold fully filled letters at 100%
  let waveTime = 0;
  let startTime = null;
  let hasDismissed = false;
  let isHolding = false;

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function dismiss() {
    if (hasDismissed) return;
    hasDismissed = true;
    overlay.classList.add("fade-out");
    setTimeout(() => {
      if (entryAnimId) cancelAnimationFrame(entryAnimId);
      overlay.style.display = "none";
    }, 750);
  }

  if (skipBtn) {
    skipBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dismiss();
    });
  }

  function render(now) {
    if (!startTime) startTime = now;
    waveTime += 0.034;
    const elapsed = now - startTime;

    if (!isHolding) {
      let timeProgress = Math.min(1, elapsed / TOTAL_DURATION_MS);

      // If backend cold-start takes longer than 4.2s, pause gracefully at 94% until ready
      if (timeProgress > 0.94 && !isDataReady) {
        timeProgress = 0.94;
      }

      const eased = easeInOutCubic(timeProgress);
      entryProgress = eased * 100;
      pctLabel.textContent = `${Math.min(100, Math.floor(entryProgress))} %`;

      if (timeProgress >= 1) {
        entryProgress = 100;
        pctLabel.textContent = "100 %";
        isHolding = true;
        setTimeout(dismiss, HOLD_DURATION_MS);
      }
    }

    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    offCtx.clearRect(0, 0, w, h);

    const cw = w / dpr;
    const ch = h / dpr;

    ctx.save();
    ctx.scale(dpr, dpr);

    const text = "DTC";
    const fontSize = Math.min(cw * 0.38, 140);
    ctx.font = `900 ${fontSize}px "Montserrat", system-ui, -apple-system, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    const centerX = cw / 2;
    const centerY = ch / 2 - 4;

    // 1. Ghost / Silhouette Background Text
    ctx.fillStyle = "rgba(255, 255, 255, 0.16)";
    ctx.fillText(text, centerX, centerY);

    // 2. Offscreen Wave Mask for Liquid Text Fill
    const textHeight = fontSize * 0.82;
    const fillRange = textHeight + 16;
    const waterY = (centerY + textHeight / 2 + 8) - (entryProgress / 100) * fillRange;

    offCtx.save();
    offCtx.scale(dpr, dpr);

    offCtx.fillStyle = "#ffffff";
    offCtx.font = ctx.font;
    offCtx.textAlign = "center";
    offCtx.textBaseline = "middle";
    offCtx.fillText(text, centerX, centerY);

    offCtx.globalCompositeOperation = "source-in";

    // Secondary Wave (Deeper, darker liquid layer with subtle opacity)
    offCtx.save();
    offCtx.beginPath();
    offCtx.moveTo(0, ch);
    const amp1 = 7.0;
    const freq1 = 0.020;
    for (let x = 0; x <= cw; x += 3) {
      const y = waterY + Math.sin(x * freq1 - waveTime * 1.1) * amp1 - 2;
      offCtx.lineTo(x, y);
    }
    offCtx.lineTo(cw, ch);
    offCtx.closePath();
    offCtx.fillStyle = "#ffffff";
    offCtx.globalAlpha = 0.42;
    offCtx.fill();
    offCtx.restore();

    // Primary Wave (Surface liquid crests)
    offCtx.save();
    offCtx.beginPath();
    offCtx.moveTo(0, ch);
    const amp2 = 5.5;
    const freq2 = 0.026;
    for (let x = 0; x <= cw; x += 3) {
      const y = waterY + Math.cos(x * freq2 + waveTime * 1.4) * amp2;
      offCtx.lineTo(x, y);
    }
    offCtx.lineTo(cw, ch);
    offCtx.closePath();
    offCtx.fillStyle = "#ffffff";
    offCtx.globalAlpha = 1.0;
    offCtx.fill();
    offCtx.restore();

    offCtx.restore();

    // Draw liquid back to main canvas
    ctx.drawImage(offCanvas, 0, 0, cw, ch);
    ctx.restore();

    if (!hasDismissed) {
      entryAnimId = requestAnimationFrame(render);
    }
  }

  entryAnimId = requestAnimationFrame(render);

  // Safety fallback: Ensure data is marked ready after 8s max
  setTimeout(() => {
    finishEntryScreen();
  }, 8000);
}

function finishEntryScreen() {
  isDataReady = true;
}

function initEventListeners() {
  // Reset Venue Seats Button
  if (DOM.btnRestock) {
    DOM.btnRestock.addEventListener("click", resetVenueSeats);
  }

  // Cancel / Release Active Hold
  if (DOM.btnCancelHold) {
    DOM.btnCancelHold.addEventListener("click", releaseActiveHold);
  }

  // Seat Quantity Change
  const quantityEl = document.getElementById("seat-quantity");
  if (quantityEl) {
    quantityEl.addEventListener("change", (e) => {
      let requested = parseInt(e.target.value, 10);
      const totalAvailable = Array.from(STATE.seatsMap.values()).filter(s => s.status === "AVAILABLE" || (s.status === "HELD" && s.held_until && new Date(s.held_until) < new Date())).length;
      
      if (requested > totalAvailable && totalAvailable > 0) {
        showToast({
          title: "Limited Availability",
          message: `Only ${totalAvailable} seat(s) are available to book right now. Quantity automatically adjusted.`,
          type: "warning",
          duration: 4500
        });
        e.target.value = totalAvailable;
        requested = totalAvailable;
      }

      if (STATE.myHold) {
        updateCheckoutSummary();
      } else if (STATE.draftSeatIds && STATE.draftSeatIds.length > 0) {
        // They changed quantity while having an unreserved draft.
        // Wipe the draft so they can start fresh and we don't get stuck in a broken state.
        const drafted = [...STATE.draftSeatIds];
        STATE.draftSeatIds = [];
        STATE.draftSeatLabels = [];
        drafted.forEach(id => {
           const seat = STATE.seatsMap.get(id);
           applySeatTileDOMState(id, seat ? seat.status : "AVAILABLE");
        });
        updateBottomPayBar();
        showToast({
          title: "Selection Reset",
          message: "You changed the seat quantity. Your pending selection has been cleared. Please pick your seats again.",
          type: "info",
          duration: 3000
        });
      } else {
        updateBottomPayBar();
      }
    });
  }

  // Proceed to Payment CTA
  if (DOM.btnProceedPay) {
    DOM.btnProceedPay.addEventListener("click", initiateSagaCheckout);
  }

  // Bottom Pay Bar
  const btnBottomPay = document.getElementById("btn-bottom-pay");
  if (btnBottomPay) {
    btnBottomPay.addEventListener("click", handleBottomPaySubmit);
  }

  // Race Condition Modal Refresh
  const btnRaceRefresh = document.getElementById("btn-race-refresh");
  if (btnRaceRefresh) {
    btnRaceRefresh.addEventListener("click", () => {
      const raceModal = document.getElementById("race-condition-modal");
      if (raceModal) raceModal.classList.remove("show");
      
      STATE.draftSeatIds = [];
      STATE.draftSeatLabels = [];
      updateBottomPayBar();
      
      loadCatalog(false);
    });
  }

  // Simulation Option Radio Buttons
  const radioInputs = document.querySelectorAll('input[name="pay_sim"]');
  radioInputs.forEach(radio => {
    radio.addEventListener("change", (e) => {
      if (e.target.value === "500") {
        DOM.optHappy.classList.add("selected");
        DOM.optDecline.classList.remove("selected");
      } else {
        DOM.optHappy.classList.remove("selected");
        DOM.optDecline.classList.add("selected");
      }
    });
  });

  // Modal Close Button
  if (DOM.btnModalClose) {
    DOM.btnModalClose.addEventListener("click", () => {
      DOM.sagaModal.classList.add("hidden");
      loadCatalog(false);
    });
  }
}

/* ==========================================================================
   CATALOG LOADING & INITIAL RENDER
   ========================================================================== */

async function loadCatalog(isInitial = false) {
  try {
    // Single consolidated fetch: /events/ already includes event metadata AND all nested seats!
    const eventsRes = await fetchJson(`${CONFIG.inventoryUrl}/events/`);

    if (!eventsRes.ok || !Array.isArray(eventsRes.data) || eventsRes.data.length === 0) {
      if (DOM.healthText) DOM.healthText.textContent = "Inventory: Offline";
      if (DOM.healthIndicator) {
        const ind = DOM.healthIndicator.querySelector(".status-indicator");
        if (ind) ind.style.background = "#ef4444";
      }
      if (isInitial) {
        finishEntryScreen();
        setTimeout(() => loadCatalog(true), 1500);
      }
      return;
    }

    if (DOM.healthText) DOM.healthText.textContent = "Inventory: Online";
    if (DOM.healthIndicator) {
      const ind = DOM.healthIndicator.querySelector(".status-indicator");
      if (ind) ind.style.background = "var(--color-available)";
    }

    // Update Event Metadata
    const ev = eventsRes.data[0];
    STATE.eventData = ev;
    if (DOM.eventTitle) DOM.eventTitle.textContent = ev.name;
    if (DOM.eventMeta) {
      DOM.eventMeta.innerHTML = `
        <span>📍 ${ev.venue || "Grand Arena Stadium"}</span>
        <span class="meta-sep">•</span>
        <span>📅 Tomorrow, 8:00 PM</span>
        <span class="meta-sep">•</span>
        <span>⏱️ 10-Min Ephemeral Holds</span>
        <span class="meta-sep"></span>
        <span style="color: #38bdf8; font-weight: 600;"></span>
      `;
    }

    // Extract nested seats directly from the event payload (zero extra HTTP calls)
    let seatsList = Array.isArray(ev.seats) ? ev.seats : [];

    // If no seats exist in database yet, automatically trigger seed restock
    if (seatsList.length === 0) {
      const seedRes = await fetchJson(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
      if (seedRes.ok) {
        const refreshedEvents = await fetchJson(`${CONFIG.inventoryUrl}/events/`);
        if (refreshedEvents.ok && Array.isArray(refreshedEvents.data) && refreshedEvents.data[0]) {
          seatsList = refreshedEvents.data[0].seats || [];
        }
      }
    }

    // Populate Seats Map
    let ownerTxId = STATE.myHold ? STATE.myHold.txId : null;
    let ownerHeldUntil = STATE.myHold ? STATE.myHold.heldUntil : null;
    const ownerSeatIds = [];
    const ownerSeatLabels = [];

    seatsList.forEach(seat => {
      STATE.seatsMap.set(seat.id, seat);
      
      if (STATE.user && (STATE.user.email || STATE.user.userId)) {
        if (seat.status === "HELD") {
          const isOwner = (seat.user_email && STATE.user.email && seat.user_email === STATE.user.email) || 
                          (seat.user_id && STATE.user.userId && seat.user_id === STATE.user.userId) ||
                          (STATE.myHold && seat.held_by_transaction_id === STATE.myHold.txId);
          if (isOwner) {
            if (!ownerTxId) ownerTxId = seat.held_by_transaction_id;
            if (!ownerHeldUntil) ownerHeldUntil = seat.held_until;
            
            if (seat.held_by_transaction_id === ownerTxId) {
              ownerSeatIds.push(seat.id);
              ownerSeatLabels.push(`${seat.row}${seat.seat_number}`);
            }
          }
        }
      }
    });

    if (ownerSeatIds.length > 0) {
      const currentIds = STATE.myHold && STATE.myHold.seatIds ? STATE.myHold.seatIds : [];
      if (!STATE.myHold || currentIds.length !== ownerSeatIds.length || STATE.myHold.txId !== ownerTxId) {
        STATE.myHold = {
          seatId: ownerSeatIds[0],
          seatIds: ownerSeatIds,
          seatNumbers: ownerSeatLabels.join(" - "),
          txId: ownerTxId,
          heldUntil: ownerHeldUntil,
          timerInterval: STATE.myHold ? STATE.myHold.timerInterval : null
        };
        saveHoldToStorage(STATE.myHold);
      }
      if (!STATE.myHold.timerInterval) {
        startHoldTimer(ownerHeldUntil);
      }
      updateCheckoutSummary();
      updateMatrixHoldState();
    } else {
      // Database ground truth: User has ZERO active holds.
      // Clean up any stale local hold object, clear storage, and ensure timer is hidden.
      if (STATE.myHold) {
        if (STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
        STATE.myHold = null;
      }
      clearHoldFromStorage();
      hideHoldTimer();
      updateCheckoutSummary();
      updateMatrixHoldState();
    }

    if (isInitial || !DOM.seatMatrix || DOM.seatMatrix.children.length === 0) {
      renderFullSeatMatrix();
      if (DOM.seatSkeleton) DOM.seatSkeleton.classList.add("hidden");
    } else {
      reconcileSeatStates(seatsList);
    }

    updateInventoryStats();
    if (isInitial) {
      finishEntryScreen();
    }
  } catch (err) {
    console.error("loadCatalog error:", err);
    if (isInitial) {
      finishEntryScreen();
      setTimeout(() => loadCatalog(true), 2000);
    }
  }
}

/* ==========================================================================
   FULL SEAT MATRIX RENDERING
   ========================================================================== */

function renderFullSeatMatrix() {
  if (!DOM.seatMatrix) return;
  DOM.seatMatrix.innerHTML = "";

  // Group seats by Row (A, B, C)
  const rowsMap = new Map();
  STATE.seatsMap.forEach(seat => {
    const r = seat.row || "A";
    if (!rowsMap.has(r)) rowsMap.set(r, []);
    rowsMap.get(r).push(seat);
  });

  // Sort rows and seat numbers
  const sortedRows = Array.from(rowsMap.keys()).sort();

  sortedRows.forEach(rowKey => {
    const seatsInRow = rowsMap.get(rowKey).sort((a, b) => parseInt(a.seat_number) - parseInt(b.seat_number));
    const tierMeta = TIERS[rowKey] || { name: `Row ${rowKey} Section`, price: "500.00" };

    const tierSection = document.createElement("div");
    tierSection.className = "seat-tier-section";

    tierSection.innerHTML = `
      <div class="tier-header">
        <span class="tier-name">${tierMeta.name}</span>
        <span class="tier-price-tag">$${tierMeta.price}</span>
      </div>
      <div class="seat-row">
        <span class="row-label">${rowKey}</span>
        <div class="seats-list" id="row-${rowKey}-seats"></div>
      </div>
    `;

    const seatsList = tierSection.querySelector(`#row-${rowKey}-seats`);

    seatsInRow.forEach(seat => {
      const tile = createSeatTile(seat);
      seatsList.appendChild(tile);
    });

    DOM.seatMatrix.appendChild(tierSection);
  });

  updateMatrixHoldState();
}

function createSeatTile(seat) {
  const tile = document.createElement("div");
  tile.className = "seat-tile";
  tile.id = `seat-tile-${seat.id}`;
  tile.dataset.seatId = seat.id;

  // Determine initial status
  let effectiveStatus = seat.status;
  if (STATE.myHold && STATE.myHold.seatIds && STATE.myHold.seatIds.includes(seat.id)) {
    effectiveStatus = "SELECTED";
  } else if (STATE.myHold && STATE.myHold.seatId === seat.id) {
    effectiveStatus = "SELECTED";
  } else if (STATE.draftSeatIds && STATE.draftSeatIds.includes(seat.id)) {
    effectiveStatus = "SELECTED";
  }

  tile.dataset.status = effectiveStatus;
  tile.innerHTML = `
    <span class="seat-num">${seat.row}${seat.seat_number}</span>
    <span class="seat-icon">${getSeatStatusIcon(effectiveStatus)}</span>
  `;

  tile.addEventListener("click", () => handleSeatTileClick(seat.id));
  return tile;
}

function getSeatStatusIcon(status) {
  if (status === "SELECTED") return "✓";
  if (status === "BOOKED") return "✕";
  return "•";
}

/* ==========================================================================
   OPTIMISTIC SELECTION & CAS HOLD ORCHESTRATION
   ========================================================================== */

async function handleSeatTileClick(seatId) {
  const seat = STATE.seatsMap.get(seatId);
  if (!seat) return;

  // 0. Enforce Authentication Requirement for Live Holds
  if (!isUserAuthenticated(true)) {
    return;
  }

  // 1. Guard against rapid click during an active API call
  if (STATE.isActionInProgress) {
    return;
  }

  // 2. If clicking own already-held seat -> release hold
  if (STATE.myHold && STATE.myHold.seatIds && STATE.myHold.seatIds.includes(seatId)) {
    await releaseActiveHold();
    return;
  }

  // 3. If clicking a BOOKED seat (Sold Out)
  if (seat.status === "BOOKED") {
    const tile = document.getElementById(`seat-tile-${seatId}`);
    if (tile) {
      tile.style.animation = "shake 0.4s ease-in-out";
      setTimeout(() => (tile.style.animation = ""), 400);
    }
    showToast({
      title: "Seat Already Booked",
      message: `You cannot reserve Seat ${seat.row}${seat.seat_number} because it is already booked.`,
      type: "danger",
      icon: "✕",
      duration: 4000,
    });
    return;
  }

  // 4. If clicking a seat HELD by another user or an orphaned hold
  if (seat.status === "HELD") {
    const isMyHold = STATE.myHold && STATE.myHold.seatIds && STATE.myHold.seatIds.includes(seatId);
    
    // Check if it belongs to current user but wasn't reconstructed yet
    const isOwner = STATE.user && (
      (seat.user_email && STATE.user.email && seat.user_email === STATE.user.email) || 
      (seat.user_id && STATE.user.userId && seat.user_id === STATE.user.userId)
    );

    if (isOwner && !isMyHold) {
      // Force a catalog load which will reconstruct the hold properly, then return
      loadCatalog(false);
      return;
    }

    if (!isMyHold) {
      const tile = document.getElementById(`seat-tile-${seatId}`);
      if (tile) {
        tile.style.animation = "shake 0.4s ease-in-out";
        setTimeout(() => (tile.style.animation = ""), 400);
      }
      
      let timeMsg = "Please select an available seat.";
      if (seat.held_until) {
        const expiryDate = new Date(seat.held_until);
        const remainingMs = Math.max(0, expiryDate - Date.now());
        if (remainingMs > 0) {
          const mins = Math.floor(remainingMs / 60000);
          const secs = Math.floor((remainingMs % 60000) / 1000);
          const timeString = expiryDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          timeMsg = `This reservation expires in ${mins}m ${secs}s (at ${timeString}).`;
        } else {
          timeMsg = `This reservation is expiring soon.`;
        }
      }

      showToast({
        title: "Seat Already Held",
        message: `Seat ${seat.row}${seat.seat_number} is currently held by another user. ${timeMsg}`,
        type: "warning",
        icon: "",
        duration: 5000,
      });
      return;
    }
  }

  const quantityEl = document.getElementById("seat-quantity");
  const requestedQuantity = quantityEl ? parseInt(quantityEl.value, 10) : 1;
  
  if (!STATE.draftSeatIds) STATE.draftSeatIds = [];
  if (!STATE.draftSeatLabels) STATE.draftSeatLabels = [];

  // If user ALREADY has a hold on another seat
  if (STATE.myHold) {
    const tile = document.getElementById(`seat-tile-${seatId}`);
    if (tile) {
      tile.style.animation = "shake 0.4s ease-in-out";
      setTimeout(() => (tile.style.animation = ""), 400);
    }
    if (DOM.holdTimerCard) {
      DOM.holdTimerCard.style.animation = "pulse-glow 0.6s ease-in-out 2";
      setTimeout(() => (DOM.holdTimerCard.style.animation = ""), 1200);
    }
    showToast({
      title: "Release Current Hold to Switch",
      message: `You are currently holding seats. Please release your current hold before making a new reservation.`,
      type: "warning",
      icon: "",
      duration: 4500,
    });
    return;
  }

  // If clicking a DRAFTED seat -> remove from draft
  if (STATE.draftSeatIds.includes(seatId)) {
    const idx = STATE.draftSeatIds.indexOf(seatId);
    STATE.draftSeatIds.splice(idx, 1);
    STATE.draftSeatLabels.splice(idx, 1);
    applySeatTileDOMState(seatId, seat.status);
    updateBottomPayBar();
    return;
  }

  let remainingQuantity = requestedQuantity - STATE.draftSeatIds.length;
  if (remainingQuantity <= 0) {
     showToast({ title: "Max Seats Reached", message: "You have already selected the requested number of seats.", type: "warning"});
     return;
  }

  // --- CONTIGUOUS SEAT SELECTION ---
  const rowSeats = Array.from(STATE.seatsMap.values())
    .filter(s => s.row === seat.row)
    .sort((a, b) => parseInt(a.seat_number, 10) - parseInt(b.seat_number, 10));
    
  const startIndex = rowSeats.findIndex(s => s.id === seatId);

  const isAvailable = (s) => (s.status === "AVAILABLE" || (s.status === "HELD" && s.held_until && new Date(s.held_until) < new Date())) && !STATE.draftSeatIds.includes(s.id);

  let leftIdx = startIndex;
  while (leftIdx > 0 && isAvailable(rowSeats[leftIdx - 1])) {
    leftIdx--;
  }

  let rightIdx = startIndex;
  while (rightIdx < rowSeats.length - 1 && isAvailable(rowSeats[rightIdx + 1])) {
    rightIdx++;
  }

  const selectedSeatIds = [];
  const selectedSeatLabels = [];

  const rightAvailable = rightIdx - startIndex + 1;
  const leftAvailable = startIndex - leftIdx; 

  if (rightAvailable >= remainingQuantity) {
    for (let i = startIndex; i < startIndex + remainingQuantity; i++) {
      selectedSeatIds.push(rowSeats[i].id);
      selectedSeatLabels.push(`${rowSeats[i].row}${rowSeats[i].seat_number}`);
    }
  } else if (rightAvailable + leftAvailable >= remainingQuantity) {
    const takeLeft = remainingQuantity - rightAvailable;
    for (let i = startIndex - takeLeft; i <= rightIdx; i++) {
      selectedSeatIds.push(rowSeats[i].id);
      selectedSeatLabels.push(`${rowSeats[i].row}${rowSeats[i].seat_number}`);
    }
  } else {
    for (let i = leftIdx; i <= rightIdx; i++) {
      selectedSeatIds.push(rowSeats[i].id);
      selectedSeatLabels.push(`${rowSeats[i].row}${rowSeats[i].seat_number}`);
    }
  }
  
  if (selectedSeatIds.length === 0) return;

  STATE.draftSeatIds.push(...selectedSeatIds);
  STATE.draftSeatLabels.push(...selectedSeatLabels);
  selectedSeatIds.forEach(id => applySeatTileDOMState(id, "SELECTED"));

  const totalAvailableNow = Array.from(STATE.seatsMap.values()).filter(s => s.status === "AVAILABLE" || (s.status === "HELD" && s.held_until && new Date(s.held_until) < new Date())).length;

  if (STATE.draftSeatIds.length < requestedQuantity) {
    if (STATE.draftSeatIds.length >= totalAvailableNow) {
       if (quantityEl) quantityEl.value = STATE.draftSeatIds.length;
       updateBottomPayBar();
       showToast({
         title: "Max Available Reached",
         message: `Only ${STATE.draftSeatIds.length} seat(s) are available right now. Proceeding to book them.`,
         type: "info",
         duration: 5000,
       });
    } else {
       const missing = requestedQuantity - STATE.draftSeatIds.length;
       updateBottomPayBar();
       showToast({
         title: "Selection Pending",
         message: `You have selected ${STATE.draftSeatIds.length} seat(s). Please click ${missing} more seat(s) to finish your selection. No seats are reserved yet.`,
         type: "info",
         duration: 5000,
       });
       return;
    }
  }

  // 6. Update Bottom Pay Bar
  updateBottomPayBar();
}

async function handleBottomPaySubmit() {
  // Enforce Authentication Requirement
  if (!isUserAuthenticated(true)) {
    return;
  }

  if (STATE.draftSeatIds.length === 0) return;
  if (STATE.isActionInProgress) return;

  STATE.isActionInProgress = true;
  if (!DOM.btnBottomPay) DOM.btnBottomPay = document.getElementById("btn-bottom-pay");
  if (!DOM.raceConditionModal) DOM.raceConditionModal = document.getElementById("race-condition-modal");

  if (DOM.btnBottomPay) {
    DOM.btnBottomPay.disabled = true;
    DOM.btnBottomPay.textContent = "Processing...";
  }

  const holdTxId = crypto.randomUUID(); // kept only as a local fallback label; server owns the real ID
  const allSeatIds = [...STATE.draftSeatIds];
  const allSeatLabels = [...STATE.draftSeatLabels];

  STATE.pendingHoldSeatIds = allSeatIds;
  allSeatIds.forEach(id => STATE.inFlightSeatIds.add(id));
  updateMatrixHoldState();

  try {
    const res = await fetchJson(`${CONFIG.inventoryUrl}/seats/reserve/`, {
      method: "POST",
      body: JSON.stringify({
        seat_ids: allSeatIds,
        // transaction_id, user_id, and user_email are intentionally omitted —
        // the inventory service generates the transaction_id server-side and reads
        // identity from the X-User-ID / X-User-Email headers injected by fetchJson.
      }),
    });

    if (res.ok) {
      // SUCCESS — use the server-assigned transaction_id from the response
      const serverTxId = res.data.transaction_id || holdTxId;
      const heldUntil = res.data.held_until || new Date(Date.now() + 10 * 60 * 1000).toISOString();
      STATE.myHold = {
        seatId: allSeatIds[0],
        seatIds: allSeatIds,
        seatNumbers: allSeatLabels,
        txId: serverTxId,
        heldUntil: heldUntil,
        timerInterval: null,
      };
      saveHoldToStorage(STATE.myHold);
      
      STATE.draftSeatIds = [];
      STATE.draftSeatLabels = [];
      STATE.pendingHoldSeatIds = null;
      allSeatIds.forEach(id => STATE.inFlightSeatIds.delete(id));
      updateBottomPayBar();

      // Update internal map
      allSeatIds.forEach(id => {
        const s = STATE.seatsMap.get(id);
        if (s) {
            s.status = "HELD";
            s.held_by_transaction_id = serverTxId;
            s.held_until = heldUntil;
            s.user_email = STATE.user ? STATE.user.email : undefined;
            s.user_id = STATE.user ? STATE.user.userId : undefined;
        }
      });

      startHoldTimer(heldUntil);
      updateCheckoutSummary();
      updateMatrixHoldState();
      updateInventoryStats();
      
      showToast({
        title: "10-Minute Hold Acquired!",
        message: `${allSeatIds.length} seat(s) reserved for you. Complete payment before the timer expires.`,
        type: "success",
        icon: "",
        duration: 4000,
      });

    } else {
      STATE.pendingHoldSeatIds = null;
      allSeatIds.forEach(id => STATE.inFlightSeatIds.delete(id));
      allSeatIds.forEach(id => {
         applySeatTileDOMState(id, "AVAILABLE");
      });
      STATE.draftSeatIds = [];
      STATE.draftSeatLabels = [];
      updateBottomPayBar();
      updateMatrixHoldState();

      if (res.status === 409 || (res.data && res.data.code === "SEAT_UNAVAILABLE")) {
        // Genuine race condition: seat was acquired by another transaction
        if (DOM.raceConditionModal) {
          DOM.raceConditionModal.classList.add("show");
        }
      } else if (res.status === 429 || (res.data && res.data.code === "MAX_HOLDS_EXCEEDED")) {
        showToast({
          title: "Hold Limit Reached",
          message: (res.data && res.data.error) || "You have reached the maximum allowed active holds. Please complete checkout or wait for previous holds to expire.",
          type: "warning",
          duration: 6000,
        });
      } else if (res.status === 401 || res.status === 403) {
        handleLogout();
        showAuthModal(1);
        showToast({
          title: "Authentication Required",
          message: (res.data && res.data.error) || "Your session expired. Please sign in again with your email to reserve seats.",
          type: "warning",
          duration: 6000,
        });
      } else {
        showToast({
          title: "Hold Failed",
          message: (res.data && res.data.error) || "Unable to reserve selected seats. Please try again.",
          type: "danger",
          duration: 5000,
        });
      }
    }
  } catch (err) {
      STATE.pendingHoldSeatIds = null;
      allSeatIds.forEach(id => STATE.inFlightSeatIds.delete(id));
      allSeatIds.forEach(id => {
         applySeatTileDOMState(id, "AVAILABLE");
      });
      STATE.draftSeatIds = [];
      STATE.draftSeatLabels = [];
      updateBottomPayBar();
      updateMatrixHoldState();
      showToast({
        title: "Network Error",
        message: "Could not reach Inventory service. Please check your connection and try again.",
        type: "danger",
        duration: 5000,
      });
  } finally {
      if (DOM.btnBottomPay) {
        DOM.btnBottomPay.disabled = false;
        DOM.btnBottomPay.textContent = "Pay Amount";
      }
      STATE.isActionInProgress = false;
  }
}

function updateMatrixHoldState() {
  const matrix = DOM.seatMatrix || document.getElementById("seat-matrix");
  if (!matrix) return;
  const hasHold = Boolean(STATE.myHold || STATE.pendingHoldSeatIds || STATE.isActionInProgress);
  if (hasHold) {
    matrix.classList.add("has-active-hold");
  } else {
    matrix.classList.remove("has-active-hold");
  }
}

/* ==========================================================================
   RELEASE HOLD (DESELECTION & BEACON)
   ========================================================================== */

async function releaseActiveHold() {
  if (!STATE.myHold) return;
  if (STATE.isActionInProgress) return;

  const { seatIds, txId, timerInterval } = STATE.myHold;
  const targetIds = seatIds || [STATE.myHold.seatId];
  if (timerInterval) clearInterval(timerInterval);

  STATE.isActionInProgress = true;
  targetIds.forEach(id => STATE.inFlightSeatIds.add(id));
  STATE.myHold = null;
  STATE.pendingHoldSeatIds = null;
  clearHoldFromStorage();

  // Optimistically mark as AVAILABLE
  targetIds.forEach(id => applySeatTileDOMState(id, "AVAILABLE"));
  hideHoldTimer();
  updateCheckoutSummary();
  updateMatrixHoldState();
  updateBottomPayBar();

  try {
    await fetchJson(`${CONFIG.inventoryUrl}/seats/release/`, {
      method: "POST",
      body: JSON.stringify({ transaction_id: txId, seat_ids: targetIds }),
    });

    targetIds.forEach(id => {
      const seat = STATE.seatsMap.get(id);
      if (seat) {
        seat.status = "AVAILABLE";
        seat.held_until = null;
        seat.held_by_transaction_id = null;
      }
    });

    showToast({
      title: "Hold Released",
      message: `${targetIds.length} seat(s) released back to the venue catalog.`,
      type: "normal",
      icon: "",
    });
  } catch (err) {
    console.warn("Release hold error", err);
  } finally {
    STATE.isActionInProgress = false;
    targetIds.forEach(id => STATE.inFlightSeatIds.delete(id));
    updateMatrixHoldState();
    updateInventoryStats();
  }
}

/**
 * Tab and visibility change handler — keeps hold active across tab switches.
 */
function setupBeaconUnloadHandler() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      if (STATE.myHold && STATE.myHold.heldUntil) {
        // If expired while away in another tab, clean up gracefully
        if (Date.now() >= new Date(STATE.myHold.heldUntil).getTime()) {
          handleHoldAutoExpired();
        }
      }
      loadCatalog(false);
    }
  });
}

/* ==========================================================================
   LIVE HOLD COUNTDOWN TIMER
   ========================================================================== */

function startHoldTimer(heldUntilIso) {
  if (!DOM.holdTimerCard) DOM.holdTimerCard = document.getElementById("hold-timer-card");
  if (!DOM.holdTimerCard) return;
  DOM.holdTimerCard.classList.remove("hidden");
  DOM.holdTimerCard.style.display = "";

  if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);

  const targetTime = new Date(heldUntilIso).getTime();
  const totalHoldMs = 10 * 60 * 1000; // 10 minutes

  const updateTick = () => {
    const now = Date.now();
    const remainingMs = Math.max(0, targetTime - now);

    const minutes = Math.floor(remainingMs / 60000);
    const seconds = Math.floor((remainingMs % 60000) / 1000);

    const formatted = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    if (DOM.holdClock) DOM.holdClock.textContent = formatted;

    const progressPct = Math.min(100, Math.max(0, (remainingMs / totalHoldMs) * 100));
    if (DOM.timerProgressFill) DOM.timerProgressFill.style.width = `${progressPct}%`;

    if (remainingMs <= 0) {
      if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
      handleHoldAutoExpired();
    }
  };

  updateTick();
  if (STATE.myHold) {
    STATE.myHold.timerInterval = setInterval(updateTick, 1000);
  }
}

function hideHoldTimer() {
  if (!DOM.holdTimerCard) DOM.holdTimerCard = document.getElementById("hold-timer-card");
  if (DOM.holdTimerCard) {
    DOM.holdTimerCard.classList.add("hidden");
    DOM.holdTimerCard.style.display = "none";
  }
}

function handleHoldAutoExpired() {
  if (!STATE.myHold) return;
  const seatIds = STATE.myHold.seatIds || [STATE.myHold.seatId];
  const firstSeat = STATE.seatsMap.get(seatIds[0]);
  const seatLabel = STATE.myHold.seatNumbers || (firstSeat ? `${firstSeat.row}${firstSeat.seat_number}` : "");

  if (STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
  STATE.myHold = null;
  clearHoldFromStorage();
  hideHoldTimer();
  updateCheckoutSummary();
  updateMatrixHoldState();

  showToast({
    title: "Hold Expired",
    message: `Your 10-minute hold on Seats ${seatLabel} has expired and was returned to the catalog. You may be penalized.`,
    type: "warning",
    icon: "",
    duration: 6000,
  });

  loadCatalog(false);
}

/* ==========================================================================
   CHECKOUT SIDEBAR STATE UPDATES
   ========================================================================== */

function updateCheckoutSummary() {
  if (!STATE.myHold) {
    if (DOM.summaryEmpty) DOM.summaryEmpty.classList.remove("hidden");
    if (DOM.summaryActive) DOM.summaryActive.classList.add("hidden");
    if (DOM.summaryTier) DOM.summaryTier.textContent = "No Selection";
    return;
  }

  const seatIds = STATE.myHold.seatIds || [STATE.myHold.seatId];
  const firstSeat = STATE.seatsMap.get(seatIds[0]);
  if (!firstSeat) return;

  const tierMeta = TIERS[firstSeat.row] || { name: "General Seat", price: "500.00", basePrice: 500 };
  const totalBasePrice = tierMeta.basePrice * seatIds.length;

  if (DOM.summaryEmpty) DOM.summaryEmpty.classList.add("hidden");
  if (DOM.summaryActive) DOM.summaryActive.classList.remove("hidden");

  if (DOM.summaryTier) DOM.summaryTier.textContent = `${tierMeta.name} (${seatIds.length})`;
  if (DOM.summarySeatLabel) DOM.summarySeatLabel.textContent = `Row ${firstSeat.row} - Seats ${STATE.myHold.seatNumbers || firstSeat.seat_number}`;
  if (DOM.summarySeatTier) DOM.summarySeatTier.textContent = `${seatIds.length}x ${tierMeta.name}`;
  if (DOM.summaryTxId) DOM.summaryTxId.textContent = `${STATE.myHold.txId.substring(0, 8)}...`;
  if (DOM.summaryBasePrice) DOM.summaryBasePrice.textContent = `$${totalBasePrice.toFixed(2)}`;
  if (DOM.summaryTotalPrice) DOM.summaryTotalPrice.textContent = `$${totalBasePrice.toFixed(2)}`;
  
  // Set the pay sim values based on correct total price
  const radioSuccess = document.getElementById("pay-sim-success");
  const radioFail = document.getElementById("pay-sim-fail");
  if (radioSuccess) radioSuccess.value = totalBasePrice;
  if (radioFail) radioFail.value = 1; // insufficient funds sim

  const quantityEl = document.getElementById("seat-quantity");
  const requestedQuantity = quantityEl ? parseInt(quantityEl.value, 10) : 1;
  if (DOM.btnProceedPay) {
    if (seatIds.length < requestedQuantity) {
      DOM.btnProceedPay.textContent = `Select ${requestedQuantity - seatIds.length} more seat(s)`;
      DOM.btnProceedPay.disabled = true;
      DOM.btnProceedPay.classList.add("opacity-50", "cursor-not-allowed");
    } else {
      DOM.btnProceedPay.textContent = "Proceed to Payment →";
      DOM.btnProceedPay.disabled = false;
      DOM.btnProceedPay.classList.remove("opacity-50", "cursor-not-allowed");
    }
  }
}

/* ==========================================================================
   SAGA ORCHESTRATION & CHECKOUT
   ========================================================================== */

async function initiateSagaCheckout() {
  if (!STATE.myHold) return;

  // Enforce Authentication Requirement
  if (!isUserAuthenticated(true)) {
    if (DOM.btnProceedPay) DOM.btnProceedPay.disabled = false;
    return;
  }

  const seatIds = STATE.myHold.seatIds || [STATE.myHold.seatId];
  const firstSeat = STATE.seatsMap.get(seatIds[0]);
  if (!firstSeat) return;

  const txId = STATE.myHold.txId;
  const selectedAmount = document.querySelector('input[name="pay_sim"]:checked').value;

  // Validate quantity
  const quantityEl = document.getElementById("seat-quantity");
  const requestedQuantity = quantityEl ? parseInt(quantityEl.value, 10) : 1;
  if (seatIds.length < requestedQuantity) {
    showToast({
      title: "Incomplete Selection",
      message: `You requested ${requestedQuantity} seats but have only selected ${seatIds.length}. Please click ${requestedQuantity - seatIds.length} more seat(s) to complete your reservation.`,
      type: "warning",
      duration: 5000,
    });
    return;
  }

  // Prevent double click
  if (DOM.btnProceedPay) DOM.btnProceedPay.disabled = true;

  // Open Saga Orchestration Stepper Modal
  openSagaModal(firstSeat, selectedAmount);

  const queryParams = new URLSearchParams();
  if (selectedAmount === "9999") {
    queryParams.set("simulate_payment_decline", "true");
  }
  const queryString = queryParams.toString();
  const endpointUrl = `${CONFIG.coordinatorUrl}/transactions/${queryString ? `?${queryString}` : ""}`;

  // POST to Coordinator — asynchronous non-blocking saga execution (returns HTTP 202 Accepted)
  // Celery worker drives the distributed saga pipeline in the background via Redis
  // Identity is securely transmitted via Authorization Bearer JWT header in fetchJson
  const res = await fetchJson(endpointUrl, {
    method: "POST",
    body: JSON.stringify({
      seat_ids: seatIds,
    }),
  });

  const finalTxId = (res.data && res.data.transaction_id) || txId;
  if (STATE.myHold) {
    STATE.myHold.txId = finalTxId;
  }

  if (res.ok && res.data && res.data.status) {
    if (res.status === 202) {
      // Async: Celery picked it up — listen via Server-Sent Events (SSE) push (reduces to 2 requests)
      listenToTransactionSSE(finalTxId, selectedAmount);
    } else {
      // Sync: Coordinator returned final result immediately (200)
      handleSagaTerminalState(res.data.status, finalTxId, selectedAmount);
    }
  } else {
    if (res.status === 401 || (res.data && res.data.code === "AUTHENTICATION_REQUIRED")) {
      handleLogout();
      showAuthModal(1);
      showToast({
        title: "Session Expired",
        message: (res.data && res.data.error) || "Your session expired during checkout. Please sign in again to book your seats.",
        type: "warning",
        duration: 6000,
      });
      if (DOM.sagaModal) DOM.sagaModal.classList.add("hidden");
      if (DOM.btnProceedPay) DOM.btnProceedPay.disabled = false;
      return;
    }
    // Show failure reason
    handleSagaTerminalState("FAILED", txId, selectedAmount);
    showToast({
      title: "Coordinator Error",
      message: (res.data && res.data.error) || "Could not reach Coordinator service.",
      type: "danger",
    });
    if (DOM.btnProceedPay) DOM.btnProceedPay.disabled = false;
  }
}

function openSagaModal(firstSeat, amount) {
  if (!DOM.sagaModal) return;
  DOM.sagaModal.classList.remove("hidden");

  DOM.step1.className = "step-item done";
  DOM.step1Status.textContent = "VERIFIED";

  DOM.step2.className = "step-item active";
  DOM.step2Status.textContent = "IN PROGRESS";

  DOM.step3.className = "step-item";
  DOM.step3Status.textContent = "WAITING";

  if (DOM.modalResult) DOM.modalResult.classList.add("hidden");
  if (DOM.eticketCard) DOM.eticketCard.classList.add("hidden");
  if (DOM.compensationCard) DOM.compensationCard.classList.add("hidden");

  if (DOM.btnModalClose) {
    DOM.btnModalClose.disabled = true;
    DOM.btnModalClose.textContent = "Processing...";
  }

  if (DOM.btnViewForensics) {
    DOM.btnViewForensics.href = `index.html`;
    DOM.btnViewForensics.classList.add("hidden");
  }
}

/**
 * Listen to real-time transaction state updates via Server-Sent Events (SSE).
 * Reduces network requests from 20-50 polling calls to exactly 1 streaming connection!
 * Gracefully falls back to polling if the stream encounters a network error.
 */
function listenToTransactionSSE(txId, amount) {
  const token = localStorage.getItem("dtc_auth_token") || "";
  const queryParam = token ? `?token=${encodeURIComponent(token)}` : "";
  const sseUrl = `${CONFIG.coordinatorUrl}/transactions/${txId}/stream/${queryParam}`;

  let eventSource = null;
  let hasHandledTerminal = false;
  let streamClosed = false;

  const closeStream = () => {
    if (streamClosed) return;
    streamClosed = true;
    if (eventSource) {
      try {
        eventSource.close();
      } catch (e) {}
      eventSource = null;
    }
  };

  try {
    eventSource = new EventSource(sseUrl);
  } catch (err) {
    console.warn("EventSource initialization failed, falling back to polling:", err);
    pollSagaStatus(txId, amount);
    return;
  }

  // Safety fallback timer: if SSE doesn't finish within 25 seconds, switch to polling check
  const safetyTimeout = setTimeout(() => {
    if (!hasHandledTerminal) {
      closeStream();
      pollSagaStatus(txId, amount);
    }
  }, 25000);

  eventSource.onmessage = (event) => {
    if (!event.data) return;

    try {
      const data = JSON.parse(event.data);
      const status = data.status;

      // Update Phase Stepper Visual Progression in real-time
      if (status === "PAYMENT_PROCESSING") {
        DOM.step2.className = "step-item active";
        DOM.step2Status.textContent = "AUTHORIZING";
      } else if (status === "INVENTORY_RESERVED") {
        DOM.step1.className = "step-item done";
        DOM.step1Status.textContent = "RESERVED";
      }

      if (data.is_terminal || ["COMPLETED", "COMPENSATED", "INVENTORY_RESERVE_FAILED", "COMPENSATION_FAILED"].includes(status)) {
        if (hasHandledTerminal) return;
        hasHandledTerminal = true;
        clearTimeout(safetyTimeout);
        closeStream();
        handleSagaTerminalState(status, txId, amount);
      }
    } catch (e) {
      console.warn("Error parsing SSE data chunk:", e);
    }
  };

  eventSource.onerror = (err) => {
    // If stream already completed, ignore error on close
    if (hasHandledTerminal) return;

    console.warn("SSE connection error or closed by server. Falling back to poll:", err);
    clearTimeout(safetyTimeout);
    closeStream();

    // Fall back to polling immediately
    pollSagaStatus(txId, amount);
  };
}

async function pollSagaStatus(txId, amount) {
  let attempts = 0;
  const maxAttempts = 25; // 25 attempts * 1000ms = 25s safety ceiling
  let isPolling = true;

  const poll = async () => {
    if (!isPolling) return;
    attempts++;

    // High Performance: Query ONLY the status endpoint (NOT events/) during polling
    const txRes = await fetchJson(`${CONFIG.coordinatorUrl}/transactions/${txId}/`);

    if (!isPolling) return;

    if (!txRes.ok || !txRes.data) {
      if (attempts >= maxAttempts) {
        isPolling = false;
        handleSagaTerminalState("TIMEOUT", txId, amount);
        return;
      }
      setTimeout(poll, 1000);
      return;
    }

    const txn = txRes.data;
    const status = txn.status;

    // Phase Stepper Visual Progression
    if (status === "PAYMENT_PROCESSING") {
      DOM.step2.className = "step-item active";
      DOM.step2Status.textContent = "AUTHORIZING";
    } else if (status === "INVENTORY_RESERVED") {
      DOM.step1.className = "step-item done";
      DOM.step1Status.textContent = "RESERVED";
    }

    if (txn.is_terminal || ["COMPLETED", "COMPENSATED", "INVENTORY_RESERVE_FAILED", "COMPENSATION_FAILED"].includes(status)) {
      isPolling = false;
      // Fetch forensic events ONCE upon completion for modal display
      let eventsData = [];
      try {
        const evRes = await fetchJson(`${CONFIG.coordinatorUrl}/transactions/${txId}/events/`);
        if (evRes && evRes.ok && evRes.data) {
          eventsData = Array.isArray(evRes.data) ? evRes.data : (evRes.data.events || []);
        }
      } catch (e) {}
      handleSagaTerminalState(status, txId, amount, eventsData);
      return;
    }

    if (attempts >= maxAttempts) {
      isPolling = false;
      handleSagaTerminalState("TIMEOUT", txId, amount);
      return;
    }

    // Sequential non-overlapping poll (prevents socket exhaustion and (pending) queue freezes)
    setTimeout(poll, 1000);
  };

  // Give Celery a 600ms head start before the first poll
  setTimeout(poll, 600);
}

function handleSagaTerminalState(status, txId, amount, eventsData) {
  if (DOM.modalResult) DOM.modalResult.classList.remove("hidden");
  if (DOM.btnModalClose) {
    DOM.btnModalClose.disabled = false;
    DOM.btnModalClose.textContent = "Close & Finish";
  }
  if (DOM.btnViewForensics) {
    DOM.btnViewForensics.classList.remove("hidden");
  }

  if (status === "COMPLETED") {
    // SAGA SUCCESS: E-Ticket Confirmed
    DOM.step2.className = "step-item done";
    DOM.step2Status.textContent = "CHARGED";

    DOM.step3.className = "step-item done";
    DOM.step3Title.textContent = "Ticket Confirmed";
    DOM.step3Desc.textContent = "PostgreSQL row transitioned HELD → BOOKED";
    DOM.step3Status.textContent = "BOOKED";

    if (DOM.modalTitle) DOM.modalTitle.textContent = "Booking Confirmed!";
    if (DOM.modalSubtitle) DOM.modalSubtitle.textContent = "Your payment was processed and your seat is locked in the venue.";

    if (DOM.eticketCard) {
      DOM.eticketCard.classList.remove("hidden");
      if (STATE.myHold) {
        const seatIds = STATE.myHold.seatIds || [STATE.myHold.seatId];
        const seat = STATE.seatsMap.get(seatIds[0]);
        if (seat && DOM.ticketSeatNumber) {
          DOM.ticketSeatNumber.textContent = `Row ${seat.row} - Seats ${STATE.myHold.seatNumbers || seat.seat_number}`;
        }
      }
      if (DOM.ticketTxId) DOM.ticketTxId.textContent = txId;
    }

    // Clean up local hold state
    if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
    STATE.myHold = null;
    clearHoldFromStorage();
    hideHoldTimer();
    updateCheckoutSummary();
    loadCatalog(false);
  } else if (status === "COMPENSATED" || status === "PAYMENT_FAILED") {
    // SAGA COMPENSATION: Payment Declined -> Compensating Action Released Seat
    DOM.step2.className = "step-item compensated";
    DOM.step2Status.textContent = "DECLINED (422)";

    DOM.step3.className = "step-item compensated";
    DOM.step3Title.textContent = "3. Compensating Action Executed";
    DOM.step3Desc.textContent = "Coordinator called POST /seats/release/ to restore seat availability";
    DOM.step3Status.textContent = "COMPENSATED";

    if (DOM.modalTitle) DOM.modalTitle.textContent = "Payment Declined — Hold Auto-Compensated";
    if (DOM.modalSubtitle) DOM.modalSubtitle.textContent = "Payment gateway declined amount $9,999. Seat was released back to the venue.";

    if (DOM.compensationCard) {
      DOM.compensationCard.classList.remove("hidden");
      if (DOM.compReason) {
        DOM.compReason.textContent = "Simulated card decline amount ($9,999). Coordinator triggered 2PC compensation release.";
      }
    }

    // Clean up local hold state
    if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
    STATE.myHold = null;
    clearHoldFromStorage();
    hideHoldTimer();
    updateCheckoutSummary();
    loadCatalog(false);
  } else {
    // SAGA ERROR / TIMEOUT / CONFLICT
    DOM.step2.className = "step-item compensated";
    DOM.step2Status.textContent = status || "FAILED";

    DOM.step3.className = "step-item compensated";
    DOM.step3Title.textContent = "3. Transaction Terminated";
    DOM.step3Desc.textContent = `Coordinator resolved saga state to ${status}.`;
    DOM.step3Status.textContent = status || "FAILED";

    if (DOM.modalTitle) DOM.modalTitle.textContent = `Transaction ${status}`;
    if (DOM.modalSubtitle) DOM.modalSubtitle.textContent = `The saga reached terminal state: ${status}.`;

    if (DOM.compensationCard) {
      DOM.compensationCard.classList.remove("hidden");
      if (DOM.compReason) {
        DOM.compReason.textContent = `Terminal Status: ${status}. Downstream service response was not verified within SLA.`;
      }
    }

    if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
    STATE.myHold = null;
    clearHoldFromStorage();
    hideHoldTimer();
    updateCheckoutSummary();
    loadCatalog(false);
  }

  if (DOM.btnProceedPay) DOM.btnProceedPay.disabled = false;
}

/* ==========================================================================
   DIFFERENTIAL RECONCILIATION ENGINE
   ========================================================================== */

function startDifferentialPolling() {
  // Automatic polling disabled: auto-refresh of /seats is turned off.
  if (STATE.pollTimer) {
    clearInterval(STATE.pollTimer);
    STATE.pollTimer = null;
  }
}

/**
 * Explicit Optimistic-Update-vs-Poll Reconciliation Rule:
 * 1. For any seat ID present in `STATE.inFlightSeatIds`, SKIP updating from poll to prevent
 *    overwriting an in-flight user interaction before its Promise settles.
 * 2. For the current user's active hold (`STATE.myHold`), preserve "SELECTED" status in the DOM
 *    as long as server confirms hold.
 * 3. For all other seats, patch only modified DOM attributes (`dataset.status`, icon, class) with
 *    smooth CSS transitions.
 */
function reconcileSeatStates(newSeats) {
  if (DOM.seatMatrix && DOM.seatMatrix.children.length === 0 && newSeats.length > 0) {
    newSeats.forEach(seat => STATE.seatsMap.set(seat.id, seat));
    renderFullSeatMatrix();
    if (DOM.seatSkeleton) DOM.seatSkeleton.classList.add("hidden");
    return;
  }

  newSeats.forEach(newSeat => {
    const seatId = newSeat.id;

    // Do not clobber in-flight user action
    if (STATE.inFlightSeatIds.has(seatId)) return;

    STATE.seatsMap.set(seatId, newSeat);

    const isUserHold = STATE.myHold && (STATE.myHold.seatIds || [STATE.myHold.seatId]).includes(seatId) &&
                       (newSeat.held_by_transaction_id === STATE.myHold.txId ||
                        newSeat.status === "HELD" ||
                        (STATE.user && newSeat.user_id === STATE.user.userId));

    const isDrafted = STATE.draftSeatIds && STATE.draftSeatIds.includes(seatId);

    if (isUserHold || isDrafted) {
      applySeatTileDOMState(seatId, "SELECTED");
    } else {
      // All other seats in the venue MUST strictly reflect their true server status
      applySeatTileDOMState(seatId, newSeat.status);
    }
  });
}

function applySeatTileDOMState(seatId, status) {
  const tile = document.getElementById(`seat-tile-${seatId}`);
  if (!tile) return;

  // Strict Single-Seat Integrity Guard:
  if (STATE.myHold && (STATE.myHold.seatIds || [STATE.myHold.seatId]).includes(seatId)) {
    status = "SELECTED";
  } else if (STATE.draftSeatIds && STATE.draftSeatIds.includes(seatId)) {
    status = "SELECTED";
  } else if (status === "SELECTED") {
    const isPending = STATE.pendingHoldSeatIds ? STATE.pendingHoldSeatIds.includes(seatId) : (STATE.pendingHoldSeatId === seatId);
    if (!isPending) {
      const seat = STATE.seatsMap.get(seatId);
      status = (seat && seat.status) || "AVAILABLE";
    }
  }

  if (tile.dataset.status !== status) {
    tile.dataset.status = status;
    const iconEl = tile.querySelector(".seat-icon");
    if (iconEl) iconEl.textContent = getSeatStatusIcon(status);
  }
}

function updateInventoryStats() {
  let avail = 0;
  let held = 0;
  let booked = 0;

  STATE.seatsMap.forEach(seat => {
    if (seat.status === "AVAILABLE") avail++;
    else if (seat.status === "HELD") held++;
    else if (seat.status === "BOOKED") booked++;
  });

  if (DOM.statAvailable) DOM.statAvailable.textContent = avail;
  if (DOM.statHeld) DOM.statHeld.textContent = held;
  if (DOM.statBooked) DOM.statBooked.textContent = booked;
}

/* ==========================================================================
   RESET VENUE SEATS (DEMO RESTOCK)
   ========================================================================== */

async function resetVenueSeats() {
  try {
    const res = await fetchJson(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
    if (res.ok) {
      if (STATE.myHold && STATE.myHold.timerInterval) clearInterval(STATE.myHold.timerInterval);
      STATE.myHold = null;
      clearHoldFromStorage();
      hideHoldTimer();
      updateCheckoutSummary();
      updateMatrixHoldState();

      showToast({
        title: "Venue Restocked",
        message: "All 30 venue seats have been reset to AVAILABLE in PostgreSQL!",
        type: "success",
        icon: "",
      });

      loadCatalog(false);
    } else {
      showToast({ title: "Reset Failed", message: "Could not reset venue seats", type: "danger" });
    }
  } catch (err) {
    showToast({ title: "Error", message: err.message, type: "danger" });
  }
}
