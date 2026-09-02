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

// Service Configuration (Locked to Production Invariants)
const CONFIG = {
  coordinatorUrl: "https://coordinator-production-4685.up.railway.app",
  inventoryUrl: "https://inventory-production-9e43.up.railway.app",
  paymentUrl: "https://payment-production-bcb1.up.railway.app",
  pollIntervalMs: 1500,
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
    userId: localStorage.getItem("dtc_user_id") || localStorage.getItem("dtc_actor_id") || null,
    actorId: localStorage.getItem("dtc_user_id") || localStorage.getItem("dtc_actor_id") || null,
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
  profileActorId: document.getElementById("profile-actor-id"),
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
};

// Tier Metadata Definition
const TIERS = {
  A: { name: "VIP Recliner Club", price: "750.00", basePrice: 750 },
  B: { name: "Premium Balcony", price: "500.00", basePrice: 500 },
  C: { name: "Executive Stalls", price: "350.00", basePrice: 350 },
};

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
      txId: hold.txId,
      heldUntil: hold.heldUntil,
      userId: (STATE.user && (STATE.user.userId || STATE.user.actorId)) || null,
      userEmail: (STATE.user && STATE.user.email) || null,
    };
    localStorage.setItem(HOLD_STORAGE_KEY, JSON.stringify(data));
  } catch (e) {}
}

function clearHoldFromStorage() {
  try {
    localStorage.removeItem(HOLD_STORAGE_KEY);
  } catch (e) {}
}

function restoreHoldFromStorage() {
  try {
    const raw = localStorage.getItem(HOLD_STORAGE_KEY);
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

    startHoldTimer(data.heldUntil);
    updateCheckoutSummary();
    updateMatrixHoldState();
  } catch (e) {
    clearHoldFromStorage();
  }
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
      headers["Authorization"] = `Bearer ${STATE.user.token}`;
    }
    if (STATE.user && (STATE.user.userId || STATE.user.actorId)) {
      const uid = STATE.user.userId || STATE.user.actorId;
      headers["X-User-ID"] = uid;
      headers["X-Actor-ID"] = uid;
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
  const userId = localStorage.getItem("dtc_user_id") || localStorage.getItem("dtc_actor_id");

  STATE.user = { token, email, userId, actorId: userId };

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
      DOM.authOtpInput.value = "";
      setTimeout(() => DOM.authOtpInput.focus(), 100);
    }
  } else if (step === 3 && DOM.authStep3) {
    DOM.authStep3.classList.remove("hidden");
    if (DOM.profileEmailDisplay && STATE.user) {
      DOM.profileEmailDisplay.textContent = STATE.user.email;
    }
    if (DOM.profileActorId && STATE.user) {
      DOM.profileActorId.textContent = STATE.user.userId || STATE.user.actorId || `usr_${STATE.user.email}`;
    }
  }
}

function startOtpCountdown(seconds = 30) {
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
      body: JSON.stringify({ email }),
    });

    if (res.ok && res.data.success) {
      if (DOM.authTargetEmail) DOM.authTargetEmail.textContent = res.data.email;
      renderAuthStep(2);
      if (DOM.authOtpInput) {
        DOM.authOtpInput.value = "";
      }
      startOtpCountdown(res.data.cooldown_remaining || 30);

      const chancesText = res.data.chances_remaining !== undefined ? `\n(You have ${res.data.chances_remaining} attempts remaining)` : "";
      
      const isCooldown = res.data.cooldown_remaining !== undefined;
      const title = isCooldown ? "Code Already Sent!" : "Verification Code Sent!";
      const messageBody = isCooldown 
        ? `An active verification code was already sent to ${res.data.email}. Please check your inbox.`
        : `6-Digit verification code sent to ${res.data.email}. Please check your inbox.`;

      showToast({
        title: title,
        message: messageBody + chancesText,
        type: "success",
        duration: 8000,
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
      body: JSON.stringify({ email, otp }),
    });

    if (res.ok && res.data.success) {
      const { token, user_id, user_email, actor_id, email: resEmail } = res.data;
      const finalUserId = user_id || actor_id;
      const finalEmail = user_email || resEmail || email;
      localStorage.setItem("dtc_auth_token", token);
      localStorage.setItem("dtc_user_email", finalEmail);
      localStorage.setItem("dtc_user_id", finalUserId);
      localStorage.setItem("dtc_actor_id", finalUserId);

      STATE.user = { token, email: finalEmail, userId: finalUserId, actorId: finalUserId };
      updateAuthWidgetUI();

      showToast({
        title: "Signed In Successfully",
        message: `Welcome! User ID: ${finalUserId}. Hold cap: Max 10 seats.`,
        type: "success",
        duration: 5000,
      });

      renderAuthStep(3);
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

function handleLogout() {
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
  localStorage.removeItem("dtc_actor_id");

  STATE.user = { token: null, email: null, userId: null, actorId: null };
  updateAuthWidgetUI();
  closeAuthModal();

  showToast({
    title: "Signed Out",
    message: "Identity detached. Live seat reservations will require signing in again.",
    type: "info",
  });
}

/* ==========================================================================
   INITIALIZATION & BOOTSTRAP
   ========================================================================== */

// Session Heartbeat
setInterval(() => {
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    fetch(`${CONFIG.coordinatorUrl}/auth/heartbeat/`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` }
    }).then(res => {
      if (res.status === 401 || res.status === 403) {
        handleLogout(); // Auto-logout if session expired
      }
    }).catch(() => {});
  }
}, 15000);

// Tab Close Session Cleanup
window.addEventListener("beforeunload", () => {
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    fetch(`${CONFIG.coordinatorUrl}/auth/logout/`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}` },
      keepalive: true
    }).catch(() => {});
  }
});

document.addEventListener("DOMContentLoaded", () => {
  setupAuthEventListeners();
  updateAuthWidgetUI();
  restoreHoldFromStorage();
  initEventListeners();
  loadCatalog(true);
  startDifferentialPolling();
  setupBeaconUnloadHandler();
});

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
        showToast({
          title: "Selection Reset",
          message: "You changed the seat quantity. Your pending selection has been cleared. Please pick your seats again.",
          type: "info",
          duration: 3000
        });
      }
    });
  }

  // Proceed to Payment CTA
  if (DOM.btnProceedPay) {
    DOM.btnProceedPay.addEventListener("click", initiateSagaCheckout);
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

  // Config Modal Integration
  initConfigModal();
}

function initConfigModal() {
  const btnConfig = document.getElementById("btn-config");
  const modalConfig = document.getElementById("modal-config");
  const btnCloseConfig = document.getElementById("btn-close-config");
  const formConfig = document.getElementById("form-config");
  const cfgCoordUrl = document.getElementById("cfg-coord-url");
  const cfgInvUrl = document.getElementById("cfg-inv-url");
  const cfgPayUrl = document.getElementById("cfg-pay-url");
  const cfgPollMs = document.getElementById("cfg-poll-ms");
  const btnResetCfg = document.getElementById("btn-reset-cfg");

  if (!btnConfig || !modalConfig) return;

  btnConfig.addEventListener("click", () => {
    if (cfgCoordUrl) cfgCoordUrl.value = CONFIG.coordinatorUrl;
    if (cfgInvUrl) cfgInvUrl.value = CONFIG.inventoryUrl;
    if (cfgPayUrl) cfgPayUrl.value = CONFIG.paymentUrl;
    if (cfgPollMs) cfgPollMs.value = CONFIG.pollIntervalMs;
    modalConfig.classList.remove("hidden");
  });

  btnCloseConfig?.addEventListener("click", () => modalConfig.classList.add("hidden"));
  const btnCloseConfigFooter = document.getElementById("btn-close-config-footer");
  btnCloseConfigFooter?.addEventListener("click", () => modalConfig.classList.add("hidden"));
  modalConfig.addEventListener("click", (e) => {
    if (e.target === modalConfig) modalConfig.classList.add("hidden");
  });
}

/* ==========================================================================
   CATALOG LOADING & INITIAL RENDER
   ========================================================================== */

async function loadCatalog(isInitial = false) {
  try {
    const [eventsRes, seatsRes] = await Promise.all([
      fetchJson(`${CONFIG.inventoryUrl}/events/`),
      fetchJson(`${CONFIG.inventoryUrl}/seats/`),
    ]);

    if (!seatsRes.ok || !Array.isArray(seatsRes.data)) {
      if (DOM.healthText) DOM.healthText.textContent = "Inventory: Offline";
      if (DOM.healthIndicator) {
        const ind = DOM.healthIndicator.querySelector(".status-indicator");
        if (ind) ind.style.background = "#ef4444";
      }
      if (isInitial) {
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
    if (eventsRes && eventsRes.ok && Array.isArray(eventsRes.data) && eventsRes.data.length > 0) {
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
    }

    // If no seats exist in database yet, automatically trigger seed restock
    if (seatsRes.data.length === 0) {
      const seedRes = await fetchJson(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
      if (seedRes.ok) {
        const refreshedSeats = await fetchJson(`${CONFIG.inventoryUrl}/seats/`);
        if (refreshedSeats.ok && Array.isArray(refreshedSeats.data)) {
          seatsRes.data = refreshedSeats.data;
        }
      }
    }

    // Populate Seats Map
    let orphanedTxId = null;
    let orphanedHeldUntil = null;
    const orphanedSeatIds = [];
    const orphanedSeatLabels = [];

    seatsRes.data.forEach(seat => {
      STATE.seatsMap.set(seat.id, seat);
      
      if (!STATE.myHold && STATE.user && (STATE.user.email || STATE.user.actorId)) {
        if (seat.status === "HELD") {
          const isOwner = (seat.user_email && STATE.user.email && seat.user_email === STATE.user.email) || 
                          (seat.user_id && STATE.user.actorId && seat.user_id === STATE.user.actorId) ||
                          (seat.held_by_user_email && STATE.user.email && seat.held_by_user_email === STATE.user.email);
          if (isOwner) {
            if (!orphanedTxId) orphanedTxId = seat.held_by_transaction_id;
            if (!orphanedHeldUntil) orphanedHeldUntil = seat.held_until;
            
            if (seat.held_by_transaction_id === orphanedTxId) {
              orphanedSeatIds.push(seat.id);
              orphanedSeatLabels.push(`${seat.row}${seat.seat_number}`);
            }
          }
        }
      }
    });

    if (!STATE.myHold && orphanedSeatIds.length > 0) {
      STATE.myHold = {
        seatId: orphanedSeatIds[0],
        seatIds: orphanedSeatIds,
        seatNumbers: orphanedSeatLabels.join(" - "),
        txId: orphanedTxId,
        heldUntil: orphanedHeldUntil,
        timerInterval: null
      };
      saveHoldToStorage(STATE.myHold);
      startHoldTimer(orphanedHeldUntil);
      updateCheckoutSummary();
    }

    if (isInitial || !DOM.seatMatrix || DOM.seatMatrix.children.length === 0) {
      renderFullSeatMatrix();
      if (DOM.seatSkeleton) DOM.seatSkeleton.classList.add("hidden");
    } else {
      reconcileSeatStates(seatsRes.data);
    }

    updateInventoryStats();
  } catch (err) {
    console.error("loadCatalog error:", err);
    if (isInitial) {
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
  if (status === "HELD") return "🔒";
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
  if (!STATE.user || !STATE.user.token) {
    showAuthModal(1);
    showToast({
      title: "Sign In Required",
      message: "Please sign in with your email OTP to place real-time seat holds, or switch to Mission Control Demo Mode.",
      type: "warning",
      duration: 6000,
    });
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
      (seat.user_id && STATE.user.actorId && seat.user_id === STATE.user.actorId) ||
      (seat.held_by_user_email && STATE.user.email && seat.held_by_user_email === STATE.user.email)
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
       showToast({
         title: "Max Available Reached",
         message: `Only ${STATE.draftSeatIds.length} seat(s) are available right now. Proceeding to book them.`,
         type: "info",
         duration: 5000,
       });
    } else {
       const missing = requestedQuantity - STATE.draftSeatIds.length;
       showToast({
         title: "Selection Pending",
         message: `You have selected ${STATE.draftSeatIds.length} seat(s). Please click ${missing} more seat(s) to finish your selection. No seats are reserved yet.`,
         type: "info",
         duration: 5000,
       });
       return;
    }
  }

  // 6. Proceed to Acquire Hold
  STATE.isActionInProgress = true;
  
  const holdTxId = crypto.randomUUID();
  const allSeatIds = [...STATE.draftSeatIds];
  const allSeatLabels = [...STATE.draftSeatLabels];

  // Clear draft
  STATE.draftSeatIds = [];
  STATE.draftSeatLabels = [];

  STATE.pendingHoldSeatIds = allSeatIds;
  allSeatIds.forEach(id => STATE.inFlightSeatIds.add(id));

  updateMatrixHoldState();

  showToast({
    title: "Holding Seats...",
    message: `Locking ${allSeatLabels.join(", ")} in Redis & PostgreSQL CAS hold...`,
    type: "normal",
    icon: "",
    duration: 1500,
  });

  try {
    const uid = STATE.user ? (STATE.user.userId || STATE.user.actorId) : undefined;
    const res = await fetchJson(`${CONFIG.inventoryUrl}/seats/reserve/`, {
      method: "POST",
      body: JSON.stringify({
        seat_ids: allSeatIds,
        transaction_id: holdTxId,
        user_id: uid,
        user_email: STATE.user ? STATE.user.email : undefined,
        actor_id: uid,
      }),
    });

    if (res.ok) {
      if (res.data.warning) {
          showToast({
              title: "Warning",
              message: res.data.warning,
              type: "warning",
              duration: 8000,
          });
      }
      
      const heldUntil = res.data.held_until || new Date(Date.now() + 10 * 60 * 1000).toISOString();
      STATE.myHold = {
        seatId: allSeatIds[0], 
        seatIds: allSeatIds,
        seatNumbers: allSeatLabels,
        txId: holdTxId,
        heldUntil: heldUntil,
        timerInterval: null,
      };
      saveHoldToStorage(STATE.myHold);
      STATE.pendingHoldSeatIds = null;

      // Update internal map
      allSeatIds.forEach(id => {
        const s = STATE.seatsMap.get(id);
        if (s) {
            s.status = "HELD";
            s.held_by_transaction_id = holdTxId;
            s.held_until = heldUntil;
            s.user_email = STATE.user ? STATE.user.email : undefined;
            s.user_id = STATE.user ? (STATE.user.userId || STATE.user.actorId) : undefined;
        }
      });

      startHoldTimer(heldUntil);
      updateCheckoutSummary();
      selectedSeatIds.forEach(id => applySeatTileDOMState(id, "SELECTED"));
      updateMatrixHoldState();
      updateInventoryStats();

      showToast({
        title: "10-Minute Hold Acquired!",
        message: `${selectedSeatIds.length} seat(s) reserved for you. Complete payment before the timer expires.`,
        type: "success",
        icon: "",
        duration: 4000,
      });
    } else {
      // 409 Conflict: Another buyer grabbed seat OR hold cap exceeded/penalty blocked!
      const conflictMsg = (res.data && res.data.error) || "Seats were just claimed by another user!";
      STATE.pendingHoldSeatIds = null;
      STATE.myHold = null;
      clearHoldFromStorage();
      
      allSeatIds.forEach(id => {
        applySeatTileDOMState(id, "AVAILABLE");
        const tile = document.getElementById(`seat-tile-${id}`);
        if (tile) {
          tile.style.animation = "shake 0.4s ease-in-out";
          setTimeout(() => (tile.style.animation = ""), 400);
        }
      });

      if (res.status === 403) {
          showToast({
              title: "Account Blocked",
              message: conflictMsg,
              type: "danger",
              duration: 8000,
          });
      } else if (res.data && res.data.code === "HOLD_CAP_EXCEEDED") {
        showToast({
          title: "Hold Cap Exceeded",
          message: conflictMsg,
          type: "danger",
          icon: "",
          duration: 6000,
        });
      } else {
        showToast({
          title: "Hold Conflict",
          message: `${conflictMsg} Please pick available seats.`,
          type: "danger",
          icon: "",
          duration: 4000,
        });
      }
    }
  } catch (err) {
    STATE.pendingHoldSeatIds = null;
    STATE.myHold = null;
    clearHoldFromStorage();
    allSeatIds.forEach(id => applySeatTileDOMState(id, "AVAILABLE"));
    showToast({
      title: "Network Error",
      message: err.message || "Failed to reserve seats.",
      type: "danger",
      icon: "",
    });
  } finally {
    STATE.isActionInProgress = false;
    allSeatIds.forEach(id => STATE.inFlightSeatIds.delete(id));
    updateMatrixHoldState();
    updateInventoryStats();
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
  if (!DOM.holdTimerCard) return;
  DOM.holdTimerCard.classList.remove("hidden");

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
  if (DOM.holdTimerCard) DOM.holdTimerCard.classList.add("hidden");
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

  const uid = STATE.user ? (STATE.user.userId || STATE.user.actorId) : undefined;
  const uemail = STATE.user ? STATE.user.email : undefined;

  // POST to Coordinator — synchronous execution on Railway (returns 200 directly)
  const res = await fetchJson(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({
      transaction_id: txId,
      seat_ids: seatIds,
      seat_numbers: STATE.myHold.seatNumbers || firstSeat.seat_number,
      quantity: seatIds.length,
      amount: parseInt(selectedAmount, 10),
      user_id: uid,
      user_email: uemail,
      held_by_user_id: uid,
      held_by_user_email: uemail,
      actor_id: uid,
    }),
  });

  if (res.ok && res.data && res.data.status) {
    if (res.status === 202) {
      // Async: Celery picked it up — start polling
      pollSagaStatus(txId, selectedAmount);
    } else {
      // Sync: Coordinator returned final result immediately (200)
      handleSagaTerminalState(res.data.status, txId, selectedAmount);
    }
  } else {
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

  // Note: Async Celery is disabled on this Railway deployment.
  // The saga runs synchronously — the modal will resolve in ~1-2 seconds.
  if (DOM.btnModalClose) {
    DOM.btnModalClose.disabled = true;
    DOM.btnModalClose.textContent = "Processing...";
  }

  if (DOM.btnViewForensics) {
    DOM.btnViewForensics.href = `index.html`;
    DOM.btnViewForensics.classList.add("hidden");
  }
}

async function pollSagaStatus(txId, amount) {
  let attempts = 0;
  const maxAttempts = 30;

  const interval = setInterval(async () => {
    attempts++;
    const [txRes, eventsRes] = await Promise.all([
      fetchJson(`${CONFIG.coordinatorUrl}/transactions/${txId}/`),
      fetchJson(`${CONFIG.coordinatorUrl}/transactions/${txId}/events/`),
    ]);

    if (!txRes.ok || !txRes.data) {
      if (attempts >= maxAttempts) {
        clearInterval(interval);
        handleSagaTerminalState("TIMEOUT", txId);
      }
      return;
    }

    const txn = txRes.data;
    const status = txn.status;

    // Phase Stepper Visual Progression
    if (status === "PAYMENT_PROCESSING") {
      DOM.step2.className = "step-item active";
      DOM.step2Status.textContent = "AUTHORIZING";
    }

    if (txn.is_terminal || ["COMPLETED", "COMPENSATED", "INVENTORY_RESERVE_FAILED", "COMPENSATION_FAILED"].includes(status)) {
      clearInterval(interval);
      handleSagaTerminalState(status, txId, amount, eventsRes.data);
    }
  }, 400);
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
    DOM.step3Title.textContent = "3. Ticket Confirmed";
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
   ZERO-JANK DIFFERENTIAL RECONCILIATION ENGINE
   ========================================================================== */

function startDifferentialPolling() {
  if (STATE.pollTimer) clearInterval(STATE.pollTimer);
  STATE.pollTimer = setInterval(async () => {
    const res = await fetchJson(`${CONFIG.inventoryUrl}/seats/`);
    if (res.ok && Array.isArray(res.data)) {
      reconcileSeatStates(res.data);
      updateInventoryStats();
    }
  }, CONFIG.pollIntervalMs);
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
                        (STATE.user && newSeat.held_by_user_id === (STATE.user.userId || STATE.user.actorId)) ||
                        (STATE.user && newSeat.user_id === (STATE.user.userId || STATE.user.actorId)));

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
        message: "All 30 venue seats have been reset to AVAILABLE in PostgreSQL & Redis!",
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
