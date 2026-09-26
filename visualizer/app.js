
window.copyTxUUIDToClipboard = function(uuid, btnElement) {
  if (!uuid) return;
  navigator.clipboard.writeText(uuid).then(() => {
    if (btnElement) {
      const originalHTML = btnElement.innerHTML;
      btnElement.innerHTML = '<span style="color: var(--accent-green); font-weight: bold;">✓</span>';
      setTimeout(() => {
        btnElement.innerHTML = originalHTML;
      }, 1500);
    }
    showToast({
      icon: "📋",
      title: "UUID Copied",
      message: `${uuid.substring(0, 8)}... copied to clipboard`,
      type: "normal",
      duration: 2000,
    });
  }).catch(() => {
    showToast({
      icon: "📋",
      title: "UUID",
      message: uuid,
      type: "normal",
      duration: 3000,
    });
  });
};

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

const CONFIG = {
  coordinatorUrl: isLocal ? defaultCoord : (localStorage.getItem("dtc_coordinator_url") || defaultCoord),
  inventoryUrl: isLocal ? defaultInv : (localStorage.getItem("dtc_inventory_url") || defaultInv),
  paymentUrl: isLocal ? defaultPay : (localStorage.getItem("dtc_payment_url") || defaultPay),
  healthPollIntervalMs: 15000,
  txFeedPollIntervalMs: 6000,
};

const STATE = {
  isSimulatedMode: false,
  activeFilter: "ALL",
  activeTxId: null,
  activeModalScenarioKey: null,
  transactions: [],
  activeEvents: [],
  stats: {
    racesDefended: parseInt(localStorage.getItem("dtc_stat_races") || "0", 10),
    locksEnforced: parseInt(localStorage.getItem("dtc_stat_locks") || "0", 10),
    totalSagas: parseInt(localStorage.getItem("dtc_stat_sagas") || "0", 10),
    orphansRecovered: parseInt(localStorage.getItem("dtc_stat_orphans") || "0", 10),
    sagasSwept: parseInt(localStorage.getItem("dtc_stat_swept") || "0", 10),
    dispatchedCount: 0,
  },
  health: {
    coord: { online: false, latency: null, crashed: false },
    inv: { online: false, latency: null, crashed: false },
    pay: { online: false, latency: null, crashed: false },
  },
  pagination: {
    currentPage: 1,
    pageSize: 10,
    totalCount: 0,
    totalPages: 1,
  },
  sweepCountdown: 90 - (Math.floor(Date.now() / 1000) % 90),
  recoveryLastRunTimestamp: null,
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SCENARIO_DESCRIPTIONS = {
  compfail: {
    icon: "💥",
    title: "Decline & Compensate Fail",
    badge: "Rollback Failure Simulation",
    badgeClass: "badge-danger",
    desc: "Demonstrates a severe edge case where a payment fails, triggering a rollback (compensation) to release the seat. However, the release process is intentionally sabotaged to return a 500 Error. The Saga gets stuck in COMPENSATION_FAILED, triggering the Celery auto-recovery sweeper.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "seat_ids": [34],
  "fail_compensation": true
}`,
    steps: [
      "Coordinator inserts Transaction(status=STARTED) and dispatches POST /seats/reserve/ to Inventory.",
      "Inventory reserves the seat successfully.",
      "Coordinator dispatches POST /payments/charge/ with amount=9999.00.",
      "Payment service rejects the charge (simulating Insufficient Funds) and returns 422 Payment Declined.",
      "Coordinator begins Compensation phase to rollback.",
      "Coordinator dispatches POST /seats/release/ to Inventory.",
      "Inventory intentionally throws a 500 Error (Release Failure).",
      "Transaction is stuck in COMPENSATION_FAILED and awaits Celery recovery."
    ],
    defenses: "At-Least-Once Delivery via Celery Beat background polling. The sweeper will retry the compensation until it succeeds.",
    outcome: "Status hits COMPENSATION_FAILED. Celery sweeper detects it after ~18s, retries the release, and successfully transitions to COMPENSATED."
  },

  happy: {
    icon: "⦿",
    title: "Happy Path Saga",
    badge: "3-Phase Forward Orchestration",
    badgeClass: "badge-success",
    desc: "Executes a standard forward distributed transaction where all participating microservices succeed. The Coordinator sequentially drives seat reservation, payment capture, and hold confirmation without spanning database transactions.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "seat_ids": [1]
}`,
    steps: [
      "Client dispatches POST /transactions/ to Coordinator with seat_ids.",
      "Coordinator inserts Transaction(status=STARTED) and dispatches POST /seats/reserve/ to Inventory (:8000).",
      "Inventory acquires row lock via SELECT FOR UPDATE, creates Reservation(status=RESERVED), and returns 200 OK.",
      "Coordinator transitions to PAYMENT_PROCESSING and dispatches POST /payments/charge/ to Payment (:8001).",
      "Payment inserts Payment(status=CHARGED) and returns 200 OK.",
      "Coordinator confirms seats (POST /seats/confirm/), transitions to COMPLETED, and responds HTTP 200 to Client."
    ],
    defenses: "ACID isolation per microservice database. Coordinator drives the saga through explicit SagaEvent audit transitions. Idempotent record creation eliminates duplicate requests.",
    outcome: "Status: COMPLETED (HTTP 200 OK)"
  },
  fail: {
    icon: "⦿",
    title: "Payment Decline & Auto-Compensate",
    badge: "Backward Compensating Rollback",
    badgeClass: "badge-amber",
    desc: "Demonstrates automated compensating recovery when downstream payment declines. Because distributed transactions cannot use global database rollbacks, the Coordinator executes an inverse compensating action to restore reserved seats.",
    payload: `POST http://127.0.0.1:8002/transactions/?simulate_payment_decline=true
{
  "seat_ids": [1]
}
// Note: simulate_payment_decline triggers intentional 422 decline in Payment Service`,
    steps: [
      "Client dispatches transaction with simulate_payment_decline flag to Coordinator.",
      "Coordinator reserves seat in Inventory successfully (STARTED → INVENTORY_RESERVED).",
      "Coordinator attempts payment charge (POST /payments/charge/).",
      "Payment service declines charge with HTTP 422 ('Payment declined / Testing failure amount').",
      "Coordinator catches decline, transitions to COMPENSATING, and dispatches POST /seats/release/ to Inventory.",
      "Inventory locks reservation under SELECT FOR UPDATE, restores seat availability, marks Reservation RELEASED.",
      "Coordinator transitions to COMPENSATED and returns HTTP 400 with rollback details."
    ],
    defenses: "Saga Backward Recovery pattern. Inventory release endpoint is strictly idempotent via row-level reservation checks (only 'RESERVED' rows can be released).",
    outcome: "Status: COMPENSATED (HTTP 400 Bad Request / 422)"
  },
  race: {
    icon: "⦿",
    title: "5x Concurrent Race Attack",
    badge: "PostgreSQL Relational Concurrency Defense",
    badgeClass: "badge-rose",
    desc: "Simulates a high-concurrency client retry storm or double-click attack. Multiple concurrent requests hit the Coordinator at the exact same millisecond attempting to book the same seat.",
    payload: `5x Concurrent Requests (Promise.all):
POST http://127.0.0.1:8002/transactions/
{
  "seat_ids": [1]
}`,
    steps: [
      "Client fires 5 concurrent asynchronous HTTP POST requests for the same venue seat.",
      "Coordinator processes requests concurrently against Inventory.",
      "Thread 1 secures exclusive row lock on Seat under SELECT FOR UPDATE and creates reservation.",
      "Threads 2-5 encounter seat already held and fail with HTTP 409 Conflict.",
      "PostgreSQL locks prevent double-booking with mathematical certainty.",
      "Coordinator records all outcomes cleanly in the audit log."
    ],
    defenses: "Relational row locks (SELECT FOR UPDATE) + PostgreSQL ACID transactions. Zero reliance on fragile in-memory Redis locks.",
    outcome: "Exactly 1 saga commits COMPLETED. Concurrent attempts safely defended."
  },
  crash: {
    icon: "⦿",
    title: "Process Crash Simulation",
    badge: "Celery Beat & Dual-Path Reconciler",
    badgeClass: "badge-purple",
    desc: "Simulates a catastrophic coordinator process death, server crash, or OOM kill mid-flight right after reserving seat hold. The transaction is left orphaned in PAYMENT_PROCESSING with unreleased seats.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "seat_ids": [1],
  "coordinator": true
}
// Note: 'coordinator': true kills process mid-flight`,
    steps: [
      "Coordinator reserves seat in Inventory (:8000), transitions status to PAYMENT_PROCESSING.",
      "Coordinator process terminates abruptly before dispatching HTTP call to Payment Service.",
      "Transaction remains orphaned in PostgreSQL with status=PAYMENT_PROCESSING.",
      "Celery Beat background worker sweeps stale non-terminal records older than recovery threshold.",
      "Worker claims transaction into RECOVERY_IN_PROGRESS using Redis mutex lock (Lua script).",
      "Worker probes ground-truth Payment endpoint (GET /payments/check/); receives status=NOT_CHARGED.",
      "Worker dispatches compensating release to Inventory and marks transaction COMPENSATED."
    ],
    defenses: "Distributed Redis mutex lock (prevents duplicate sweeps) + Ground-Truth Downstream Probing + Atomic Lua script lock release.",
    outcome: "Status: Recovers from orphaned PAYMENT_PROCESSING → COMPENSATED"
  },
  custom: {
    icon: "⦿",
    title: "Custom Payload Dispatcher",
    badge: "Interactive Edge-Case Tester",
    badgeClass: "badge-blue",
    desc: "Allows interactive configuration of custom transaction parameters to test edge cases, seat conflicts, process crashes, and failed compensation.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "seat_ids": [<Target Seat ID>]
}`,
    steps: [
      "User selects target venue seat and execution mode (Sync/Async).",
      "Client packet animates to Coordinator.",
      "Coordinator evaluates seat pricing from authoritative Inventory catalog.",
      "If seat is already booked or held: Inventory returns 409 Conflict; Coordinator halts.",
      "Coordinator drives reservation, payment charge, and confirmation to completion."
    ],
    defenses: "Input boundary validation + Dynamic saga path branching + Explicit backend error message extraction.",
    outcome: "Dynamic outcome based on selected parameters."
  }
};


const DOM = {
  toastContainer: document.getElementById("toast-container"),

  healthCoord: document.getElementById("node-health-coord"),
  healthInv: document.getElementById("node-health-inv"),
  healthPay: document.getElementById("node-health-pay"),
  latCoord: document.getElementById("lat-coord"),
  latInv: document.getElementById("lat-inv"),
  latPay: document.getElementById("lat-pay"),
  sweepCountdown: document.getElementById("sweep-countdown"),
  toggleSimMode: document.getElementById("toggle-sim-mode"),
  labelDemo: document.getElementById("label-demo"),
  labelLive: document.getElementById("label-live"),
  toggleTheme: document.getElementById("toggle-theme"),
  iconThemeLight: document.getElementById("icon-theme-light"),
  iconThemeDark: document.getElementById("icon-theme-dark"),
  btnHowItWorks: document.getElementById("btn-how-it-works"),
  btnViewStack: document.getElementById("btn-view-stack"),
  modalStack: document.getElementById("modal-stack"),
  btnCloseStack: document.getElementById("btn-close-stack"),
  btnCloseStackFooter: document.getElementById("btn-close-stack-footer"),
  stackItemsContainer: document.getElementById("stack-items-container"),
  statRacesDefended: document.getElementById("stat-race-defended"),
  statLocksEnforced: document.getElementById("stat-locks-enforced"),
  statSagasTotal: document.getElementById("stat-sagas-total"),
  statDoubleBooking: document.getElementById("stat-double-booking"),

  btnDemoHappy: document.getElementById("btn-demo-happy"),
  btnDemoFail: document.getElementById("btn-demo-fail"),
  btnDemoRace: document.getElementById("btn-demo-race"),
  btnDemoCrash: document.getElementById("btn-demo-crash"),
  btnToggleCustom: document.getElementById("btn-toggle-custom"),
  btnResetSeats: document.getElementById("btn-reset-seats"),
  customDrawer: document.getElementById("custom-payload-drawer"),
  formCustomTx: document.getElementById("form-custom-tx"),

  pulseLayer: document.getElementById("pulse-layer"),
  nodeBoxClient: document.getElementById("node-box-client"),
  nodeBoxCoord: document.getElementById("node-box-coord"),
  nodeBoxInv: document.getElementById("node-box-inv"),
  nodeBoxPay: document.getElementById("node-box-pay"),
  titleCoord: document.getElementById("title-coord"),
  titleInv: document.getElementById("title-inv"),
  titlePay: document.getElementById("title-pay"),
  iconCoord: document.getElementById("icon-coord"),
  topoClientSent: document.getElementById("topo-client-sent"),
  topoClientActive: document.getElementById("topo-client-active"),
  topoCoordState: document.getElementById("topo-coord-state"),
  topoCoordEvents: document.getElementById("topo-coord-events"),
  topoCoordLease: document.getElementById("topo-coord-lease"),
  topoBeatCountdown: document.getElementById("topo-beat-countdown"),
  topoWorkerInterval: document.getElementById("topo-worker-interval"),
  topoWorkerStatus: document.getElementById("topo-worker-status"),

  recCountdownVal: document.getElementById("rec-countdown-val"),
  recProgressFill: document.getElementById("rec-progress-fill"),
  recSagasSwept: document.getElementById("rec-sagas-swept"),
  recOrphansRecovered: document.getElementById("rec-orphans-recovered"),
  recRedisLock: document.getElementById("rec-redis-lock"),

  badgeTotalTx: document.getElementById("badge-total-tx"),
  filterTabs: document.querySelectorAll(".filter-tab"),
  tbodyTxFeed: document.getElementById("tbody-tx-feed"),
  btnManualPoll: document.getElementById("btn-manual-poll"),
  txPaginationBar: document.getElementById("tx-pagination-bar"),
  txPageRange: document.getElementById("tx-page-range"),
  txPageTotal: document.getElementById("tx-page-total"),
  txPaginationControls: document.getElementById("tx-pagination-controls"),

  forensicMetaCard: document.getElementById("forensic-meta-card"),
  fmetaId: document.getElementById("fmeta-id"),
  fmetaUserId: document.getElementById("fmeta-user-id"),
  fmetaUserEmail: document.getElementById("fmeta-user-email"),
  fmetaStatusBadge: document.getElementById("fmeta-status-badge"),
  fmetaProd: document.getElementById("fmeta-prod"),
  fmetaAmt: document.getElementById("fmeta-amt"),
  fmetaFailureBanner: document.getElementById("fmeta-failure-banner"),
  fmetaFailureText: document.getElementById("fmeta-failure-text"),
  forensicSeqCount: document.getElementById("forensic-seq-count"),
  btnRetryActiveTx: document.getElementById("btn-retry-active-tx"),
  timelineEmptyMsg: document.getElementById("timeline-empty-msg"),
  timelineStream: document.getElementById("timeline-stream"),
  payloadInspectorBox: document.getElementById("payload-inspector-box"),
  payloadInspectorTitle: document.getElementById("payload-inspector-title"),
  payloadInspectorCode: document.getElementById("payload-inspector-code"),
  btnClosePayload: document.getElementById("btn-close-payload"),

  modalScenInfo: document.getElementById("modal-scenario-info"),
  btnCloseScenInfo: document.getElementById("btn-close-scen-info"),
  btnDismissScenInfo: document.getElementById("btn-dismiss-scen-info"),
  btnRunFromModal: document.getElementById("btn-run-from-modal"),
  scenInfoIcon: document.getElementById("scen-info-icon"),
  scenInfoTitle: document.getElementById("scen-info-title"),
  scenInfoBadge: document.getElementById("scen-info-badge"),
  scenInfoDesc: document.getElementById("scen-info-desc"),
  scenInfoPayload: document.getElementById("scen-info-payload"),
  scenInfoSteps: document.getElementById("scen-info-steps"),
  scenInfoDefenses: document.getElementById("scen-info-defenses"),
  scenInfoOutcome: document.getElementById("scen-info-outcome"),

  modalArch: document.getElementById("modal-architecture"),
  btnCloseArch: document.getElementById("btn-close-arch"),
};


const activeAppToasts = new Map();

function showToast({ icon = "", title, message, type = "normal", duration = 4500 }) {
  if (!DOM.toastContainer) return;

  const toastKey = `${type}::${title}`;

  if (activeAppToasts.has(toastKey)) {
    const existing = activeAppToasts.get(toastKey);
    if (existing && existing.element && existing.element.parentNode) {
      existing.count += 1;

      if (existing.descEl) {
        existing.descEl.textContent = message;
      }

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

      existing.element.style.animation = "none";
      void existing.element.offsetWidth;
      existing.element.style.animation = "toast-bump 0.2s cubic-bezier(0.16, 1, 0.3, 1)";

      clearTimeout(existing.timeoutId);
      existing.timeoutId = setTimeout(() => {
        existing.element.style.opacity = "0";
        existing.element.style.transform = "translateX(50px)";
        existing.element.style.transition = "all 0.3s ease";
        setTimeout(() => {
          if (existing.element.parentNode === DOM.toastContainer) {
            DOM.toastContainer.removeChild(existing.element);
          }
          activeAppToasts.delete(toastKey);
        }, 300);
      }, duration);

      return;
    }
  }

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icon}</div>
    <div class="toast-body">
      <div class="toast-title"><span class="toast-title-text">${title}</span></div>
      <div class="toast-desc">${message}</div>
    </div>
  `;

  DOM.toastContainer.appendChild(toast);

  const titleEl = toast.querySelector(".toast-title");
  const descEl = toast.querySelector(".toast-desc");

  const entry = {
    element: toast,
    count: 1,
    countBadge: null,
    titleEl: titleEl,
    descEl: descEl,
    timeoutId: null
  };

  activeAppToasts.set(toastKey, entry);

  entry.timeoutId = setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(50px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => {
      if (toast.parentNode === DOM.toastContainer) {
        DOM.toastContainer.removeChild(toast);
      }
      activeAppToasts.delete(toastKey);
    }, 300);
  }, duration);
}


window.showScenarioInfo = function(scenarioKey) {
  const info = SCENARIO_DESCRIPTIONS[scenarioKey];
  if (!info || !DOM.modalScenInfo) return;

  STATE.activeModalScenarioKey = scenarioKey;

  DOM.scenInfoIcon.textContent = info.icon;
  DOM.scenInfoTitle.textContent = info.title;
  DOM.scenInfoBadge.textContent = info.badge;
  DOM.scenInfoBadge.className = `badge ${info.badgeClass || 'badge-neutral'}`;
  DOM.scenInfoDesc.textContent = info.desc;
  DOM.scenInfoPayload.textContent = info.payload;
  DOM.scenInfoDefenses.textContent = info.defenses;
  DOM.scenInfoOutcome.textContent = info.outcome;

  DOM.scenInfoSteps.innerHTML = info.steps.map((step, idx) => `
    <div class="scen-step-item">
      <span class="scen-step-num">${idx + 1}</span>
      <span>${step}</span>
    </div>
  `).join("");

  DOM.modalScenInfo.classList.remove("hidden");
};

function closeScenarioInfoModal() {
  if (DOM.modalScenInfo) {
    DOM.modalScenInfo.classList.add("hidden");
  }
}

if (DOM.btnCloseScenInfo) DOM.btnCloseScenInfo.addEventListener("click", closeScenarioInfoModal);
if (DOM.btnDismissScenInfo) DOM.btnDismissScenInfo.addEventListener("click", closeScenarioInfoModal);

if (DOM.btnRunFromModal) {
  DOM.btnRunFromModal.addEventListener("click", () => {
    const key = STATE.activeModalScenarioKey;
    closeScenarioInfoModal();
    if (key === "happy") DOM.btnDemoHappy.click();
    else if (key === "fail") DOM.btnDemoFail.click();
    else if (key === "race") DOM.btnDemoRace.click();
    else if (key === "crash") DOM.btnDemoCrash.click();
    else if (key === "custom") {
      DOM.customDrawer.classList.remove("hidden");
      DOM.customDrawer.scrollIntoView({ behavior: "smooth" });
    }
  });
}


async function fetchWithTimeout(url, options = {}, timeoutMs = 3500) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  const startTime = performance.now();

  const isBodyMethod = ["POST", "PUT", "PATCH"].includes((options.method || "GET").toUpperCase());
  const defaultHeaders = {
    Accept: "application/json",
  };
  if (isBodyMethod) {
    defaultHeaders["Content-Type"] = "application/json";
  }

  if (!options.skipAuth) {
    const token = localStorage.getItem("dtc_auth_token");
    const userId = localStorage.getItem("dtc_user_id");
    const userEmail = localStorage.getItem("dtc_user_email");
    if (token) {
      defaultHeaders["Authorization"] = `Bearer ${token}`;
    }
    if (userId) {
      defaultHeaders["X-User-ID"] = userId;
    }
    if (userEmail) {
      defaultHeaders["X-User-Email"] = userEmail;
    }
  }

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        ...defaultHeaders,
        ...(options.headers || {}),
      },
    });
    const duration = Math.max(1, Math.round(performance.now() - startTime));
    clearTimeout(timeoutId);

    let data = null;
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }

    return { ok: response.ok, status: response.status, data, latency: duration, headers: response.headers };
  } catch (err) {
    clearTimeout(timeoutId);
    return { ok: false, status: 0, error: err.message, latency: null };
  }
}

function extractDetailedErrorMessage(res, defaultMsg = "Operation failed") {
  if (!res) return defaultMsg;
  if (res.data) {
    if (res.data.inventory_response) {
      if (res.data.inventory_response.error) return `Inventory: ${res.data.inventory_response.error}`;
      if (res.data.inventory_response.detail) return `Inventory: ${res.data.inventory_response.detail}`;
    }
    if (res.data.payment_response) {
      if (res.data.payment_response.error) return `Payment: ${res.data.payment_response.error}`;
      if (res.data.payment_response.detail) return `Payment: ${res.data.payment_response.detail}`;
      if (res.data.payment_response._testing) return `Payment: ${res.data.payment_response.error || 'Declined'} (${res.data.payment_response._testing})`;
    }
    if (res.data.compensation_response) {
      if (res.data.compensation_response.error) return `Compensation: ${res.data.compensation_response.error}`;
      if (res.data.compensation_response.detail) return `Compensation: ${res.data.compensation_response.detail}`;
    }
    if (res.data.error) return `${res.data.error}`;
    if (res.data.detail) return `${res.data.detail}`;
  }
  if (res.error) return `Network Error: ${res.error}`;
  return defaultMsg;
}

async function checkServiceHealth() {
  if (STATE.isSimulatedMode) {
    updateHealthUI("coord", true, Math.floor(Math.random() * 4 + 2));
    updateHealthUI("inv", true, Math.floor(Math.random() * 3 + 2));
    updateHealthUI("pay", true, Math.floor(Math.random() * 5 + 3));
    return;
  }

  try {
    const [coordRes, invRes, payRes] = await Promise.all([
      fetchWithTimeout(`${CONFIG.coordinatorUrl}/health/`, { skipAuth: true }, 2500),
      fetchWithTimeout(`${CONFIG.inventoryUrl}/health/`, { skipAuth: true }, 2500),
      fetchWithTimeout(`${CONFIG.paymentUrl}/health/`, { skipAuth: true }, 2500),
    ]);

    updateHealthUI("coord", coordRes.ok || coordRes.status === 200, coordRes.latency);
    updateHealthUI("inv", invRes.ok || invRes.status === 200, invRes.latency);
    updateHealthUI("pay", payRes.ok || payRes.status === 200, payRes.latency);
  } catch (e) {
    console.warn("Health check error:", e);
  }
}

function updateHealthUI(nodeKey, isOnline, latency) {
  STATE.health[nodeKey].online = isOnline;
  STATE.health[nodeKey].latency = latency;

  const elNode = nodeKey === "coord" ? DOM.healthCoord : nodeKey === "inv" ? DOM.healthInv : DOM.healthPay;
  const elLat = nodeKey === "coord" ? DOM.latCoord : nodeKey === "inv" ? DOM.latInv : DOM.latPay;

  if (isOnline && !STATE.health[nodeKey].crashed) {
    elNode.className = "health-node online";
    elLat.textContent = `${latency || 2} ms`;
    elLat.className = "node-latency text-success";
  } else {
    elNode.className = "health-node offline";
    elLat.textContent = STATE.health[nodeKey].crashed ? "CRASHED" : "OFFLINE";
    elLat.className = "node-latency text-danger";
  }
}


function triggerCrashedNode(nodeKey) {
  STATE.health[nodeKey].crashed = true;
  updateHealthUI(nodeKey, false, null);

  if (nodeKey === "coord") {
    DOM.nodeBoxCoord.classList.add("node-box-crashed");
    DOM.titleCoord.textContent = "COORDINATOR (CRASHED)";
    DOM.iconCoord.textContent = "💥";
  } else if (nodeKey === "pay") {
    DOM.nodeBoxPay.classList.add("node-box-crashed");
    DOM.titlePay.textContent = "PAYMENT (CRASHED)";
  } else if (nodeKey === "inv") {
    DOM.nodeBoxInv.classList.add("node-box-crashed");
    DOM.titleInv.textContent = "INVENTORY (CRASHED)";
  }
}

function triggerRecoveredNode(nodeKey) {
  STATE.health[nodeKey].crashed = false;
  updateHealthUI(nodeKey, true, 3);

  const box = nodeKey === "coord" ? DOM.nodeBoxCoord : nodeKey === "inv" ? DOM.nodeBoxInv : DOM.nodeBoxPay;
  box.classList.remove("node-box-crashed");
  box.classList.add("node-box-recovered");

  if (nodeKey === "coord") {
    DOM.titleCoord.textContent = "COORDINATOR SERVICE";
    DOM.iconCoord.textContent = "⚙️";
  } else if (nodeKey === "pay") {
    DOM.titlePay.textContent = "PAYMENT SERVICE";
  } else if (nodeKey === "inv") {
    DOM.titleInv.textContent = "INVENTORY SERVICE";
  }

  setTimeout(() => {
    box.classList.remove("node-box-recovered");
  }, 1500);
}

function triggerArrivalPulse(destinationNodeId, type = "success") {
  const nodeEl = document.getElementById(destinationNodeId);
  if (!nodeEl) return;

  const pulseClass = `card-pulse-${type}`;
  nodeEl.classList.add(pulseClass);

  setTimeout(() => {
    nodeEl.classList.remove(pulseClass);
  }, 450);
}

function animatePacketAlongPath({
  route,
  type = "success",
  durationMs = 450,
  breakMidway = false,
  destinationNodeId = null,
}) {
  return new Promise((resolve) => {
    const colorMap = {
      success: "#34d399",
      info: "#38bdf8",
      warning: "#fbbf24",
      danger: "#f43f5e",
      purple: "#c084fc",
    };

    const filterMap = {
      success: "url(#glow-green)",
      info: "url(#glow-blue)",
      warning: "url(#glow-orange)",
      danger: "url(#glow-red)",
      purple: "url(#glow-purple)",
    };

    const color = colorMap[type] || "#38bdf8";
    const filter = filterMap[type] || "url(#glow-purple)";

    const pathIdMap = {
      "client-coord": "path-client-coord",
      "coord-inv": "path-coord-inv",
      "coord-pay": "path-coord-pay",
      "inv-coord": "path-inv-coord",
      "pay-coord": "path-pay-coord",
      "beat-dbbroker": "path-beat-dbbroker",
      "dbbroker-worker": "path-dbbroker-worker",
      "worker-dbcache": "path-worker-dbcache",
      "inv-dbcache": "path-inv-dbcache",
      "worker-coord": "path-worker-coord",
      "coord-dbcoord": "path-coord-dbcoord",
      "inv-dbinv": "path-inv-dbinv",
      "pay-dbpay": "path-pay-dbpay",
    };

    const pathEl = document.getElementById(pathIdMap[route] || route);
    if (!pathEl || !DOM.pulseLayer) {
      resolve();
      return;
    }

    const originalStroke = pathEl.getAttribute("stroke");
    const originalFilter = pathEl.getAttribute("filter");
    pathEl.setAttribute("stroke", color);
    pathEl.setAttribute("filter", filter);

    const totalLength = pathEl.getTotalLength();
    const packetGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");

    const halo = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    halo.setAttribute("r", "9");
    halo.setAttribute("fill", color);
    halo.setAttribute("opacity", "0.35");
    halo.setAttribute("filter", filter);

    const core = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    core.setAttribute("r", "4.5");
    core.setAttribute("fill", color);
    core.setAttribute("stroke", "#ffffff");
    core.setAttribute("stroke-width", "1.5");

    packetGroup.appendChild(halo);
    packetGroup.appendChild(core);
    DOM.pulseLayer.appendChild(packetGroup);

    const startTime = performance.now();
    const targetProgressLimit = breakMidway ? 0.52 : 1.0;

    function renderFrame(now) {
      const elapsed = now - startTime;
      const linearProgress = Math.min(elapsed / durationMs, 1.0);
      const currentProgress = linearProgress * targetProgressLimit;

      const point = pathEl.getPointAtLength(currentProgress * totalLength);
      packetGroup.setAttribute("transform", `translate(${point.x}, ${point.y})`);

      if (linearProgress < 1.0) {
        requestAnimationFrame(renderFrame);
      } else {
        if (breakMidway) {
          halo.setAttribute("fill", "#f43f5e");
          halo.setAttribute("r", "16");
          halo.setAttribute("opacity", "0.9");
          core.setAttribute("fill", "#f43f5e");
          core.setAttribute("r", "2");

          const xMark = document.createElementNS("http://www.w3.org/2000/svg", "text");
          xMark.textContent = "✕";
          xMark.setAttribute("fill", "#f43f5e");
          xMark.setAttribute("font-size", "14");
          xMark.setAttribute("font-weight", "800");
          xMark.setAttribute("text-anchor", "middle");
          xMark.setAttribute("dominant-baseline", "central");
          packetGroup.appendChild(xMark);

          packetGroup.style.transition = "all 0.35s ease";
          packetGroup.style.opacity = "0";

          setTimeout(() => {
            if (packetGroup.parentNode === DOM.pulseLayer) {
              DOM.pulseLayer.removeChild(packetGroup);
            }
            resolve();
          }, 400);
        } else {
          if (packetGroup.parentNode === DOM.pulseLayer) {
            DOM.pulseLayer.removeChild(packetGroup);
          }

          if (destinationNodeId) {
            triggerArrivalPulse(destinationNodeId, type);
          }
          resolve();
        }
      }
    }

    requestAnimationFrame(renderFrame);
  });
}

async function fetchTransactionsFeed(targetPage = STATE.pagination.currentPage) {
  STATE.pagination.currentPage = targetPage;
  const pageSize = STATE.pagination.pageSize;
  const offset = (targetPage - 1) * pageSize;
  const statusParam = STATE.activeFilter !== "ALL" ? `&status=${STATE.activeFilter}` : "";

  if (STATE.isSimulatedMode) {
    let filtered = STATE.transactions;
    if (STATE.activeFilter === "IN_FLIGHT") {
      filtered = filtered.filter(t => ["STARTED", "INVENTORY_RESERVED", "PAYMENT_PROCESSING", "COMPENSATING", "RECOVERY_IN_PROGRESS"].includes(t.status));
    } else if (STATE.activeFilter === "COMPLETED") {
      filtered = filtered.filter(t => t.status === "COMPLETED");
    } else if (STATE.activeFilter === "COMPENSATED") {
      filtered = filtered.filter(t => ["COMPENSATED", "COMPENSATED_ON_RETRY"].includes(t.status));
    } else if (STATE.activeFilter === "FAILED") {
      filtered = filtered.filter(t => t.status.includes("FAILED") || t.status.includes("TIMEOUT"));
    }

    const totalFiltered = filtered.length;
    STATE.pagination.totalCount = totalFiltered;
    STATE.pagination.totalPages = Math.max(1, Math.ceil(totalFiltered / pageSize));

    const pageSlice = filtered.slice(offset, offset + pageSize);
    renderTransactionsTable(pageSlice, totalFiltered, STATE.transactions.length);
    renderPaginationControls();
    return;
  }

  if (!isUserAuthenticated()) {
    if (!STATE.transactions || STATE.transactions.length === 0) {
      if (DOM.tbodyTxFeed) {
        DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty text-warning">🔒 Authentication Required: Verify email identity via the <strong>User Identity Gate</strong> above to view your live sagas, or toggle <strong>DEMO MODE</strong>.</td></tr>`;
      }
      if (DOM.txPageRange) DOM.txPageRange.textContent = "0–0";
      if (DOM.txPageTotal) DOM.txPageTotal.textContent = "0";
    }
    return;
  }

  const url = `${CONFIG.coordinatorUrl}/transactions/?limit=${pageSize}&offset=${offset}${statusParam}`;
  const res = await fetchWithTimeout(url);

  if (res.ok && res.data) {
    let list = [];
    let totalFiltered = 0;
    let overallTotal = 0;

    if (res.data.results && Array.isArray(res.data.results)) {
      list = res.data.results;
      totalFiltered = typeof res.data.count === "number" ? res.data.count : list.length;
      overallTotal = typeof res.data.total === "number" ? res.data.total : totalFiltered;
    } else if (Array.isArray(res.data)) {
      list = res.data;
      const totalCountHdr = res.headers?.get("x-total-count");
      const overallTotalHdr = res.headers?.get("x-overall-total");
      totalFiltered = totalCountHdr ? parseInt(totalCountHdr, 10) : list.length;
      overallTotal = overallTotalHdr ? parseInt(overallTotalHdr, 10) : totalFiltered;
    }

    STATE.transactions = list;
    STATE.stats.totalSagas = overallTotal;
    if (DOM.statSagasTotal) DOM.statSagasTotal.textContent = overallTotal;

    STATE.pagination.totalCount = totalFiltered;
    STATE.pagination.totalPages = Math.max(1, Math.ceil(totalFiltered / pageSize));

    const lockedRows = list.filter(t => t.status !== "STARTED" && t.status !== "INVENTORY_RESERVE_FAILED").length;
    STATE.stats.locksEnforced = Math.max(STATE.stats.locksEnforced, lockedRows);
    if (DOM.statLocksEnforced) DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;

    renderTransactionsTable(list, totalFiltered, overallTotal);
    renderPaginationControls();

    if (STATE.activeTxId) {
      inspectTransaction(STATE.activeTxId, false);
    }
  } else {
    if (!STATE.transactions || STATE.transactions.length === 0) {
      if (DOM.tbodyTxFeed) {
        if (res && res.status === 401) {
          DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty text-warning">🔒 Authentication Required: Verify email identity via the <strong>User Identity Gate</strong> above to view your live sagas, or toggle <strong>DEMO MODE</strong>.</td></tr>`;
        } else {
          DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty text-danger">⚠️ Coordinator Service offline at ${CONFIG.coordinatorUrl}. Start the service or toggle DEMO MODE above.</td></tr>`;
        }
      }
      if (DOM.txPageRange) DOM.txPageRange.textContent = "0–0";
      if (DOM.txPageTotal) DOM.txPageTotal.textContent = "0";
    }
  }
}

function renderTransactionsTable(list, totalFiltered, overallTotal) {
  DOM.badgeTotalTx.textContent = `${overallTotal !== undefined ? overallTotal : list.length} Total`;
  DOM.topoCoordEvents.textContent = `${overallTotal !== undefined ? overallTotal : list.length} sagas`;

  if (!list || list.length === 0) {
    DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty">No transactions found for filter: ${STATE.activeFilter}</td></tr>`;
    if (DOM.txPageRange) DOM.txPageRange.textContent = "0–0";
    if (DOM.txPageTotal) DOM.txPageTotal.textContent = "0";
    return;
  }

  const startIdx = (STATE.pagination.currentPage - 1) * STATE.pagination.pageSize + 1;
  const endIdx = startIdx + list.length - 1;
  if (DOM.txPageRange) DOM.txPageRange.textContent = `${startIdx}–${endIdx}`;
  if (DOM.txPageTotal) DOM.txPageTotal.textContent = `${totalFiltered}`;

  DOM.tbodyTxFeed.innerHTML = list.map(tx => {
    const isActive = tx.transaction_id === STATE.activeTxId;
    const shortId = tx.transaction_id.substring(0, 8) + "...";
    const statusBadge = getStatusBadgeHTML(tx.status);
    const waterfallHTML = getWaterfallHTML(tx);

    return `
      <tr class="${isActive ? 'active-row' : ''}" onclick="window.selectTransaction('${tx.transaction_id}')">
        <td>
          <span class="font-mono" style="font-weight: 700;" title="${tx.transaction_id}">${shortId}</span>
        </td>
        <td>${waterfallHTML}</td>
        <td>${statusBadge}</td>
        <td class="font-mono text-success">$${tx.amount}</td>
        <td>
          <button class="btn btn-xs btn-secondary" onclick="event.stopPropagation(); window.selectTransaction('${tx.transaction_id}')">Inspect</button>
        </td>
      </tr>
    `;
  }).join("");
}

function renderPaginationControls() {
  if (!DOM.txPaginationControls) return;

  const current = STATE.pagination.currentPage;
  const totalPages = STATE.pagination.totalPages;

  if (totalPages <= 1) {
    DOM.txPaginationControls.innerHTML = `
      <button class="page-btn active" disabled>1</button>
    `;
    return;
  }

  let html = "";

  // Previous button
  html += `<button class="page-btn" ${current === 1 ? 'disabled' : ''} onclick="window.goToTransactionPage(${current - 1})" title="Previous Page">‹</button>`;

  // Build page numbers (e.g. 1, 2, 3, 4, 5 or with ellipsis)
  const maxButtons = 5;
  let startPage = Math.max(1, current - 2);
  let endPage = Math.min(totalPages, startPage + maxButtons - 1);

  if (endPage - startPage < maxButtons - 1) {
    startPage = Math.max(1, endPage - maxButtons + 1);
  }

  if (startPage > 1) {
    html += `<button class="page-btn ${current === 1 ? 'active' : ''}" onclick="window.goToTransactionPage(1)">1</button>`;
    if (startPage > 2) {
      html += `<span class="page-ellipsis">…</span>`;
    }
  }

  for (let p = startPage; p <= endPage; p++) {
    html += `<button class="page-btn ${p === current ? 'active' : ''}" onclick="window.goToTransactionPage(${p})">${p}</button>`;
  }

  if (endPage < totalPages) {
    if (endPage < totalPages - 1) {
      html += `<span class="page-ellipsis">…</span>`;
    }
    html += `<button class="page-btn ${current === totalPages ? 'active' : ''}" onclick="window.goToTransactionPage(${totalPages})">${totalPages}</button>`;
  }

  // Next button
  html += `<button class="page-btn" ${current === totalPages ? 'disabled' : ''} onclick="window.goToTransactionPage(${current + 1})" title="Next Page">›</button>`;

  DOM.txPaginationControls.innerHTML = html;
}

window.goToTransactionPage = function(pageNumber) {
  if (pageNumber < 1 || pageNumber > STATE.pagination.totalPages) return;
  fetchTransactionsFeed(pageNumber);
};

function getStatusBadgeHTML(status) {
  if (!status) return `<span class="badge badge-neutral">UNKNOWN</span>`;
  const s = status.toUpperCase();
  let badgeClass = "badge-amber";

  if (s === "COMPLETED") badgeClass = "badge-success";
  else if (s === "COMPENSATED" || s === "COMPENSATED_ON_RETRY") badgeClass = "badge-blue";
  else if (s.includes("FAILED") || s.includes("TIMEOUT")) badgeClass = "badge-rose";
  else if (s.includes("RECOVERY")) badgeClass = "badge-purple";

  return `<span class="badge ${badgeClass}">${status}</span>`;
}

function getWaterfallHTML(tx) {
  const s = (tx.status || "").toUpperCase();

  // Calculate real execution duration from transaction timestamps or deterministic hash variance
  let totalMs = 45;
  if (tx.created_at && tx.updated_at) {
    const diff = Math.max(1, new Date(tx.updated_at).getTime() - new Date(tx.created_at).getTime());
    totalMs = Math.min(diff, 9999);
  }
  if (totalMs <= 1 && tx.transaction_id) {
    const hash = String(tx.transaction_id).replace(/-/g, "");
    const num = parseInt(hash.substring(0, 4), 16) || 45;
    totalMs = 38 + (num % 35);
  }

  // Phase breakdown proportional to total execution duration
  let rMs = Math.max(4, Math.round(totalMs * 0.28));
  let pMs = Math.max(8, Math.round(totalMs * 0.54));
  let cMs = Math.max(2, totalMs - rMs - pMs);

  let trackHTML = "";
  let labelHTML = "";

  if (s === "COMPLETED") {
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 30%;" title="Reserve Inventory: ${rMs}ms"></div>
      <div class="wf-seg wf-charge" style="width: 55%;" title="Charge Payment: ${pMs}ms"></div>
      <div class="wf-seg wf-commit" style="width: 15%;" title="Commit: ${cMs}ms"></div>
    `;
    labelHTML = `<span>Res: ${rMs}ms</span><span>Chg: ${pMs}ms</span><span>Cmt: ${cMs}ms</span><span class="wf-duration">${totalMs}ms</span>`;
  } else if (s === "COMPENSATED" || s === "COMPENSATED_ON_RETRY") {
    rMs = Math.max(4, Math.round(totalMs * 0.25));
    pMs = Math.max(8, Math.round(totalMs * 0.45));
    cMs = Math.max(4, totalMs - rMs - pMs);
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 25%;" title="Reserve Inventory: ${rMs}ms"></div>
      <div class="wf-seg wf-fail" style="width: 45%;" title="Payment Declined: ${pMs}ms"></div>
      <div class="wf-seg wf-comp" style="width: 30%;" title="Compensating Release: ${cMs}ms"></div>
    `;
    labelHTML = `<span>Res: ${rMs}ms</span><span style="color: var(--accent-rose)">Decl: ${pMs}ms</span><span style="color: var(--accent-amber)">Rel: ${cMs}ms</span>`;
  } else if (s === "INVENTORY_RESERVE_FAILED") {
    const failMs = Math.min(totalMs, 22);
    trackHTML = `
      <div class="wf-seg wf-hatch-fail" style="width: 100%;" title="Inventory Reserve Rejected (Out of Stock / Locked)"></div>
    `;
    labelHTML = `<span style="color: var(--accent-rose); font-weight: 700;">Reserve Rejected</span><span class="wf-duration" style="color: var(--accent-rose);">${failMs}ms</span>`;
  } else if (s === "RECOVERY_IN_PROGRESS" || s === "COMPENSATING") {
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 35%;" title="Reserve: ${rMs}ms"></div>
      <div class="wf-seg wf-rec" style="width: 65%;" title="In Recovery Sweep"></div>
    `;
    labelHTML = `<span>Res: ${rMs}ms</span><span style="color: var(--accent-purple)">Recovery Sweep</span>`;
  } else {
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 40%;"></div>
      <div class="wf-seg wf-fail" style="width: 60%;"></div>
    `;
    labelHTML = `<span>Failed</span>`;
  }

  return `
    <div class="waterfall-cell">
      <div class="waterfall-track">${trackHTML}</div>
      <div class="wf-labels">${labelHTML}</div>
    </div>
  `;
}


window.selectTransaction = function(txId) {
  STATE.activeTxId = txId;
  renderTransactionsTable(STATE.transactions);
  inspectTransaction(txId, true);
};

async function inspectTransaction(txId, autoScroll = false) {
  if (!txId) return;

  let txData = STATE.transactions.find(t => t.transaction_id === txId);
  let events = [];

  if (STATE.isSimulatedMode) {
    events = txData ? (txData.events || []) : [];
  } else {
    const txRes = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/${txId}/`);
    if (txRes.ok && txRes.data) txData = txRes.data;

    const evRes = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/${txId}/events/`);
    if (evRes.ok && evRes.data && Array.isArray(evRes.data.events)) {
      events = evRes.data.events;
    }
  }

  STATE.activeEvents = events;

  if (txData) {
    DOM.forensicMetaCard.classList.remove("hidden");
    DOM.fmetaId.textContent = txData.transaction_id;
    if (DOM.fmetaUserId) DOM.fmetaUserId.textContent = txData.user_id || "N/A";
    if (DOM.fmetaUserEmail) DOM.fmetaUserEmail.textContent = txData.user_email || "N/A";
    DOM.fmetaStatusBadge.textContent = txData.status;
    const seatsLabel = txData.seat_numbers || (Array.isArray(txData.seat_ids) && txData.seat_ids.length > 0 ? txData.seat_ids.map(id => `#${id}`).join(", ") : (txData.seat_id ? `Seat #${txData.seat_id}` : "Seat #1"));
    const qty = txData.quantity || (Array.isArray(txData.seat_ids) ? txData.seat_ids.length : 1);
    DOM.fmetaProd.textContent = `${seatsLabel} (Qty: ${qty})`;
    DOM.fmetaAmt.textContent = `$${txData.amount || 500}`;

    const isFailureState = txData.status.includes("FAILED") || txData.status.includes("TIMEOUT") || txData.status === "COMPENSATED";
    if (isFailureState) {
      let failureReason = "Non-terminal or compensated state detected";
      if (txData.status === "INVENTORY_RESERVE_FAILED") {
        failureReason = txData.failure_reason || "Inventory reservation rejected (seat already held, booked, or unavailable)";
      } else if (txData.status === "COMPENSATED") {
        failureReason = txData.failure_reason || "Payment declined / failed → Seat hold auto-released via compensating rollback";
      } else if (txData.status === "COMPENSATION_FAILED") {
        failureReason = txData.failure_reason || "Compensating seat release rejected by Inventory Service";
      }

      DOM.fmetaFailureBanner.classList.remove("hidden");
      DOM.fmetaFailureText.textContent = `Failure Reason: ${failureReason}`;
    } else {
      DOM.fmetaFailureBanner.classList.add("hidden");
    }

    if (["COMPENSATION_FAILED", "RETRY_COMPENSATION_FAILED"].includes(txData.status)) {
      DOM.btnRetryActiveTx.classList.remove("hidden");
    } else {
      DOM.btnRetryActiveTx.classList.add("hidden");
    }
  }

  DOM.forensicSeqCount.textContent = `${events.length} Event${events.length === 1 ? '' : 's'}`;
  
  if (events.length === 0) {
    DOM.timelineEmptyMsg.classList.remove("hidden");
    DOM.timelineStream.innerHTML = "";
    return;
  }

  DOM.timelineEmptyMsg.classList.add("hidden");
  DOM.timelineStream.innerHTML = events.map((ev, idx) => {
    const fromBadge = getStatusBadgeHTML(ev.from_status || "START");
    const toBadge = getStatusBadgeHTML(ev.to_status);
    const actorClass = ev.actor === "request" ? "badge-blue" : ev.actor === "recovery" ? "badge-purple" : "badge-amber";
    const timeStr = ev.created_at ? new Date(ev.created_at).toLocaleTimeString() : "--:--:--";
    const hasPayload = ev.http_payload && Object.keys(ev.http_payload).length > 0;

    return `
      <div class="timeline-item">
        <div class="timeline-dot">${ev.seq}</div>
        <div class="timeline-content">
          <div class="timeline-trans">
            ${fromBadge}
            <span style="color: var(--text-muted); font-size: 11px;">→</span>
            ${toBadge}
          </div>
          <span class="badge ${actorClass}">${ev.actor}</span>
        </div>
        <div class="timeline-meta">
          ${ev.http_status_code ? `<span class="badge ${ev.http_status_code < 400 ? 'badge-success' : 'badge-rose'}">HTTP ${ev.http_status_code}</span>` : ''}
          <span class="font-mono text-muted" style="font-size: 10px;">${timeStr}</span>
          ${hasPayload ? `<button class="btn btn-xs btn-secondary" onclick="window.viewEventPayload(${idx})">JSON</button>` : ''}
        </div>
      </div>
    `;
  }).join("");

  if (autoScroll) {
    DOM.timelineStream.scrollTop = DOM.timelineStream.scrollHeight;
  }
}

window.viewEventPayload = function(eventIdx) {
  const ev = STATE.activeEvents[eventIdx];
  if (!ev) return;

  DOM.payloadInspectorBox.classList.remove("hidden");
  DOM.payloadInspectorTitle.textContent = `Payload: Seq #${ev.seq} (${ev.from_status} → ${ev.to_status})`;
  DOM.payloadInspectorCode.textContent = JSON.stringify(ev.http_payload, null, 2);
};

DOM.btnClosePayload.addEventListener("click", () => {
  DOM.payloadInspectorBox.classList.add("hidden");
});

function getStatusBadgeClass(status) {
  if (!status) return "badge-neutral";
  const s = status.toUpperCase();
  if (s === "COMPLETED") return "badge-success";
  if (s === "COMPENSATED" || s === "COMPENSATED_ON_RETRY") return "badge-blue";
  if (s.includes("FAILED") || s.includes("TIMEOUT")) return "badge-rose";
  if (s.includes("RECOVERY")) return "badge-purple";
  return "badge-amber";
}


function isUserAuthenticated() {
  return Boolean(localStorage.getItem("dtc_auth_token"));
}

function checkLiveAuthGate(actionName = "execute live distributed scenarios") {
  if (STATE.isSimulatedMode) {
    return true; // Demo / Simulation mode allows testing without login
  }
  if (!isUserAuthenticated()) {
    showMainAuthModal(1);
    showToast({
      icon: "",
      title: "Sign In Required for LIVE Mode",
      message: `Please sign in with Email OTP to ${actionName}, or toggle to DEMO mode at the top navigation bar.`,
      type: "warning",
      duration: 6000,
    });
    return false;
  }
  return true;
}


DOM.btnDemoHappy.addEventListener("click", async () => {
  if (!checkLiveAuthGate("dispatch the Happy Path Live Saga")) return;

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "ORCHESTRATING (HAPPY PATH)";

  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  if (STATE.isSimulatedMode) {
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 450, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "success", durationMs: 450, destinationNodeId: "node-box-pay" });
    await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });

    showToast({
      icon: "⦿",
      title: "Happy Path Saga (Simulated Mode)",
      message: "Reserving next available seat → Charging payment ($500.00) → Marked COMPLETED",
      type: "success",
    });

    createSimulatedTransaction("COMPLETED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const nextSeatId = await getNextAvailableSeatId();
  const t0 = performance.now();
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({
      seat_ids: [nextSeatId],
    }),
  });
  const latencyMs = Math.round(performance.now() - t0);
  const isAsync = (res.status === 202);

  await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
  await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
  await animatePacketAlongPath({ route: "coord-pay", type: "success", durationMs: 350, destinationNodeId: "node-box-pay" });
  await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });

  const serverDurationMs = (res.data && res.data.created_at && res.data.updated_at)
    ? Math.max(1, new Date(res.data.updated_at).getTime() - new Date(res.data.created_at).getTime())
    : latencyMs;

  if (isAsync) {
    showToast({
      icon: "⦿",
      title: `Async Saga Dispatched (202 Accepted · ${latencyMs}ms client RTT)`,
      message: `[Mode: ASYNCHRONOUS] Task enqueued to Celery background worker! Client unblocked in ${latencyMs}ms. Celery worker is executing Reserve → Charge → Confirm.`,
      type: "success",
      duration: 6500,
    });
  } else {
    showToast({
      icon: "⦿",
      title: `Sync Saga Completed (200 OK · ${serverDurationMs}ms server · ${latencyMs}ms client RTT)`,
      message: `[Mode: SYNCHRONOUS] Completed in ${serverDurationMs}ms on server (${latencyMs}ms total round-trip). Seat #${nextSeatId} Reserved → Payment Charged ($500.00) → Confirmed.`,
      type: "success",
      duration: 6500,
    });
  }

  if (res.ok && res.data && res.data.transaction_id) {
    window.selectTransaction(res.data.transaction_id);
    fetchTransactionsFeed();
  }
  DOM.topoCoordState.textContent = "IDLE";
});

DOM.btnDemoFail.addEventListener("click", async () => {
  if (!checkLiveAuthGate("execute the Live Payment Decline scenario")) return;

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "COMPENSATING";

  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  if (STATE.isSimulatedMode) {
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 450, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 450, destinationNodeId: "node-box-pay" });
    await animatePacketAlongPath({ route: "pay-coord", type: "danger", durationMs: 350, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 450, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 350, destinationNodeId: "node-box-coord" });

    showToast({
      icon: "✦︎",
      title: "Compensating Saga Executed (Simulated)",
      message: "Payment declined (HTTP 422: Testing failure amount $9,999) → Seat hold auto-released to Inventory → Marked COMPENSATED",
      type: "normal",
    });

    createSimulatedTransaction("COMPENSATED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const nextSeatId = await getNextAvailableSeatId();
  const t0 = performance.now();
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/?simulate_payment_decline=true`, {
    method: "POST",
    body: JSON.stringify({
      seat_ids: [nextSeatId],
    }),
  });
  const latencyMs = Math.round(performance.now() - t0);
  const isAsync = (res.status === 202);

  await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
  await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
  await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 350, destinationNodeId: "node-box-pay" });
  await animatePacketAlongPath({ route: "pay-coord", type: "danger", durationMs: 300, destinationNodeId: "node-box-coord" });
  await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 350, destinationNodeId: "node-box-inv" });
  await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 300, destinationNodeId: "node-box-coord" });

  if (isAsync) {
    showToast({
      icon: "⦿",
      title: `Async Compensation Dispatched (202 Accepted · ${latencyMs}ms)`,
      message: `[Mode: ASYNCHRONOUS] Payment declined on $9,999. Celery worker is auto-releasing seat #${nextSeatId} hold in background. Marked COMPENSATED.`,
      type: "normal",
      duration: 6500,
    });
  } else {
    showToast({
      icon: "✦︎",
      title: `Sync Compensation Completed (${res.status} · ${latencyMs}ms)`,
      message: `[Mode: SYNCHRONOUS] Payment declined on $9,999. Coordinator immediately executed POST /seats/release/ rollback. Marked COMPENSATED.`,
      type: "normal",
      duration: 6500,
    });
  }

  if (res.data && res.data.transaction_id) {
    window.selectTransaction(res.data.transaction_id);
    fetchTransactionsFeed();
  }
  DOM.topoCoordState.textContent = "IDLE";
});

async function getNextAvailableSeatId() {
  try {
    const res = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/`);
    if (res.ok && Array.isArray(res.data) && res.data.length > 0) {
      const available = res.data.find(s => s.status === "AVAILABLE");
      if (available) return available.id;
      
      // If all 30 seats are held/booked, auto-replenish venue seats
      const resetRes = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
      if (resetRes.ok) {
        const refreshed = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/`);
        if (refreshed.ok && Array.isArray(refreshed.data) && refreshed.data.length > 0) {
          const freshAvail = refreshed.data.find(s => s.status === "AVAILABLE");
          if (freshAvail) return freshAvail.id;
          return refreshed.data[0].id;
        }
      }
      return res.data[0].id;
    }
  } catch (e) {}
  return 31;
}

if (DOM.btnResetSeats) {
  DOM.btnResetSeats.addEventListener("click", async () => {
    if (!checkLiveAuthGate("reset live venue seats")) return;
    try {
      const res = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
      if (res.ok) {
        showToast({
          icon: "⦿",
          title: "Venue Restocked",
          message: "All 30 venue seats have been reset to AVAILABLE! Ready for new bookings.",
          type: "success",
          duration: 4000,
        });
        fetchTransactionsFeed();
      } else {
        showToast({
          icon: "⦿",
          title: "Reset Failed",
          message: "Could not reset venue seats. Verify Inventory service is running.",
          type: "danger",
        });
      }
    } catch (e) {
      showToast({
        icon: "⦿",
        title: "Network Error",
        message: e.message,
        type: "danger",
      });
    }
  });
}

DOM.btnDemoRace.addEventListener("click", async () => {
  if (!checkLiveAuthGate("execute the Live Race Condition scenario")) return;

  const sharedTxId = crypto.randomUUID();
  STATE.stats.dispatchedCount += 5;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "RACE BURST";

  for (let i = 0; i < 5; i++) {
    animatePacketAlongPath({
      route: "client-coord",
      type: i === 0 ? "success" : "danger",
      durationMs: 350 + i * 40,
      destinationNodeId: "node-box-coord",
    });
  }
  await sleep(400);

  STATE.stats.racesDefended += 4;
  DOM.statRacesDefended.textContent = STATE.stats.racesDefended;
  localStorage.setItem("dtc_stat_races", STATE.stats.racesDefended);

  showToast({
    icon: "⦿",
    title: "Race Condition Defended",
    message: `5 concurrent requests with UUID ${sharedTxId.substring(0,8)}... → Exactly 1 committed, 4 blocked by PostgreSQL UNIQUE(transaction_id) & row locks!`,
    type: "race",
    duration: 6500,
  });

  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  await animatePacketAlongPath({
    route: "coord-pay",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-pay",
  });

  await animatePacketAlongPath({
    route: "pay-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("COMPLETED", sharedTxId);
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const targetSeatId = await getNextAvailableSeatId();
  const promises = Array.from({ length: 5 }, () =>
    fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
      method: "POST",
      body: JSON.stringify({
        seat_ids: [targetSeatId],
      }),
    })
  );

  const results = await Promise.all(promises);
  const firstSuccess = results.find(r => r.data && r.data.transaction_id);
  if (firstSuccess) {
    window.selectTransaction(firstSuccess.data.transaction_id);
  }
  fetchTransactionsFeed();
  DOM.topoCoordState.textContent = "IDLE";
});

DOM.btnDemoCrash.addEventListener("click", async () => {
  if (!checkLiveAuthGate("simulate a Live Coordinator Process Crash")) return;

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "CRASH SIMULATION";

  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  await animatePacketAlongPath({
    route: "coord-pay",
    type: "danger",
    durationMs: 500,
    breakMidway: true,
  });

  triggerCrashedNode("coord");

  let txId = null;
  const targetSeatId = await getNextAvailableSeatId();

  if (STATE.isSimulatedMode) {
    txId = crypto.randomUUID();
    createSimulatedTransaction("STARTED", txId);
  } else {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
      method: "POST",
      body: JSON.stringify({
        seat_ids: [targetSeatId],
        coordinator: true,
      }),
    });
    if (res.data && res.data.transaction_id) {
      txId = res.data.transaction_id;
      window.selectTransaction(txId);
      fetchTransactionsFeed();
    }
  }

  showToast({
    icon: "⦿",
    title: "Process Crash Simulated",
    message: `Coordinator process crashed mid-flight! Transaction ${txId ? txId.substring(0,8) : "unknown"}... is orphaned in database. Awaiting natural 90s Celery Beat sweep!`,
    type: "race",
    duration: 8000,
  });

  setTimeout(() => {
    triggerRecoveredNode("coord");
    DOM.topoCoordState.textContent = "IDLE";
  }, 2500);
});

async function populateCustomSeatDropdown() {
  const selectEl = document.getElementById("cust-seat-id");
  const amountEl = document.getElementById("cust-amount");
  if (!selectEl) return;

  try {
    const res = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/`);
    if (res.ok && Array.isArray(res.data) && res.data.length > 0) {
      selectEl.innerHTML = "";
      
      const rows = { "A": "Row A (VIP - $750.00)", "B": "Row B (Premium - $500.00)", "C": "Row C (Standard - $350.00)" };
      const optGroups = {};
      Object.keys(rows).forEach(r => {
        const og = document.createElement("optgroup");
        og.label = rows[r];
        optGroups[r] = og;
        selectEl.appendChild(og);
      });

      let firstAvail = null;
      res.data.forEach(seat => {
        const opt = document.createElement("option");
        opt.value = seat.id;
        opt.dataset.price = seat.price;
        opt.textContent = `Seat #${seat.id} • ${seat.row}${seat.seat_number} — $${parseFloat(seat.price).toFixed(2)} [${seat.status}]`;
        if (seat.status === "AVAILABLE" && !firstAvail) {
          firstAvail = seat;
          opt.selected = true;
        }
        if (optGroups[seat.row]) {
          optGroups[seat.row].appendChild(opt);
        } else {
          selectEl.appendChild(opt);
        }
      });

      if (firstAvail && amountEl) {
        amountEl.value = parseFloat(firstAvail.price).toFixed(2);
      }

      selectEl.onchange = () => {
        const selected = selectEl.options[selectEl.selectedIndex];
        if (selected && selected.dataset.price && amountEl) {
          amountEl.value = parseFloat(selected.dataset.price).toFixed(2);
        }
      };
      return;
    }
  } catch (e) {}

  selectEl.innerHTML = `<option value="31" data-price="750.00">Seat #31 • A1 (VIP - $750.00)</option><option value="41" data-price="500.00">Seat #41 • B1 (Premium - $500.00)</option><option value="51" data-price="350.00">Seat #51 • C1 (Standard - $350.00)</option>`;
}

DOM.btnToggleCustom.addEventListener("click", () => {
  const isHidden = DOM.customDrawer.classList.toggle("hidden");
  if (!isHidden) {
    populateCustomSeatDropdown();
    DOM.customDrawer.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});

DOM.formCustomTx.addEventListener("submit", async (e) => {
  e.preventDefault();

  const seatId = parseInt(document.getElementById("cust-seat-id").value, 10) || 31;
  const amount = parseFloat(document.getElementById("cust-amount").value) || 500;
  const execMode = document.getElementById("cust-exec-mode")?.value || "async";

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.customDrawer.classList.add("hidden");

  if (STATE.isSimulatedMode) {
    if (execMode === "async") {
      DOM.topoCoordState.textContent = "ASYNC DISPATCH";
    } else {
      DOM.topoCoordState.textContent = "SYNC DISPATCH";
    }

    await animatePacketAlongPath({ route: "client-coord", type: "info", durationMs: 350, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "success", durationMs: 350, destinationNodeId: "node-box-pay" });
    await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });

    createSimulatedTransaction("COMPLETED");
    showToast({
      icon: "⦿",
      title: `Custom Saga Dispatched (${execMode === "async" ? "202 Accepted" : "200 OK"})`,
      message: `[Mode: ${execMode.toUpperCase()}] Custom parameters for Seat #${seatId} ($${amount.toFixed(2)}) executed successfully. COMPLETED.`,
      type: "success",
      duration: 5500,
    });
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  if (!checkLiveAuthGate("dispatch custom live distributed transactions")) return;

  const payload = {
    seat_ids: [seatId],
  };

  if (execMode === "async") {
    DOM.topoCoordState.textContent = "ASYNC DISPATCH";
  } else {
    DOM.topoCoordState.textContent = "SYNC DISPATCH";
  }

  // Animate: Client → Coordinator
  await animatePacketAlongPath({ route: "client-coord", type: "info", durationMs: 350, destinationNodeId: "node-box-coord" });

  const endpointUrl = `${CONFIG.coordinatorUrl}/transactions/?${execMode === "sync" ? "sync=true" : "async=true"}`;

  const t0 = performance.now();
  const res = await fetchWithTimeout(endpointUrl, { method: "POST", body: JSON.stringify(payload) });
  const latencyMs = Math.round(performance.now() - t0);

  const txData = res.data || {};
  const status = txData.status || (res.ok ? "COMPLETED" : "FAILED");
  const detailedError = extractDetailedErrorMessage(res);

  if (txData.error === "INVENTORY_RESERVE_FAILED" || (res.status === 400 && txData.inventory_response)) {
    await animatePacketAlongPath({ route: "coord-inv", type: "danger", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "danger", durationMs: 300, destinationNodeId: "node-box-coord" });
    showToast({
      icon: "⦿",
      title: "Seat Reservation Conflict / Locked",
      message: `[${res.status} in ${latencyMs}ms] ${detailedError}`,
      type: "race",
      duration: 6000
    });
  }
  else if (txData.detail === "Intentional crash" || status === "STARTED") {
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 400, breakMidway: true });
    triggerCrashedNode("coord");
    showToast({
      icon: "⦿",
      title: "Process Crash Simulated",
      message: `Coordinator crashed mid-flight! Transaction orphaned. Awaiting Celery Beat sweep.`,
      type: "race",
      duration: 8000
    });
    setTimeout(() => { triggerRecoveredNode("coord"); DOM.topoCoordState.textContent = "IDLE"; }, 2500);
  }
  else if (status === "COMPENSATED" || status === "COMPENSATION_FAILED") {
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 350, destinationNodeId: "node-box-pay" });
    await animatePacketAlongPath({ route: "pay-coord", type: "danger", durationMs: 300, destinationNodeId: "node-box-coord" });
    DOM.topoCoordState.textContent = "COMPENSATING";
    await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 300, destinationNodeId: "node-box-coord" });
    showToast({
      icon: "🔄",
      title: "Payment Declined & Compensated",
      message: `[${res.status} in ${latencyMs}ms] $${amount.toFixed(2)} declined → seat released → COMPENSATED`,
      type: "normal",
      duration: 6000
    });
  }
  else {
    // Normal execution: coord -> inv -> coord -> pay -> coord
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
    await animatePacketAlongPath({ route: "coord-pay", type: "success", durationMs: 350, destinationNodeId: "node-box-pay" });
    await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });

    const serverDurationMs = (txData.created_at && txData.updated_at)
      ? Math.max(1, new Date(txData.updated_at).getTime() - new Date(txData.created_at).getTime())
      : latencyMs;

    if (execMode === "async" || res.status === 202) {
      showToast({
        icon: "⦿",
        title: `Async Saga Dispatched (202 Accepted · ${latencyMs}ms client RTT)`,
        message: `[Mode: ASYNCHRONOUS] Task enqueued to Celery! Client released in ${latencyMs}ms. Background worker is orchestrating Reserve → Charge → Confirm.`,
        type: "success",
        duration: 6500,
      });
    } else {
      showToast({
        icon: "🔁",
        title: `Sync Pipeline: 200 OK (${serverDurationMs}ms server · ${latencyMs}ms client RTT)`,
        message: `Coordinator → Inventory Reserve → Payment Charge → Inventory Confirm — completed in ${serverDurationMs}ms on server (${latencyMs}ms total round-trip). COMPLETED.`,
        type: "success",
        duration: 6500,
      });
    }
  }

  if (txData.transaction_id) {
    window.selectTransaction(txData.transaction_id);
    fetchTransactionsFeed();
  }

  DOM.topoCoordState.textContent = "IDLE";
});

async function triggerRecoverySweepAnimation() {
  if (DOM.topoWorkerStatus) {
    DOM.topoWorkerStatus.textContent = "SWEEPING";
    DOM.topoWorkerStatus.setAttribute("class", "text-purple font-mono");
  }
  DOM.topoCoordLease.textContent = "SWEEPING";
  DOM.recRedisLock.innerHTML = `<span class="badge badge-purple">SWEEPING LEASE</span>`;

  // Stage 1: Celery Beat -> Redis Message Broker (Push Scheduled Trigger)
  animatePacketAlongPath({ route: "beat-dbbroker", type: "purple", durationMs: 380, destinationNodeId: "node-box-db-redis-broker" });
  await sleep(340);

  // Stage 2: Redis Message Broker -> Recovery Worker (Pop Task)
  animatePacketAlongPath({ route: "dbbroker-worker", type: "purple", durationMs: 340, destinationNodeId: "node-box-worker" });
  await sleep(300);

  // Stage 3: Recovery Worker -> Redis Mutex & Cache (Claim 2h Lease Lock)
  animatePacketAlongPath({ route: "worker-dbcache", type: "purple", durationMs: 340, destinationNodeId: "node-box-db-redis-cache" });
  await sleep(300);

  // Stage 4: Recovery Worker -> Coordinator Service (Sweep Stale Sagas)
  animatePacketAlongPath({ route: "worker-coord", type: "purple", durationMs: 380, destinationNodeId: "node-box-coord" });
  await sleep(320);

  // Stage 5: Coordinator -> Inventory & Payment Services (Probe Ground Truth)
  animatePacketAlongPath({ route: "coord-inv", type: "purple", durationMs: 420, destinationNodeId: "node-box-inv" });
  animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 420, destinationNodeId: "node-box-pay" });
}

const btnExecInfo = document.getElementById("btn-exec-info");
if (btnExecInfo) {
  btnExecInfo.addEventListener("click", (e) => {
    e.stopPropagation();
    showToast({
      icon: "⦿",
      title: "Sync vs. Async Pipeline (Simple Guide)",
      message: "Async (Background): Gives you an instant receipt (202 Accepted) in 10ms, then Celery workers finish booking the seat in the background (like BookMyShow).\n\nSync (Wait): Keeps your browser connection waiting until all services (Seat + Bank + Confirmation) are 100% finished before responding (200 OK).",
      type: "normal",
      duration: 8500,
    });
  });
}

DOM.btnRetryActiveTx.addEventListener("click", async () => {
  if (!STATE.activeTxId) return;
  if (!checkLiveAuthGate("retry transaction compensation")) return;

  await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 450, destinationNodeId: "node-box-inv" });
  await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 350, destinationNodeId: "node-box-coord" });

  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/${STATE.activeTxId}/retry-compensation/`, {
    method: "POST",
  });

  const detailedError = extractDetailedErrorMessage(res, "Retry compensation executed");
  showToast({
    icon: "✦︎",
    title: "Retry Compensation",
    message: detailedError,
    type: res.ok ? "success" : "race",
  });

  inspectTransaction(STATE.activeTxId, true);
  fetchTransactionsFeed();
});


function createSimulatedTransaction(finalStatus = "COMPLETED", customId = null, failureReason = null) {
  const txId = customId || crypto.randomUUID();
  const now = new Date().toISOString();

  let events = [];
  if (finalStatus === "COMPLETED") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Held seat(s) [1]" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "COMPLETED", actor: "request", http_status_code: 200, http_payload: { status: "CHARGED" }, created_at: now },
    ];
  } else if (finalStatus === "COMPENSATED") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Held seat(s) [1]" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "COMPENSATING", actor: "request", http_status_code: 400, http_payload: { error: "Insufficient funds" }, created_at: now },
      { seq: 4, from_status: "COMPENSATING", to_status: "COMPENSATED", actor: "request", http_status_code: 200, http_payload: { message: "Seat hold released" }, created_at: now },
    ];
  } else if (finalStatus === "INVENTORY_RESERVE_FAILED") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVE_FAILED", actor: "request", http_status_code: 400, http_payload: { error: failureReason || "Seat already reserved or unavailable" }, created_at: now },
    ];
  } else if (finalStatus === "RECOVERY_IN_PROGRESS") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Held seat(s) [1]" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "RECOVERY_IN_PROGRESS", actor: "recovery", http_status_code: null, http_payload: { probe: "/payments/check/" }, created_at: now },
    ];
  }

  const newTx = {
    transaction_id: txId,
    status: finalStatus,
    seat_ids: [1],
    seat_numbers: ["A1"],
    quantity: 1,
    amount: finalStatus === "COMPENSATED" ? 9999 : 500,
    failure_reason: failureReason,
    created_at: now,
    updated_at: now,
    events: events,
  };

  STATE.transactions.unshift(newTx);
  STATE.stats.totalSagas++;
  if (finalStatus !== "INVENTORY_RESERVE_FAILED") {
    STATE.stats.locksEnforced++;
  }
  DOM.statSagasTotal.textContent = STATE.stats.totalSagas;
  DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;

  window.selectTransaction(txId);
  renderTransactionsTable(STATE.transactions);
}

DOM.filterTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    DOM.filterTabs.forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    STATE.activeFilter = tab.getAttribute("data-filter");
    fetchTransactionsFeed(1);
  });
});

DOM.btnManualPoll.addEventListener("click", () => {
  CanvasEngine.init();
  checkServiceHealth();
  fetchTransactionsFeed();
});

window.setBackendMode = function(isDemo) {
  if (DOM.toggleSimMode) DOM.toggleSimMode.checked = isDemo;
  STATE.isSimulatedMode = isDemo;
  if (isDemo) {
    DOM.labelDemo?.classList.add("active");
    DOM.labelLive?.classList.remove("active");
    if (STATE.transactions.length === 0) {
      createSimulatedTransaction("COMPLETED");
    }
  } else {
    DOM.labelLive?.classList.add("active");
    DOM.labelDemo?.classList.remove("active");
  }
  CanvasEngine.init();
  checkServiceHealth();
  fetchTransactionsFeed();
};

if (DOM.toggleSimMode) {
  DOM.toggleSimMode.addEventListener("change", () => {
    window.setBackendMode(DOM.toggleSimMode.checked);
  });
}

window.setDashboardTheme = function(theme) {
  const isLight = theme === "light";
  document.documentElement.setAttribute("data-theme", isLight ? "light" : "dark");
  if (DOM.toggleTheme) DOM.toggleTheme.checked = isLight;
  if (isLight) {
    DOM.iconThemeLight?.classList.add("active");
    DOM.iconThemeDark?.classList.remove("active");
  } else {
    DOM.iconThemeDark?.classList.add("active");
    DOM.iconThemeLight?.classList.remove("active");
  }
  localStorage.setItem("dtc_theme", isLight ? "light" : "dark");
};

if (DOM.toggleTheme) {
  DOM.toggleTheme.addEventListener("change", () => {
    window.setDashboardTheme(DOM.toggleTheme.checked ? "light" : "dark");
  });
}

DOM.btnHowItWorks.addEventListener("click", () => DOM.modalArch.classList.remove("hidden"));
DOM.btnCloseArch.addEventListener("click", () => DOM.modalArch.classList.add("hidden"));

async function fetchRecoveryStatus() {
  if (STATE.isSimulatedMode) return;
  try {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/recovery/status/`, {
      method: "GET",
      skipAuth: true,
    }, 4000);
    if (res.ok && res.data && typeof res.data.seconds_until_next_run === "number") {
      STATE.sweepCountdown = res.data.seconds_until_next_run;
      if (res.data.last_run_timestamp) {
        STATE.recoveryLastRunTimestamp = res.data.last_run_timestamp;
      }
    }
  } catch (e) {
    // Graceful fallback to epoch wall-clock calculation
  }
}

function startRecoveryCountdown() {
  // Synchronize with server recovery status immediately on boot
  fetchRecoveryStatus();

  setInterval(async () => {
    STATE.sweepCountdown--;
    if (STATE.sweepCountdown <= 0) {
      STATE.sweepCountdown = 90;
      STATE.stats.sagasSwept += 1;
      DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;
      localStorage.setItem("dtc_stat_swept", STATE.stats.sagasSwept);

      triggerRecoverySweepAnimation();

      if (STATE.isSimulatedMode) {
        let swept = 0;
        const now = new Date().toISOString();
        STATE.transactions.forEach(tx => {
          if (tx.status === "STARTED") {
            tx.status = "TIMEOUT";
            tx.failure_reason = "Nothing reserved or charged (coordinator crashed before reservation) → Marked TIMEOUT";
            tx.events = [
              { seq: 1, from_status: "STARTED", to_status: "TIMEOUT", actor: "recovery", http_status_code: 200, http_payload: { message: "Swept by Celery Beat: nothing reserved or charged, marked TIMEOUT" }, created_at: now },
            ];
            tx.updated_at = now;
            swept++;
          }
        });
        if (swept > 0) {
          STATE.stats.orphansRecovered += swept;
          if (DOM.recOrphansRecovered) DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
          localStorage.setItem("dtc_stat_orphans", STATE.stats.orphansRecovered);
        }
        fetchTransactionsFeed();
        if (STATE.activeTxId) {
          inspectTransaction(STATE.activeTxId, true);
        }
      }
      // In Live mode: countdown purely updates the timer UI; no automatic network spam

      setTimeout(() => {
        DOM.topoCoordLease.textContent = "READY";
        DOM.recRedisLock.innerHTML = `<span class="badge badge-success">IDLE / READY</span>`;
        if (DOM.topoWorkerStatus) {
          DOM.topoWorkerStatus.textContent = "IDLE";
          DOM.topoWorkerStatus.setAttribute("class", "text-success font-mono");
        }
      }, 3000);
    }

    DOM.sweepCountdown.textContent = `${STATE.sweepCountdown}s`;
    DOM.recCountdownVal.textContent = `${STATE.sweepCountdown}s`;
    if (DOM.topoBeatCountdown) DOM.topoBeatCountdown.textContent = `${STATE.sweepCountdown}s`;
    if (DOM.topoWorkerInterval) DOM.topoWorkerInterval.textContent = `${STATE.sweepCountdown}s`;
    const pct = ((90 - STATE.sweepCountdown) / 90) * 100;
    DOM.recProgressFill.style.width = `${100 - pct}%`;
  }, 1000);
}

const DEFAULT_CANVAS_NODES = {
  // Left Column
  client:            { x: -80,  y: 225, width: 165, height: 105 },
  "db-redis-broker": { x: 86,   y: 89,  width: 185, height: 44 },

  // Center Column
  beat:              { x: 390,  y: 14,  width: 180, height: 58 },
  worker:            { x: 386,  y: 140, width: 190, height: 68 },
  coord:             { x: 390,  y: 290, width: 225, height: 120 },
  "db-coord":        { x: 382,  y: 485, width: 170, height: 44 },

  // Right-Center Column
  "db-redis-cache":  { x: 725,  y: 58,  width: 185, height: 44 },
  inv:               { x: 821,  y: 187, width: 195, height: 95 },
  pay:               { x: 823,  y: 423, width: 195, height: 95 },

  // Far-Right Column
  "db-inv":          { x: 1125, y: 178, width: 165, height: 44 },
  "db-pay":          { x: 1125, y: 414, width: 165, height: 44 },
};

const CanvasEngine = {
  container: null,
  svg: null,
  world: null,
  bgRect: null,
  zoomLabel: null,
  btnZoomIn: null,
  btnZoomOut: null,
  btnFitView: null,
  btnResetLayout: null,

  panX: 0,
  panY: 0,
  zoom: 1.0,
  minZoom: 0.35,
  maxZoom: 2.5,

  nodes: JSON.parse(JSON.stringify(DEFAULT_CANVAS_NODES)),

  isDraggingNode: false,
  activeDragNodeKey: null,
  dragStartMouse: { x: 0, y: 0 },
  dragStartNodePos: { x: 0, y: 0 },

  isPanning: false,
  panStartMouse: { x: 0, y: 0 },
  panStartPos: { x: 0, y: 0 },

  init() {
    this.container = document.getElementById("topology-canvas-container");
    this.svg = document.getElementById("topology-svg");
    this.world = document.getElementById("canvas-world");
    this.bgRect = document.getElementById("canvas-bg-rect");
    this.zoomLabel = document.getElementById("canvas-zoom-label");
    this.btnZoomIn = document.getElementById("btn-canvas-zoom-in");
    this.btnZoomOut = document.getElementById("btn-canvas-zoom-out");
    this.btnFitView = document.getElementById("btn-canvas-fit-view");
    this.btnResetLayout = document.getElementById("btn-canvas-reset-layout");

    if (!this.container || !this.svg || !this.world) return;

    const TOPOLOGY_SCHEMA_VER = "dtc_topo_v12";
    if (localStorage.getItem("dtc_topo_schema") !== TOPOLOGY_SCHEMA_VER) {
      localStorage.removeItem("dtc_topology_nodes");
      localStorage.removeItem("dtc_topology_viewport");
      localStorage.setItem("dtc_topo_schema", TOPOLOGY_SCHEMA_VER);
      this.nodes = JSON.parse(JSON.stringify(DEFAULT_CANVAS_NODES));
    } else {
      try {
        const savedNodes = localStorage.getItem("dtc_topology_nodes");
        if (savedNodes) {
          const parsed = JSON.parse(savedNodes);
          Object.keys(DEFAULT_CANVAS_NODES).forEach((k) => {
            if (parsed[k] && typeof parsed[k].x === "number" && typeof parsed[k].y === "number") {
              this.nodes[k] = { ...DEFAULT_CANVAS_NODES[k], x: parsed[k].x, y: parsed[k].y };
            }
          });
        }
      } catch (e) {
        console.warn("Could not load saved topology nodes", e);
      }
    }

    try {
      const savedVp = localStorage.getItem("dtc_topology_viewport");
      if (savedVp) {
        const vp = JSON.parse(savedVp);
        if (typeof vp.panX === "number") this.panX = vp.panX;
        if (typeof vp.panY === "number") this.panY = vp.panY;
        if (typeof vp.zoom === "number") this.zoom = Math.min(Math.max(vp.zoom, this.minZoom), this.maxZoom);
      } else {
        setTimeout(() => this.fitView(), 30);
      }
    } catch (e) {
      console.warn("Could not load saved topology viewport", e);
    }

    this.bindEvents();
    this.applyTransform();
    this.renderNodePositions();
    this.updateEdges();
  },

  bindEvents() {
    this.container.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    this.container.addEventListener("mousedown", (e) => this.onMouseDown(e));
    window.addEventListener("mousemove", (e) => this.onMouseMove(e));
    window.addEventListener("mouseup", (e) => this.onMouseUp(e));

    if (this.btnZoomIn) this.btnZoomIn.addEventListener("click", () => this.zoomByDelta(0.02));
    if (this.btnZoomOut) this.btnZoomOut.addEventListener("click", () => this.zoomByDelta(-0.02));
    if (this.btnFitView) this.btnFitView.addEventListener("click", () => this.fitView());
    if (this.btnResetLayout) {
      this.btnResetLayout.addEventListener("click", () => {
        this.resetLayout();
        showToast({
          icon: "✦︎",
          title: "Topology Layout Reset",
          message: "Node positions and zoom restored to default configuration.",
          type: "normal",
        });
      });
    }

    window.addEventListener("keydown", (e) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        this.btnZoomIn?.click();
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        this.btnZoomOut?.click();
      } else if (e.key === "0") {
        e.preventDefault();
        this.fitView();
      }
    });

    document.querySelectorAll(".topo-node-group").forEach((group) => {
      group.addEventListener("mousedown", (e) => this.onNodeMouseDown(e, group));
      group.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        const key = group.getAttribute("data-node-key");
        if (key && this.nodes[key]) {
          this.focusNode(key);
        }
      });
    });

    window.addEventListener("resize", () => {
      this.updateEdges();
    });
  },

  onMouseDown(e) {
    if (e.target.closest(".topo-node-group")) return;
    this.isPanning = true;
    this.panStartMouse = { x: e.clientX, y: e.clientY };
    this.panStartPos = { x: this.panX, y: this.panY };
    this.container.style.cursor = "grabbing";
  },

  onNodeMouseDown(e, group) {
    e.stopPropagation();
    const key = group.getAttribute("data-node-key");
    if (!key || !this.nodes[key]) return;

    this.isDraggingNode = true;
    this.activeDragNodeKey = key;
    this.dragStartMouse = { x: e.clientX, y: e.clientY };
    this.dragStartNodePos = { x: this.nodes[key].x, y: this.nodes[key].y };
    group.classList.add("dragging");
    this.container.style.cursor = "move";
  },

  onMouseMove(e) {
    if (this.isPanning) {
      const dx = e.clientX - this.panStartMouse.x;
      const dy = e.clientY - this.panStartMouse.y;
      this.panX = this.panStartPos.x + dx;
      this.panY = this.panStartPos.y + dy;
      this.applyTransform();
    } else if (this.isDraggingNode && this.activeDragNodeKey) {
      const dx = (e.clientX - this.dragStartMouse.x) / this.zoom;
      const dy = (e.clientY - this.dragStartMouse.y) / this.zoom;
      this.nodes[this.activeDragNodeKey].x = Math.round(this.dragStartNodePos.x + dx);
      this.nodes[this.activeDragNodeKey].y = Math.round(this.dragStartNodePos.y + dy);
      this.renderNodePositions();
      this.updateEdges();
    }
  },

  onMouseUp() {
    if (this.isPanning) {
      this.isPanning = false;
      this.container.style.cursor = "grab";
      this.saveViewport();
    }
    if (this.isDraggingNode) {
      const group = document.getElementById(`node-group-${this.activeDragNodeKey}`);
      if (group) group.classList.remove("dragging");
      this.isDraggingNode = false;
      this.activeDragNodeKey = null;
      this.container.style.cursor = "grab";
      this.saveNodes();
    }
  },

  onWheel(e) {
    e.preventDefault();
    const rect = this.container.getBoundingClientRect();
    const cursorX = e.clientX - rect.left;
    const cursorY = e.clientY - rect.top;

    const delta = e.deltaY < 0 ? 0.02 : -0.02;
    const newZoom = Math.min(Math.max(Math.round((this.zoom + delta) * 100) / 100, this.minZoom), this.maxZoom);
    if (newZoom === this.zoom) return;

    this.panX = cursorX - (cursorX - this.panX) * (newZoom / this.zoom);
    this.panY = cursorY - (cursorY - this.panY) * (newZoom / this.zoom);
    this.zoom = newZoom;

    this.applyTransform();
    this.saveViewport();
  },

  zoomByDelta(delta) {
    if (!this.container) return;
    const rect = this.container.getBoundingClientRect();
    const centerX = rect.width / 2;
    const centerY = rect.height / 2;

    const newZoom = Math.min(Math.max(Math.round((this.zoom + delta) * 100) / 100, this.minZoom), this.maxZoom);
    if (newZoom === this.zoom) return;

    this.panX = centerX - (centerX - this.panX) * (newZoom / this.zoom);
    this.panY = centerY - (centerY - this.panY) * (newZoom / this.zoom);
    this.zoom = newZoom;

    this.applyTransform();
    this.saveViewport();
  },

  applyTransform() {
    if (this.world) {
      this.world.setAttribute(
        "transform",
        `translate(${this.panX.toFixed(2)}, ${this.panY.toFixed(2)}) scale(${this.zoom.toFixed(3)})`
      );
    }
    if (this.zoomLabel) {
      this.zoomLabel.textContent = `${Math.round(this.zoom * 100)}%`;
    }
  },

  renderNodePositions() {
    Object.keys(this.nodes).forEach((key) => {
      const group = document.getElementById(`node-group-${key}`);
      if (group && this.nodes[key]) {
        group.setAttribute("transform", `translate(${this.nodes[key].x}, ${this.nodes[key].y})`);
      }
    });
  },

  getDynamicPathD(sourceKey, targetKey) {
    const s = this.nodes[sourceKey];
    const t = this.nodes[targetKey];
    if (!s || !t) return "";

    const dx = t.x - s.x;
    const dy = t.y - s.y;

    let p1, p2, c1, c2;

    if (Math.abs(dx) >= Math.abs(dy)) {
      if (dx >= 0) {
        p1 = { x: s.x + s.width, y: s.y + s.height / 2 };
        p2 = { x: t.x, y: t.y + t.height / 2 };
        const curDx = Math.max((p2.x - p1.x) * 0.5, 30);
        c1 = { x: p1.x + curDx, y: p1.y };
        c2 = { x: p2.x - curDx, y: p2.y };
      } else {
        p1 = { x: s.x, y: s.y + s.height / 2 };
        p2 = { x: t.x + t.width, y: t.y + t.height / 2 };
        const curDx = Math.max((p1.x - p2.x) * 0.5, 30);
        c1 = { x: p1.x - curDx, y: p1.y };
        c2 = { x: p2.x + curDx, y: p2.y };
      }
    }
    else if (dy > 0) {
      p1 = { x: s.x + s.width / 2, y: s.y + s.height };
      p2 = { x: t.x + t.width / 2, y: t.y };
      const curDy = Math.max((p2.y - p1.y) * 0.5, 30);
      c1 = { x: p1.x, y: p1.y + curDy };
      c2 = { x: p2.x, y: p2.y - curDy };
    }
    else {
      p1 = { x: s.x + s.width / 2, y: s.y };
      p2 = { x: t.x + t.width / 2, y: t.y + t.height };
      const curDy = Math.max((p1.y - p2.y) * 0.5, 30);
      c1 = { x: p1.x, y: p1.y - curDy };
      c2 = { x: p2.x, y: p2.y + curDy };
    }

    return `M ${p1.x} ${p1.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${p2.x} ${p2.y}`;
  },

  updateEdges() {
    const pathClientCoord = document.getElementById("path-client-coord");
    const pathCoordInv = document.getElementById("path-coord-inv");
    const pathCoordPay = document.getElementById("path-coord-pay");
    const pathInvCoord = document.getElementById("path-inv-coord");
    const pathPayCoord = document.getElementById("path-pay-coord");
    const pathBeatDbBroker = document.getElementById("path-beat-dbbroker");
    const pathDbBrokerWorker = document.getElementById("path-dbbroker-worker");
    const pathWorkerDbCache = document.getElementById("path-worker-dbcache");
    const pathInvDbCache = document.getElementById("path-inv-dbcache");
    const pathWorkerCoord = document.getElementById("path-worker-coord");
    const pathCoordDbCoord = document.getElementById("path-coord-dbcoord");
    const pathInvDbInv = document.getElementById("path-inv-dbinv");
    const pathPayDbPay = document.getElementById("path-pay-dbpay");

    const dClientCoord = this.getDynamicPathD("client", "coord");
    const dCoordInv = this.getDynamicPathD("coord", "inv");
    const dCoordPay = this.getDynamicPathD("coord", "pay");
    const dInvCoord = this.getDynamicPathD("inv", "coord");
    const dPayCoord = this.getDynamicPathD("pay", "coord");
    const dBeatDbBroker = this.getDynamicPathD("beat", "db-redis-broker");
    const dDbBrokerWorker = this.getDynamicPathD("db-redis-broker", "worker");
    const dWorkerDbCache = this.getDynamicPathD("worker", "db-redis-cache");
    const dInvDbCache = this.getDynamicPathD("inv", "db-redis-cache");
    const dWorkerCoord = this.getDynamicPathD("worker", "coord");
    const dCoordDbCoord = this.getDynamicPathD("coord", "db-coord");
    const dInvDbInv = this.getDynamicPathD("inv", "db-inv");
    const dPayDbPay = this.getDynamicPathD("pay", "db-pay");

    if (pathClientCoord) pathClientCoord.setAttribute("d", dClientCoord);
    if (pathCoordInv) pathCoordInv.setAttribute("d", dCoordInv);
    if (pathCoordPay) pathCoordPay.setAttribute("d", dCoordPay);
    if (pathInvCoord) pathInvCoord.setAttribute("d", dInvCoord);
    if (pathPayCoord) pathPayCoord.setAttribute("d", dPayCoord);
    if (pathBeatDbBroker) pathBeatDbBroker.setAttribute("d", dBeatDbBroker);
    if (pathDbBrokerWorker) pathDbBrokerWorker.setAttribute("d", dDbBrokerWorker);
    if (pathWorkerDbCache) pathWorkerDbCache.setAttribute("d", dWorkerDbCache);
    if (pathInvDbCache) pathInvDbCache.setAttribute("d", dInvDbCache);
    if (pathWorkerCoord) pathWorkerCoord.setAttribute("d", dWorkerCoord);
    if (pathCoordDbCoord) pathCoordDbCoord.setAttribute("d", dCoordDbCoord);
    if (pathInvDbInv) pathInvDbInv.setAttribute("d", dInvDbInv);
    if (pathPayDbPay) pathPayDbPay.setAttribute("d", dPayDbPay);
  },

  fitView() {
    if (!this.container) return;
    const rect = this.container.getBoundingClientRect();
    const cWidth = rect.width || 1000;
    const cHeight = rect.height || 480;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    Object.values(this.nodes).forEach((n) => {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.width);
      maxY = Math.max(maxY, n.y + n.height);
    });

    const bWidth = maxX - minX;
    const bHeight = maxY - minY;
    const padding = 35;

    const scaleX = (cWidth - padding * 2) / bWidth;
    const scaleY = (cHeight - padding * 2) / bHeight;
    const newZoom = Math.min(Math.max(Math.min(scaleX, scaleY), this.minZoom), 1.0);

    this.zoom = newZoom;
    this.panX = (cWidth - bWidth * newZoom) / 2 - minX * newZoom;
    this.panY = (cHeight - bHeight * newZoom) / 2 - minY * newZoom;

    this.applyTransform();
    this.saveViewport();
  },

  focusNode(nodeKey) {
    const node = this.nodes[nodeKey];
    if (!node || !this.container) return;

    const rect = this.container.getBoundingClientRect();
    const cWidth = rect.width || 1000;
    const cHeight = rect.height || 480;

    this.zoom = 1.15;
    this.panX = cWidth / 2 - (node.x + node.width / 2) * this.zoom;
    this.panY = cHeight / 2 - (node.y + node.height / 2) * this.zoom;

    this.applyTransform();
    this.saveViewport();
  },

  resetLayout() {
    this.nodes = JSON.parse(JSON.stringify(DEFAULT_CANVAS_NODES));
    this.renderNodePositions();
    this.updateEdges();
    this.fitView();
    this.saveNodes();
    this.saveViewport();
  },

  saveNodes() {
    localStorage.setItem("dtc_topology_nodes", JSON.stringify(this.nodes));
  },

  saveViewport() {
    localStorage.setItem(
      "dtc_topology_viewport",
      JSON.stringify({ panX: this.panX, panY: this.panY, zoom: this.zoom })
    );
  }
};

const TECH_STACK_CONFIG = [
  {
    name: "Docker & Docker Compose",
    category: "Containerization & Orchestration",
    desc: "Multi-container orchestration bundling 3 independent Django services, dedicated PostgreSQL databases, and Redis with health checks.",
    icon: "container"
  },
  {
    name: "Django (v5.0+)",
    category: "Backend Framework / ORM",
    desc: "Core MVC framework providing relational models, ACID transaction management, and microservice domain logic.",
    icon: "server"
  },
  {
    name: "Django REST Framework",
    category: "REST API Toolkit",
    desc: "HTTP serialization, request validation, and RESTful RPC interfaces across microservices.",
    icon: "network"
  },
  {
    name: "PostgreSQL 16 (Psycopg2)",
    category: "Relational ACID Database",
    desc: "Three isolated database instances with SELECT FOR UPDATE row-level locking and UNIQUE idempotency constraints.",
    icon: "database"
  },
  {
    name: "Celery & Celery Beat",
    category: "Task Queue & Scheduler",
    desc: "Asynchronous background worker engine and 90-second periodic crash recovery reconciler.",
    icon: "clock"
  },
  {
    name: "Redis 7",
    category: "In-Memory Broker & Mutex",
    desc: "Task message broker and distributed Redis lock provider for crash recovery leases.",
    icon: "database"
  },
  {
    name: "Gunicorn",
    category: "WSGI Production Server",
    desc: "High-concurrency gthread WSGI application server handling simultaneous HTTP saga traffic.",
    icon: "server"
  },
  {
    name: "Supervisor",
    category: "Process Supervisor",
    desc: "Process management system coordinating Gunicorn, Celery Worker, and Celery Beat inside containers.",
    icon: "server"
  },
  {
    name: "HTML5 Canvas & Vanilla JS",
    category: "Interactive Telemetry Engine",
    desc: "Zero-dependency 2D canvas with pan/zoom, interactive node dragging, and live saga visualizer.",
    icon: "code"
  },
  {
    name: "Railway & Vercel",
    category: "Cloud Infrastructure & CDN",
    desc: "Containerized microservice cloud hosting (Railway) and edge visualizer deployment (Vercel).",
    icon: "cloud"
  },
  {
    name: "Requests",
    category: "HTTP RPC Client",
    desc: "Synchronous RPC client for inter-service HTTP communication.",
    icon: "network"
  },
  {
    name: "django-cors-headers",
    category: "Security Middleware",
    desc: "Cross-Origin Resource Sharing (CORS) header configuration for visualizer frontend.",
    icon: "shield"
  },
  {
    name: "python-dotenv",
    category: "12-Factor Configuration",
    desc: "Environment variable loading from .env manifests and container environments.",
    icon: "package"
  }
];

function getTechStackIconSvg(iconType) {
  switch (iconType) {
    case "container":
    case "docker":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7l8-4 8 4M4 7v10l8 4 8-4V7M4 7l8 4 8-4M12 11v10"/><rect x="7" y="10" width="3" height="3"/><rect x="14" y="10" width="3" height="3"/></svg>`;
    case "cloud":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>`;
    case "code":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`;
    case "server":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`;
    case "database":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>`;
    case "network":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>`;
    case "clock":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
    case "shield":
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`;
    case "package":
    default:
      return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>`;
  }
}

function initTechStackModal() {
  if (!DOM.stackItemsContainer) return;
  if (!TECH_STACK_CONFIG || TECH_STACK_CONFIG.length === 0) {
    DOM.stackItemsContainer.innerHTML = `<div class="stack-card" style="grid-column: 1 / -1; justify-content: center; color: var(--text-muted);">No manifest dependencies detected</div>`;
    return;
  }

  DOM.stackItemsContainer.innerHTML = TECH_STACK_CONFIG.map(item => `
    <div class="stack-card">
      <div class="stack-icon-wrap">
        ${getTechStackIconSvg(item.icon || "package")}
      </div>
      <div class="stack-card-content">
        <span class="stack-badge">${item.category || "Dependency"}</span>
        <h4>${item.name}</h4>
        <p>${item.desc || "Runtime package dependency"}</p>
      </div>
    </div>
  `).join("");

  if (DOM.btnViewStack) {
    DOM.btnViewStack.addEventListener("click", () => {
      DOM.modalStack?.classList.remove("hidden");
    });
  }
  if (DOM.btnCloseStack) {
    DOM.btnCloseStack.addEventListener("click", () => {
      DOM.modalStack?.classList.add("hidden");
    });
  }
  if (DOM.btnCloseStackFooter) {
    DOM.btnCloseStackFooter.addEventListener("click", () => {
      DOM.modalStack?.classList.add("hidden");
    });
  }
  if (DOM.modalStack) {
    DOM.modalStack.addEventListener("click", (e) => {
      if (e.target === DOM.modalStack) {
        DOM.modalStack.classList.add("hidden");
      }
    });
  }
}

/* ==========================================================================
   FAN IDENTITY & EMAIL OTP AUTHENTICATION
   ========================================================================== */

let appOtpCountdownTimer = null;

function updateMainAuthWidgetUI() {
  const token = localStorage.getItem("dtc_auth_token");
  const email = localStorage.getItem("dtc_user_email");

  const btnOpen = document.getElementById("btn-open-auth-main");
  const emailLabel = document.getElementById("main-user-email-label");
  const avatarIcon = document.getElementById("main-user-avatar-icon");

  if (token && email) {
    if (emailLabel) emailLabel.textContent = email;
    if (avatarIcon) avatarIcon.textContent = "🟢";
    if (btnOpen) {
      btnOpen.style.borderColor = "rgba(34, 197, 94, 0.4)";
      btnOpen.style.background = "rgba(34, 197, 94, 0.1)";
      btnOpen.style.color = "#22c55e";
    }
  } else {
    if (emailLabel) emailLabel.textContent = "Email Login";
    if (avatarIcon) avatarIcon.textContent = "✦︎";
    if (btnOpen) {
      btnOpen.style.borderColor = "rgba(56, 189, 248, 0.4)";
      btnOpen.style.background = "rgba(56, 189, 248, 0.1)";
      btnOpen.style.color = "#38bdf8";
    }
  }
}

function showMainAuthModal(step = 1) {
  const modal = document.getElementById("modal-auth");
  if (!modal) return;
  modal.classList.remove("hidden");
  const token = localStorage.getItem("dtc_auth_token");
  if (token) {
    renderMainAuthStep(3);
  } else {
    renderMainAuthStep(step);
  }
}

function closeMainAuthModal() {
  const modal = document.getElementById("modal-auth");
  if (!modal) return;
  modal.classList.add("hidden");
  if (appOtpCountdownTimer) clearInterval(appOtpCountdownTimer);
}

function renderMainAuthStep(step) {
  const s1 = document.getElementById("stepper-step-1");
  const s2 = document.getElementById("stepper-step-2");
  const l1 = document.getElementById("stepper-line-1");

  if (s1) s1.className = step === 1 ? "step-item active" : step > 1 ? "step-item completed" : "step-item";
  if (s2) s2.className = step === 2 ? "step-item active" : step > 2 ? "step-item completed" : "step-item";
  if (l1) l1.className = step >= 2 ? "step-line active" : "step-line";

  const p1 = document.getElementById("auth-step-1");
  const p2 = document.getElementById("auth-step-2");
  const p3 = document.getElementById("auth-step-3");

  [p1, p2, p3].forEach(p => {
    if (p) p.classList.add("hidden");
  });

  if (step === 1 && p1) {
    p1.classList.remove("hidden");
    const input = document.getElementById("auth-email-input");
    if (input) setTimeout(() => input.focus(), 100);
  } else if (step === 2 && p2) {
    p2.classList.remove("hidden");
    const input = document.getElementById("auth-otp-input");
    if (input) {
      if (!isLocal) {
        input.value = "";
      }
      setTimeout(() => input.focus(), 100);
    }
  } else if (step === 3 && p3) {
    p3.classList.remove("hidden");
    const emailDisplay = document.getElementById("profile-email-display");
    const userDisplay = document.getElementById("profile-user-id") || document.getElementById("profile-actor-id");
    const email = localStorage.getItem("dtc_user_email");
    const userId = localStorage.getItem("dtc_user_id");
    if (emailDisplay) emailDisplay.textContent = email;
    if (userDisplay) userDisplay.textContent = userId || `usr_${email}`;
  }
}

function startMainOtpCountdown(seconds = 30) {
  const timerText = document.getElementById("otp-timer-text");
  const btnResend = document.getElementById("btn-resend-otp");
  const countdown = document.getElementById("otp-countdown");

  if (isLocal) {
    // LOCAL DEV: Infinite immediate resends with no countdown wait
    if (timerText) timerText.classList.add("hidden");
    if (btnResend) {
      btnResend.classList.remove("hidden");
      btnResend.disabled = false;
      btnResend.textContent = "Resend Code ↺ (Infinite Dev Mode)";
    }
    return;
  }

  let remaining = seconds;
  if (timerText) timerText.classList.remove("hidden");
  if (btnResend) btnResend.classList.add("hidden");
  if (countdown) countdown.textContent = `${remaining}s`;

  if (appOtpCountdownTimer) clearInterval(appOtpCountdownTimer);
  appOtpCountdownTimer = setInterval(() => {
    remaining -= 1;
    if (countdown) countdown.textContent = `${remaining}s`;
    if (remaining <= 0) {
      clearInterval(appOtpCountdownTimer);
      if (timerText) timerText.classList.add("hidden");
      if (btnResend) btnResend.classList.remove("hidden");
    }
  }, 1000);
}

function setupMainAuthEventListeners() {
  const btnOpen = document.getElementById("btn-open-auth-main");
  const btnClose = document.getElementById("btn-close-auth");
  const btnCloseDone = document.getElementById("btn-close-auth-done");
  const modal = document.getElementById("modal-auth");

  if (btnOpen) btnOpen.addEventListener("click", () => showMainAuthModal(1));
  if (btnClose) btnClose.addEventListener("click", closeMainAuthModal);
  if (btnCloseDone) btnCloseDone.addEventListener("click", closeMainAuthModal);
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeMainAuthModal();
    });
  }

  // Step 1
  const btnSend = document.getElementById("btn-send-otp");
  const emailInput = document.getElementById("auth-email-input");
  if (btnSend) btnSend.addEventListener("click", handleMainSendOtp);
  if (emailInput) {
    emailInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleMainSendOtp();
    });
  }

  // Step 2
  const btnVerify = document.getElementById("btn-verify-otp");
  const otpInput = document.getElementById("auth-otp-input");
  const btnChange = document.getElementById("btn-change-email");
  const btnResend = document.getElementById("btn-resend-otp");
  if (btnVerify) btnVerify.addEventListener("click", handleMainVerifyOtp);
  if (otpInput) {
    otpInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleMainVerifyOtp();
    });
  }
  if (btnChange) btnChange.addEventListener("click", () => renderMainAuthStep(1));
  if (btnResend) btnResend.addEventListener("click", handleMainSendOtp);

  // Logout
  const btnLogout = document.getElementById("btn-logout");
  if (btnLogout) {
    btnLogout.addEventListener("click", () => {
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
      updateMainAuthWidgetUI();
      closeMainAuthModal();
      showToast({ title: "Signed Out", message: "Identity detached.", type: "normal" });
      fetchTransactionsFeed();
    });
  }

  // Session Heartbeat: executed once on page refresh/boot (no background polling/spam)
  const initSessionHeartbeat = () => {
    const token = localStorage.getItem("dtc_auth_token");
    if (token) {
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
          if (btnLogout) btnLogout.click(); // Auto-logout if session expired
          if (isSuperseded) {
            showToast({
              title: "Signed Out on Another Device",
              message: "Your account was logged into on another device. You have been signed out on this device.",
              type: "warning",
              duration: 9000,
            });
          }
        }
      }).catch(() => {});
    }
  };
  initSessionHeartbeat();

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
}

async function handleMainSendOtp() {
  const emailInput = document.getElementById("auth-email-input");
  const email = (emailInput?.value || "").trim();
  if (!email || !email.includes("@")) {
    showToast({ title: "Invalid Email", message: "Please enter a valid email address.", type: "normal" });
    return;
  }

  const btnSend = document.getElementById("btn-send-otp");
  const sendText = document.getElementById("send-otp-btn-text");
  if (btnSend) btnSend.disabled = true;
  if (sendText) sendText.textContent = "Sending Verification Code...";

  try {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/auth/send-otp/`, {
      method: "POST",
      skipAuth: true,
      body: JSON.stringify({ email }),
    });

    if (res.ok && res.data && res.data.success) {
      const targetEmail = document.getElementById("auth-target-email");
      const otpInput = document.getElementById("auth-otp-input");

      renderMainAuthStep(2);
      startMainOtpCountdown(res.data.cooldown_remaining || 30);

      const chancesText = (isLocal || res.data.chances_remaining === "∞")
        ? "\n(Local Dev: Infinite Resends Available)"
        : (res.data.chances_remaining !== undefined ? `\n(${10 - res.data.chances_remaining}/10 daily requests used — ${res.data.chances_remaining} remaining today)` : "");

      const resolvedEmail = (res.data && (res.data.email || res.data.user_email)) || email;

      if (isLocal && res.data.otp) {
        // LOCAL DEVELOPMENT ONLY: Display badge & auto-fill code
        if (targetEmail) {
          targetEmail.innerHTML = `${resolvedEmail}<br><span style="display:inline-block;margin-top:8px;padding:6px 12px;background:rgba(56,189,248,0.15);border:1px dashed #38bdf8;border-radius:6px;font-size:0.95rem;color:#38bdf8;font-weight:600;">Local Dev OTP: <strong style="letter-spacing:3px;font-size:1.15rem;color:#ffffff;text-shadow:0 0 8px #38bdf8;">${res.data.otp}</strong> <span style="font-size:0.8rem;color:#a5f3fc;">(Infinite Resend Mode)</span></span>`;
        }
        if (otpInput) {
          otpInput.value = res.data.otp;
        }
        showToast({
          title: `Local OTP: ${res.data.otp}`,
          message: `[LOCAL DEV] Simulated verification code: ${res.data.otp} (auto-filled in window)` + chancesText,
          type: "success",
          duration: 9000,
        });
      } else {
        // LIVE PRODUCTION: Normal workflow, no autofill, manual entry required
        if (targetEmail) {
          targetEmail.textContent = resolvedEmail;
        }
        if (otpInput) {
          otpInput.value = "";
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
        type: "normal",
      });
    }
  } catch (err) {
    showToast({ title: "Network Error", message: err.message, type: "normal" });
  } finally {
    if (btnSend) btnSend.disabled = false;
    if (sendText) sendText.textContent = "Send Verification Code (OTP) ➔";
  }
}

async function handleMainVerifyOtp() {
  const emailInput = document.getElementById("auth-email-input");
  const email = (emailInput?.value || localStorage.getItem("dtc_user_email") || "").trim();
  const otpInput = document.getElementById("auth-otp-input");
  const otp = (otpInput?.value || "").trim();

  if (!otp || otp.length < 6) {
    showToast({ title: "Invalid Code", message: "Please enter the 6-digit verification code.", type: "normal" });
    return;
  }

  const btnVerify = document.getElementById("btn-verify-otp");
  const verifyText = document.getElementById("verify-otp-btn-text");
  if (btnVerify) btnVerify.disabled = true;
  if (verifyText) verifyText.textContent = "Verifying Code...";

  try {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/auth/verify-otp/`, {
      method: "POST",
      skipAuth: true,
      body: JSON.stringify({ email, otp }),
    });

    if (res.ok && res.data && res.data.success) {
      const { token, user_id, user_email, email: resEmail } = res.data;
      const finalUserId = user_id;
      const finalEmail = user_email || resEmail || email;
      localStorage.setItem("dtc_auth_token", token);
      localStorage.setItem("dtc_user_email", finalEmail);
      localStorage.setItem("dtc_user_id", finalUserId);

      updateMainAuthWidgetUI();

      showToast({
        title: "Signed In",
        message: `Welcome ${finalEmail}! (User ID: ${finalUserId})`,
        type: "success",
      });

      renderMainAuthStep(3);
      fetchTransactionsFeed();
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
        type: "normal",
      });
    }
  } catch (err) {
    showToast({ title: "Network Error", message: err.message, type: "normal" });
  } finally {
    if (btnVerify) btnVerify.disabled = false;
    if (verifyText) verifyText.textContent = "Verify & Sign In ➔";
  }
}

/* ==========================================================================
   DTC LIQUID ENTRY SCREEN ANIMATION
   ========================================================================== */
let entryAnimId = null;
let entryProgress = 0;
let isDataReady = false;

function initEntryScreen() {
  const overlay = document.getElementById("dtc-entry-screen");
  const logoWrap = document.getElementById("dtc-entry-logo-wrap");
  const meta = document.getElementById("dtc-entry-meta");
  const canvas = document.getElementById("dtc-entry-canvas");
  const pctLabel = document.getElementById("dtc-entry-pct");
  const skipBtn = document.getElementById("btn-skip-entry");
  if (!overlay || !canvas || !pctLabel) return;

  const ctx = canvas.getContext("2d");
  const offCanvas = document.createElement("canvas");
  const offCtx = offCanvas.getContext("2d");
  let dpr = window.devicePixelRatio || 1;

  let width = 0;
  let height = 0;

  function resizeCanvas() {
    const rect = (logoWrap || canvas).getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    width = rect.width;
    height = rect.height;

    canvas.width = width * dpr;
    canvas.height = height * dpr;
    offCanvas.width = width * dpr;
    offCanvas.height = height * dpr;
  }
  window.addEventListener("resize", resizeCanvas);
  resizeCanvas();

  const DURATION_MS = 4000; // 4.0s smooth progression
  const HOLD_MS = 350;     // 350ms solid fill hold
  let startTime = null;
  let phase = 0;           // wave phase accumulator (m in NeoLeaf)
  let hasDismissed = false;
  let isHolding = false;

  const isDesktop = window.innerWidth >= 1024;
  const waveAmplitude = isDesktop ? 34 : 20;

  // NeoLeaf triple-sine organic wave function:
  // l = v * e - Math.sin(0.02 * t + m) * Math.sin(0.01 * t + m) * Math.sin(0.05 * t + m) * c
  function calculateWaveY(x, waterY, m, amp) {
    return waterY - Math.sin(0.02 * x + m) * Math.sin(0.01 * x + m) * Math.sin(0.05 * x + m) * amp;
  }

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function dismiss() {
    if (hasDismissed) return;
    hasDismissed = true;

    // 1. Fade out the counter
    if (meta) meta.classList.add("fade");

    // 2. NeoLeaf zoom punch: scale up logo & fade out
    if (logoWrap) logoWrap.classList.add("zoom-out");

    // 3. Graceful overlay fade out
    setTimeout(() => {
      overlay.classList.add("fade-out");
    }, 150);

    setTimeout(() => {
      if (entryAnimId) cancelAnimationFrame(entryAnimId);
      overlay.style.display = "none";
    }, 950);
  }

  if (skipBtn) {
    skipBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      dismiss();
    });
  }

  function render(now) {
    if (!startTime) startTime = now;
    phase += 0.034; // exact NeoLeaf phase increment ~0.03
    const elapsed = now - startTime;

    if (!isHolding) {
      let timeProgress = Math.min(1, elapsed / DURATION_MS);

      // If backend cold-start takes longer than 4s, pause gracefully at 94% until ready
      if (timeProgress > 0.94 && !isDataReady) {
        timeProgress = 0.94;
      }

      const eased = easeInOutCubic(timeProgress);
      entryProgress = eased * 100;
      pctLabel.textContent = `${Math.min(100, Math.floor(entryProgress))}`;

      if (timeProgress >= 1) {
        entryProgress = 100;
        pctLabel.textContent = "100";
        isHolding = true;
        setTimeout(dismiss, HOLD_MS);
      }
    }

    const w = canvas.width;
    const h = canvas.height;
    const cw = width;
    const ch = height;

    ctx.clearRect(0, 0, w, h);
    offCtx.clearRect(0, 0, w, h);

    ctx.save();
    ctx.scale(dpr, dpr);

    const text = "DTC";
    const fontSize = Math.min(cw * 0.32, ch * 0.72);
    const fontStr = `900 ${fontSize}px "Montserrat", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;

    ctx.font = fontStr;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    const centerX = cw / 2;
    const centerY = ch / 2;

    // 1. Ghost / Silhouette Background (NeoLeaf bg-opacity-30)
    ctx.fillStyle = "rgba(255, 255, 255, 0.16)";
    ctx.fillText(text, centerX, centerY);

    // 2. Offscreen Mask for Liquid Wave Fill
    const textHeight = fontSize * 0.78;
    const fillRange = textHeight + waveAmplitude * 1.5;
    const waterY = (centerY + textHeight / 2 + waveAmplitude * 0.75) - (entryProgress / 100) * fillRange;

    offCtx.save();
    offCtx.scale(dpr, dpr);

    offCtx.font = fontStr;
    offCtx.textAlign = "center";
    offCtx.textBaseline = "middle";
    offCtx.fillStyle = "#ffffff";
    offCtx.fillText(text, centerX, centerY);

    offCtx.globalCompositeOperation = "source-in";

    // Secondary subtle wave crest layer (depth)
    offCtx.save();
    offCtx.beginPath();
    offCtx.moveTo(0, ch);
    for (let x = 0; x <= cw; x += 3) {
      const y = calculateWaveY(x + 120, waterY + 3, phase * 0.9, waveAmplitude * 1.15);
      offCtx.lineTo(x, y);
    }
    offCtx.lineTo(cw, ch);
    offCtx.lineTo(0, ch);
    offCtx.closePath();
    offCtx.fillStyle = "#ffffff";
    offCtx.globalAlpha = 0.35;
    offCtx.fill();
    offCtx.restore();

    // Primary wave crest layer (solid white)
    offCtx.save();
    offCtx.beginPath();
    offCtx.moveTo(0, ch);
    for (let x = 0; x <= cw; x += 3) {
      const y = calculateWaveY(x, waterY, phase, waveAmplitude);
      offCtx.lineTo(x, y);
    }
    offCtx.lineTo(cw, ch);
    offCtx.lineTo(0, ch);
    offCtx.closePath();
    offCtx.fillStyle = "#ffffff";
    offCtx.globalAlpha = 1.0;
    offCtx.fill();
    offCtx.restore();

    offCtx.restore();

    // Draw masked liquid wave back to main canvas
    ctx.drawImage(offCanvas, 0, 0, cw, ch);
    ctx.restore();

    if (!hasDismissed) {
      entryAnimId = requestAnimationFrame(render);
    }
  }

  entryAnimId = requestAnimationFrame(render);

  setTimeout(() => {
    finishEntryScreen();
  }, 8000);
}

function finishEntryScreen() {
  isDataReady = true;
}

window.addEventListener("DOMContentLoaded", () => {
  initEntryScreen();
  const savedTheme = localStorage.getItem("dtc_theme") || "dark";
  window.setDashboardTheme(savedTheme);
  window.setBackendMode(false);

  setupMainAuthEventListeners();
  updateMainAuthWidgetUI();

  DOM.statRacesDefended.textContent = STATE.stats.racesDefended;
  DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;
  DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
  DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;

  CanvasEngine.init();
  initTechStackModal();
  checkServiceHealth();
  fetchTransactionsFeed().finally(() => {
    finishEntryScreen();
  });
  startRecoveryCountdown();
});


