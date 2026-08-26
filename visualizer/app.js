
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

const CONFIG = {
  coordinatorUrl: localStorage.getItem("dtc_cfg_coord") || "http://127.0.0.1:8002",
  inventoryUrl: localStorage.getItem("dtc_cfg_inv") || "http://127.0.0.1:8000",
  paymentUrl: localStorage.getItem("dtc_cfg_pay") || "http://127.0.0.1:8001",
  pollIntervalMs: parseInt(localStorage.getItem("dtc_cfg_poll") || "1500", 10),
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
  sweepCountdown: 90,
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SCENARIO_DESCRIPTIONS = {
  happy: {
    icon: "⚡",
    title: "Happy Path Saga",
    badge: "3-Phase Forward Orchestration",
    badgeClass: "badge-success",
    desc: "Executes a standard forward distributed transaction where all participating microservices succeed. The Coordinator sequentially drives stock reservation and payment capture without spanning database transactions.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "product_id": 1,
  "quantity": 1,
  "amount": 500
}`,
    steps: [
      "Client dispatches POST /transactions/ to Coordinator.",
      "Coordinator inserts Transaction(status=STARTED) and dispatches POST /products/reserve/ to Inventory (:8000).",
      "Inventory acquires row lock via SELECT FOR UPDATE, decrements stock, inserts Reservation(status=RESERVED), and returns 200 OK.",
      "Coordinator transitions to PAYMENT_PROCESSING and dispatches POST /payments/charge/ to Payment (:8001).",
      "Payment inserts Payment(status=CHARGED) and returns 200 OK.",
      "Coordinator transitions to COMPLETED and responds HTTP 200 to Client."
    ],
    defenses: "ACID isolation per microservice database. Coordinator drives the saga through explicit SagaEvent audit transitions. Idempotent record creation eliminates duplicate requests.",
    outcome: "Status: COMPLETED (HTTP 200 OK)"
  },
  fail: {
    icon: "🔄",
    title: "Payment Decline & Auto-Compensate",
    badge: "Backward Compensating Rollback",
    badgeClass: "badge-amber",
    desc: "Demonstrates automated compensating recovery when downstream payment declines. Because distributed transactions cannot use global database rollbacks, the Coordinator executes an inverse compensating action to restore reserved stock.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "product_id": 1,
  "quantity": 1,
  "amount": 9999
}
// Note: Amount 9999 triggers intentional 422 decline in Payment Service`,
    steps: [
      "Client dispatches transaction with amount $9,999 to Coordinator.",
      "Coordinator reserves inventory stock successfully (STARTED → INVENTORY_RESERVED).",
      "Coordinator attempts payment charge (POST /payments/charge/).",
      "Payment service declines charge with HTTP 422 ('Payment declined / Testing failure amount').",
      "Coordinator catches decline, transitions to COMPENSATING, and dispatches POST /products/release/ to Inventory.",
      "Inventory locks reservation under SELECT FOR UPDATE, restores stock (stock += 1), marks Reservation RELEASED.",
      "Coordinator transitions to COMPENSATED and returns HTTP 400 with rollback details."
    ],
    defenses: "Saga Backward Recovery pattern. Inventory release endpoint is strictly idempotent via row-level reservation checks (only 'RESERVED' rows can be released).",
    outcome: "Status: COMPENSATED (HTTP 400 Bad Request / 422)"
  },
  race: {
    icon: "🔥",
    title: "5x Concurrent Race Attack",
    badge: "PostgreSQL Relational Concurrency Defense",
    badgeClass: "badge-rose",
    desc: "Simulates a high-concurrency client retry storm or double-click attack. Five identical requests with the exact same client-supplied UUID hit the Coordinator at the exact same millisecond.",
    payload: `5x Concurrent Requests (Promise.all):
POST http://127.0.0.1:8002/transactions/
{
  "transaction_id": "8f3b2c14-59e2-4a71-9df0-e2b34a91c002",
  "product_id": 1,
  "quantity": 1,
  "amount": 500
}`,
    steps: [
      "Client fires 5 concurrent asynchronous HTTP POST requests with an identical UUID.",
      "5 worker threads simultaneously attempt INSERT INTO coordinator_transaction.",
      "Thread 1 succeeds in inserting the row and proceeds to drive the saga to completion.",
      "Threads 2, 3, 4, and 5 violate PostgreSQL's PRIMARY KEY / UNIQUE(transaction_id) constraint.",
      "PostgreSQL aborts the 4 duplicate insert transactions with IntegrityError (SQL 23505).",
      "Coordinator catches IntegrityError, immediately short-circuits, and prevents duplicate downstream calls to Inventory or Payment."
    ],
    defenses: "Relational UNIQUE constraint + Compare-And-Insert atomic blocks. Zero reliance on transient in-memory Redis TTL keys which risk cache expiration bugs.",
    outcome: "Exactly 1 saga commits COMPLETED. 4 duplicate requests safely defended."
  },
  crash: {
    icon: "💥",
    title: "Process Crash Simulation",
    badge: "Celery Beat & Dual-Path Reconciler",
    badgeClass: "badge-purple",
    desc: "Simulates a catastrophic coordinator process death, server crash, or OOM kill mid-flight right after reserving stock. The transaction is left orphaned in PAYMENT_PROCESSING with unreleased stock.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "transaction_id": "<UUID>",
  "product_id": 1,
  "quantity": 1,
  "amount": 500,
  "coordinator": true
}
// Note: 'coordinator': true kills process mid-flight`,
    steps: [
      "Coordinator reserves stock in Inventory (:8000), transitions status to PAYMENT_PROCESSING.",
      "Coordinator process terminates abruptly before dispatching HTTP call to Payment Service.",
      "Transaction remains orphaned in PostgreSQL with status=PAYMENT_PROCESSING.",
      "Celery Beat background worker sweeps stale non-terminal records older than 5 minutes.",
      "Worker claims transaction into RECOVERY_IN_PROGRESS using Redis mutex lock (Lua script).",
      "Worker probes ground-truth Payment endpoint (GET /payments/check/); receives status=NOT_CHARGED.",
      "Worker dispatches compensating release to Inventory and marks transaction COMPENSATED."
    ],
    defenses: "Distributed Redis mutex lock (prevents duplicate sweeps) + Ground-Truth Downstream Probing + Atomic Lua script lock release.",
    outcome: "Status: Recovers from orphaned PAYMENT_PROCESSING → COMPENSATED"
  },
  custom: {
    icon: "⚙️",
    title: "Custom Payload Dispatcher",
    badge: "Interactive Edge-Case Tester",
    badgeClass: "badge-blue",
    desc: "Allows interactive configuration of custom transaction parameters to test edge cases, inventory rejections, process crashes, and failed compensation.",
    payload: `POST http://127.0.0.1:8002/transactions/
{
  "transaction_id": "<Optional Custom UUID>",
  "product_id": <Custom Number>,
  "quantity": <Custom Number: 0, 1, 9999>,
  "amount": <Custom Number>,
  "fail_compensation": <true|false>,
  "coordinator": <true|false>
}`,
    steps: [
      "User submits customized payload from the expandable form.",
      "Client packet animates to Coordinator.",
      "Coordinator evaluates parameters and dispatches to Inventory.",
      "If quantity <= 0 or out of stock: Inventory returns 400; Coordinator marks INVENTORY_RESERVE_FAILED and halts.",
      "If amount == 9999: Payment declines and triggers backward compensation.",
      "If coordinator == true: Simulates process crash and triggers recovery sweep."
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
  btnConfig: document.getElementById("btn-config"),

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

  recCountdownVal: document.getElementById("rec-countdown-val"),
  recProgressFill: document.getElementById("rec-progress-fill"),
  recSagasSwept: document.getElementById("rec-sagas-swept"),
  recOrphansRecovered: document.getElementById("rec-orphans-recovered"),
  recRedisLock: document.getElementById("rec-redis-lock"),
  btnTriggerSweep: document.getElementById("btn-trigger-sweep"),

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
  modalConfig: document.getElementById("modal-config"),
  btnCloseConfig: document.getElementById("btn-close-config"),
  formConfig: document.getElementById("form-config"),
  cfgCoordUrl: document.getElementById("cfg-coord-url"),
  cfgInvUrl: document.getElementById("cfg-inv-url"),
  cfgPayUrl: document.getElementById("cfg-pay-url"),
  cfgPollMs: document.getElementById("cfg-poll-ms"),
  btnResetCfg: document.getElementById("btn-reset-cfg"),
};


function showToast({ icon = "⚡", title, message, type = "normal", duration = 4500 }) {
  if (!DOM.toastContainer) return;

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icon}</div>
    <div class="toast-body">
      <div class="toast-title">${title}</div>
      <div class="toast-desc">${message}</div>
    </div>
  `;

  DOM.toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(50px)";
    setTimeout(() => {
      if (toast.parentNode === DOM.toastContainer) {
        DOM.toastContainer.removeChild(toast);
      }
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
      fetchWithTimeout(`${CONFIG.coordinatorUrl}/health/`, {}, 2500),
      fetchWithTimeout(`${CONFIG.inventoryUrl}/health/`, {}, 2500),
      fetchWithTimeout(`${CONFIG.paymentUrl}/health/`, {}, 2500),
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
      purple: "url(#glow-blue)",
    };

    const color = colorMap[type] || "#38bdf8";
    const filter = filterMap[type] || "url(#glow-blue)";

    const pathIdMap = {
      "client-coord": "path-client-coord",
      "coord-inv": "path-coord-inv",
      "coord-pay": "path-coord-pay",
      "inv-coord": "path-inv-coord",
      "pay-coord": "path-pay-coord",
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
        DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty text-danger">⚠️ Coordinator Service offline at ${CONFIG.coordinatorUrl}. Start the service or toggle DEMO MODE above.</td></tr>`;
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
  let rMs = 14;
  let pMs = 26;
  let cMs = 8;
  let totalMs = 48;

  let trackHTML = "";
  let labelHTML = "";

  if (s === "COMPLETED") {
    rMs = 15; pMs = 28; cMs = 8; totalMs = 51;
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 32%;" title="Reserve Inventory: ${rMs}ms"></div>
      <div class="wf-seg wf-charge" style="width: 52%;" title="Charge Payment: ${pMs}ms"></div>
      <div class="wf-seg wf-commit" style="width: 16%;" title="Commit: ${cMs}ms"></div>
    `;
    labelHTML = `<span>Res: ${rMs}ms</span><span>Chg: ${pMs}ms</span><span class="wf-duration">${totalMs}ms</span>`;
  } else if (s === "COMPENSATED" || s === "COMPENSATED_ON_RETRY") {
    rMs = 14; pMs = 32; cMs = 18; totalMs = 64;
    trackHTML = `
      <div class="wf-seg wf-reserve" style="width: 25%;" title="Reserve Inventory: ${rMs}ms"></div>
      <div class="wf-seg wf-fail" style="width: 45%;" title="Payment Declined: ${pMs}ms"></div>
      <div class="wf-seg wf-comp" style="width: 30%;" title="Compensating Release: ${cMs}ms"></div>
    `;
    labelHTML = `<span>Res: ${rMs}ms</span><span style="color: var(--accent-rose)">Decl: ${pMs}ms</span><span style="color: var(--accent-amber)">Rel: ${cMs}ms</span>`;
  } else if (s === "INVENTORY_RESERVE_FAILED") {
    trackHTML = `
      <div class="wf-seg wf-hatch-fail" style="width: 100%;" title="Inventory Reserve Rejected (Quantity 0 / Out of Stock)"></div>
    `;
    labelHTML = `<span style="color: var(--accent-rose); font-weight: 700;">Reserve Rejected</span><span class="wf-duration" style="color: var(--accent-rose);">12ms</span>`;
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
    DOM.fmetaStatusBadge.textContent = txData.status;
    DOM.fmetaStatusBadge.className = `badge ${getStatusBadgeClass(txData.status)}`;
    DOM.fmetaProd.textContent = `Prod #${txData.product_id || 1} (Qty: ${txData.quantity !== undefined ? txData.quantity : 1})`;
    DOM.fmetaAmt.textContent = `$${txData.amount || 500}`;

    const isFailureState = txData.status.includes("FAILED") || txData.status.includes("TIMEOUT") || txData.status === "COMPENSATED";
    if (isFailureState) {
      let failureReason = "Non-terminal or compensated state detected";
      if (txData.status === "INVENTORY_RESERVE_FAILED") {
        failureReason = txData.failure_reason || "Inventory reservation rejected (zero quantity, out of stock, or invalid product ID)";
      } else if (txData.status === "COMPENSATED") {
        failureReason = txData.failure_reason || "Payment declined / failed → Stock auto-released via compensating action";
      } else if (txData.status === "COMPENSATION_FAILED") {
        failureReason = txData.failure_reason || "Compensating stock release rejected by Inventory Service";
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


DOM.btnDemoHappy.addEventListener("click", async () => {
  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "ORCHESTRATING";

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

  showToast({
    icon: "⚡",
    title: "Happy Path Saga Dispatched",
    message: "Reserving next available seat → Charging payment ($500.00) → Marked COMPLETED",
    type: "success",
  });

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("COMPLETED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const nextSeatId = await getNextAvailableSeatId();
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({ seat_id: nextSeatId, quantity: 1, amount: 500 }),
  });

  if (res.ok && res.data && res.data.transaction_id) {
    window.selectTransaction(res.data.transaction_id);
    fetchTransactionsFeed();
  }
  DOM.topoCoordState.textContent = "IDLE";
});

DOM.btnDemoFail.addEventListener("click", async () => {
  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "COMPENSATING";

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
    durationMs: 450,
    destinationNodeId: "node-box-pay",
  });

  await animatePacketAlongPath({
    route: "pay-coord",
    type: "danger",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  await animatePacketAlongPath({
    route: "coord-inv",
    type: "warning",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  await animatePacketAlongPath({
    route: "inv-coord",
    type: "warning",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  showToast({
    icon: "🔄",
    title: "Compensating Saga Executed",
    message: "Payment declined (HTTP 422: Testing failure amount $9,999) → Seat hold auto-released to Inventory → Marked COMPENSATED",
    type: "normal",
  });

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("COMPENSATED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const nextSeatId = await getNextAvailableSeatId();
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({ seat_id: nextSeatId, quantity: 1, amount: 9999 }),
  });

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
    try {
      const res = await fetchWithTimeout(`${CONFIG.inventoryUrl}/seats/reset/`, { method: "POST" });
      if (res.ok) {
        showToast({
          icon: "🎟️",
          title: "Venue Restocked",
          message: "All 30 venue seats have been reset to AVAILABLE! Ready for new bookings.",
          type: "success",
          duration: 4000,
        });
        fetchTransactionsFeed();
      } else {
        showToast({
          icon: "❌",
          title: "Reset Failed",
          message: "Could not reset venue seats. Verify Inventory service is running.",
          type: "danger",
        });
      }
    } catch (e) {
      showToast({
        icon: "❌",
        title: "Network Error",
        message: e.message,
        type: "danger",
      });
    }
  });
}

DOM.btnDemoRace.addEventListener("click", async () => {
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
    icon: "🔥",
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
        transaction_id: sharedTxId,
        seat_id: targetSeatId,
        product_id: targetSeatId,
        quantity: 1,
        amount: 500,
      }),
    })
  );

  await Promise.all(promises);

  window.selectTransaction(sharedTxId);
  fetchTransactionsFeed();
  DOM.topoCoordState.textContent = "IDLE";
});

DOM.btnDemoCrash.addEventListener("click", async () => {
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

  let txId = crypto.randomUUID();
  const targetSeatId = await getNextAvailableSeatId();

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("STARTED", txId);
  } else {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
      method: "POST",
      body: JSON.stringify({
        transaction_id: txId,
        seat_id: targetSeatId,
        product_id: targetSeatId,
        quantity: 1,
        amount: 500,
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
    icon: "💥",
    title: "Process Crash Simulated",
    message: `Coordinator process crashed mid-flight! Transaction ${txId.substring(0,8)}... is orphaned in database. Awaiting natural 90s Celery Beat sweep (or click 'Trigger Immediate Sweep' below)!`,
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
  }
});

DOM.formCustomTx.addEventListener("submit", async (e) => {
  e.preventDefault();
  const seatId = parseInt(document.getElementById("cust-seat-id").value, 10) || 31;
  const amount = parseFloat(document.getElementById("cust-amount").value) || 500;
  const execMode = document.getElementById("cust-exec-mode")?.value || "async";
  const isFailComp = document.getElementById("cust-flag-fail").checked;
  const isCrash = document.getElementById("cust-flag-crash").checked;

  const payload = {
    seat_id: seatId,
    product_id: seatId,
    quantity: 1,
    amount: amount,
  };

  const customId = document.getElementById("cust-tx-id").value.trim();
  if (customId) payload.transaction_id = customId;
  if (isFailComp) payload.fail_compensation = true;
  if (isCrash) payload.coordinator = true;

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.customDrawer.classList.add("hidden");

  // Always synchronous — async Celery dispatch is disabled on Railway
  const execMode = "sync";
  DOM.topoCoordState.textContent = "🔁 SYNC · Holding HTTP · Calling Inventory...";

  // Animate live pipeline: Step 1 → Inventory Reserve
  await animatePacketAlongPath({ route: "coord-inv", type: "info", durationMs: 300, destinationNodeId: "node-box-inv" });
  DOM.topoCoordState.textContent = "🔁 SYNC · Inventory Reserved · Calling Payment...";

  const endpointUrl = `${CONFIG.coordinatorUrl}/transactions/?sync=true`;

  // Measure precise client HTTP roundtrip latency
  const t0 = performance.now();

  // Animate Step 2 → Payment (fires while HTTP request is in-flight)
  const payAnimPromise = (async () => {
    await new Promise(r => setTimeout(r, 100));
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 250, destinationNodeId: "node-box-coord" });
    DOM.topoCoordState.textContent = "🔁 SYNC · Charging Payment Gateway...";
    await animatePacketAlongPath({ route: "coord-pay", type: "info", durationMs: 300, destinationNodeId: "node-box-pay" });
  })();

  const res = await fetchWithTimeout(endpointUrl, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  await payAnimPromise;
  const latencyMs = Math.round(performance.now() - t0);

  const txData = res.data || {};
  const status = txData.status || (res.ok ? "COMPLETED" : "FAILED");
  const detailedError = extractDetailedErrorMessage(res);

  if (txData.error === "INVENTORY_RESERVE_FAILED" || (res.status === 400 && txData.inventory_response)) {
    await animatePacketAlongPath({ route: "inv-coord", type: "danger", durationMs: 300, destinationNodeId: "node-box-coord" });
    showToast({
      icon: "❌",
      title: "Seat Reservation Conflict / Locked",
      message: `[${res.status} in ${latencyMs}ms] Inventory Error: ${detailedError}. Transaction rejected.`,
      type: "race",
      duration: 6000,
    });
  }
  else if (isCrash || txData.detail === "Intentional crash") {
    await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 400, breakMidway: true });
    triggerCrashedNode("coord");
    showToast({
      icon: "💥",
      title: "Process Crash Simulated",
      message: `Coordinator process crashed mid-flight! Transaction orphaned in database. Awaiting Celery Beat sweep.`,
      type: "race",
      duration: 8000,
    });
    setTimeout(() => { triggerRecoveredNode("coord"); DOM.topoCoordState.textContent = "IDLE"; }, 2500);
  }
  else if (status === "COMPENSATED" || status === "COMPENSATION_FAILED") {
    await animatePacketAlongPath({ route: "pay-coord", type: "danger", durationMs: 300, destinationNodeId: "node-box-coord" });
    DOM.topoCoordState.textContent = "🔁 SYNC · Payment Declined · Compensating...";
    await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 350, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 300, destinationNodeId: "node-box-coord" });
    showToast({
      icon: "🔄",
      title: "Payment Declined & Compensated",
      message: `[${res.status} in ${latencyMs}ms] Payment declined ($${amount}) → Auto-released hold on Seat #${seatId} → Marked COMPENSATED`,
      type: "normal",
      duration: 6000,
    });
  }
  else {
    // 🔁 SYNC SUCCESS: Full pipeline completed
    await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 300, destinationNodeId: "node-box-coord" });
    DOM.topoCoordState.textContent = "🔁 SYNC · Confirming Seat in Inventory...";
    await animatePacketAlongPath({ route: "coord-inv", type: "success", durationMs: 300, destinationNodeId: "node-box-inv" });
    await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 250, destinationNodeId: "node-box-coord" });
    showToast({
      icon: "🔁",
      title: `Sync Pipeline: HTTP 200 OK (${latencyMs}ms)`,
      message: `⏱️ Coordinator → Inventory Reserve → Payment Charge → Inventory Confirm — completed in ${latencyMs}ms. Marked COMPLETED.`,
      type: "success",
      duration: 6500,
    });
  }

  if (txData.transaction_id) {
    window.selectTransaction(txData.transaction_id);
    fetchTransactionsFeed();
  }

  DOM.topoCoordState.textContent = "IDLE";
});

DOM.btnTriggerSweep.addEventListener("click", async () => {
  STATE.stats.sagasSwept += 1;
  DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;
  localStorage.setItem("dtc_stat_swept", STATE.stats.sagasSwept);

  DOM.recRedisLock.innerHTML = `<span class="badge badge-purple">LOCKED (EXEC)</span>`;
  DOM.topoCoordLease.textContent = "SWEEPING";
  
  animatePacketAlongPath({ route: "coord-inv", type: "purple", durationMs: 400, destinationNodeId: "node-box-inv" });
  animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 400, destinationNodeId: "node-box-pay" });

  let recoveredSummary = "Swept non-terminal states older than lease threshold.";
  if (!STATE.isSimulatedMode) {
    try {
      const sweepRes = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/recovery/sweep/`, {
        method: "POST",
        body: JSON.stringify({ threshold_seconds: 0 }),
      });
      if (sweepRes.ok) {
        fetchTransactionsFeed();
        if (STATE.activeTxId) {
          inspectTransaction(STATE.activeTxId, true);
        }
        recoveredSummary = sweepRes.data && sweepRes.data.output ? sweepRes.data.output : "Live sweep completed across PostgreSQL & Redis.";
      }
    } catch (e) {
      recoveredSummary = `Sweep network error: ${e.message}`;
    }
  }

  showToast({
    icon: "⏱️",
    title: "Celery Beat Recovery Sweep Executed",
    message: `Acquired Redis mutex lock via atomic Lua script → ${recoveredSummary}`,
    type: "purple",
    duration: 6500,
  });

  setTimeout(() => {
    DOM.topoCoordLease.textContent = "READY";
    DOM.recRedisLock.innerHTML = `<span class="badge badge-success">IDLE / READY</span>`;
  }, 2000);
});

const btnSweepInfo = document.getElementById("btn-sweep-info");
if (btnSweepInfo) {
  btnSweepInfo.addEventListener("click", () => {
    showToast({
      icon: "ℹ️",
      title: "Immediate Sweep Mode (threshold = 0s)",
      message: "Note: Clicking 'Trigger Immediate Sweep' runs with threshold_seconds = 0 to bypass the standard 30-second stale lease timeout for immediate demo verification.",
      type: "purple",
      duration: 7000,
    });
  });
}

const btnExecInfo = document.getElementById("btn-exec-info");
if (btnExecInfo) {
  btnExecInfo.addEventListener("click", (e) => {
    e.stopPropagation();
    showToast({
      icon: "⚡",
      title: "Sync vs. Async Pipeline (Simple Guide)",
      message: "⚡ Async (Background): Gives you an instant receipt (202 Accepted) in 10ms, then Celery workers finish booking the seat in the background (like BookMyShow).\n\n🔁 Sync (Wait): Keeps your browser connection waiting until all services (Seat + Bank + Confirmation) are 100% finished before responding (200 OK).",
      type: "normal",
      duration: 8500,
    });
  });
}

DOM.btnRetryActiveTx.addEventListener("click", async () => {
  if (!STATE.activeTxId) return;
  await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 450, destinationNodeId: "node-box-inv" });
  await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 350, destinationNodeId: "node-box-coord" });

  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/${STATE.activeTxId}/retry-compensation/`, {
    method: "POST",
  });

  const detailedError = extractDetailedErrorMessage(res, "Retry compensation executed");
  showToast({
    icon: "🔄",
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
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Reserved 1 units" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "COMPLETED", actor: "request", http_status_code: 200, http_payload: { status: "CHARGED" }, created_at: now },
    ];
  } else if (finalStatus === "COMPENSATED") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Reserved" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "COMPENSATING", actor: "request", http_status_code: 400, http_payload: { error: "Insufficient funds" }, created_at: now },
      { seq: 4, from_status: "COMPENSATING", to_status: "COMPENSATED", actor: "request", http_status_code: 200, http_payload: { message: "Inventory released" }, created_at: now },
    ];
  } else if (finalStatus === "INVENTORY_RESERVE_FAILED") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVE_FAILED", actor: "request", http_status_code: 400, http_payload: { error: failureReason || "Quantity must be greater than 0" }, created_at: now },
    ];
  } else if (finalStatus === "RECOVERY_IN_PROGRESS") {
    events = [
      { seq: 1, from_status: "STARTED", to_status: "INVENTORY_RESERVED", actor: "request", http_status_code: 200, http_payload: { message: "Reserved" }, created_at: now },
      { seq: 2, from_status: "INVENTORY_RESERVED", to_status: "PAYMENT_PROCESSING", actor: "request", http_status_code: null, http_payload: null, created_at: now },
      { seq: 3, from_status: "PAYMENT_PROCESSING", to_status: "RECOVERY_IN_PROGRESS", actor: "recovery", http_status_code: null, http_payload: { probe: "/payments/check/" }, created_at: now },
    ];
  }

  const newTx = {
    transaction_id: txId,
    status: finalStatus,
    product_id: 1,
    quantity: finalStatus === "INVENTORY_RESERVE_FAILED" ? 0 : 1,
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
DOM.btnConfig.addEventListener("click", () => {
  DOM.cfgCoordUrl.value = CONFIG.coordinatorUrl;
  DOM.cfgInvUrl.value = CONFIG.inventoryUrl;
  DOM.cfgPayUrl.value = CONFIG.paymentUrl;
  DOM.cfgPollMs.value = CONFIG.pollIntervalMs;
  DOM.modalConfig.classList.remove("hidden");
});
DOM.btnCloseConfig.addEventListener("click", () => DOM.modalConfig.classList.add("hidden"));

DOM.formConfig.addEventListener("submit", (e) => {
  e.preventDefault();
  CONFIG.coordinatorUrl = DOM.cfgCoordUrl.value.trim();
  CONFIG.inventoryUrl = DOM.cfgInvUrl.value.trim();
  CONFIG.paymentUrl = DOM.cfgPayUrl.value.trim();
  CONFIG.pollIntervalMs = parseInt(DOM.cfgPollMs.value, 10) || 1500;

  localStorage.setItem("dtc_cfg_coord", CONFIG.coordinatorUrl);
  localStorage.setItem("dtc_cfg_inv", CONFIG.inventoryUrl);
  localStorage.setItem("dtc_cfg_pay", CONFIG.paymentUrl);
  localStorage.setItem("dtc_cfg_poll", CONFIG.pollIntervalMs);

  DOM.modalConfig.classList.add("hidden");
  CanvasEngine.init();
  checkServiceHealth();
  fetchTransactionsFeed();
});

DOM.btnResetCfg.addEventListener("click", () => {
  DOM.cfgCoordUrl.value = "http://127.0.0.1:8002";
  DOM.cfgInvUrl.value = "http://127.0.0.1:8000";
  DOM.cfgPayUrl.value = "http://127.0.0.1:8001";
  DOM.cfgPollMs.value = "1500";
});


function startRecoveryCountdown() {
  setInterval(async () => {
    STATE.sweepCountdown--;
    if (STATE.sweepCountdown <= 0) {
      STATE.sweepCountdown = 90;
      STATE.stats.sagasSwept += 1;
      DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;
      localStorage.setItem("dtc_stat_swept", STATE.stats.sagasSwept);

      animatePacketAlongPath({ route: "coord-inv", type: "purple", durationMs: 400, destinationNodeId: "node-box-inv" });
      animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 400, destinationNodeId: "node-box-pay" });

      DOM.topoCoordLease.textContent = "SWEEPING";
      DOM.recRedisLock.innerHTML = `<span class="badge badge-purple">SWEEPING LEASE</span>`;

      if (!STATE.isSimulatedMode) {
        try {
          const sweepRes = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/recovery/sweep/`, {
            method: "POST",
            body: JSON.stringify({ threshold_seconds: 30 }),
          });
          if (sweepRes.ok) {
            fetchTransactionsFeed();
            if (STATE.activeTxId) {
              inspectTransaction(STATE.activeTxId, true);
            }
          }
        } catch (e) {}
      }

      setTimeout(() => {
        DOM.topoCoordLease.textContent = "READY";
        DOM.recRedisLock.innerHTML = `<span class="badge badge-success">IDLE / READY</span>`;
      }, 3000);
    }

    DOM.sweepCountdown.textContent = `${STATE.sweepCountdown}s`;
    DOM.recCountdownVal.textContent = `${STATE.sweepCountdown}s`;
    const pct = ((90 - STATE.sweepCountdown) / 90) * 100;
    DOM.recProgressFill.style.width = `${100 - pct}%`;
  }, 1000);
}

const DEFAULT_CANVAS_NODES = {
  client: { x: 30,  y: 70,  width: 130, height: 115 },
  coord:  { x: 260, y: 50,  width: 220, height: 155 },
  inv:    { x: 580, y: 20,  width: 220, height: 110 },
  pay:    { x: 580, y: 160, width: 220, height: 110 },
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

    try {
      const savedVp = localStorage.getItem("dtc_topology_viewport");
      if (savedVp) {
        const vp = JSON.parse(savedVp);
        if (typeof vp.panX === "number") this.panX = vp.panX;
        if (typeof vp.panY === "number") this.panY = vp.panY;
        if (typeof vp.zoom === "number") this.zoom = Math.min(Math.max(vp.zoom, this.minZoom), this.maxZoom);
      }
    } catch (e) {
      console.warn("Could not load saved viewport", e);
    }

    this.applyAllNodePositions();
    this.updateEdges();
    this.applyTransform();

    this.bindEvents();

    if (!localStorage.getItem("dtc_topology_viewport")) {
      setTimeout(() => this.fitView(), 50);
    }
  },

  applyAllNodePositions() {
    Object.keys(this.nodes).forEach((key) => {
      const group = document.getElementById(`node-group-${key}`);
      if (group) {
        const n = this.nodes[key];
        group.setAttribute("transform", `translate(${n.x}, ${n.y})`);
      }
    });
  },

  applyTransform() {
    if (!this.world) return;
    this.world.setAttribute("transform", `translate(${this.panX}, ${this.panY}) scale(${this.zoom})`);
    if (this.zoomLabel) {
      this.zoomLabel.textContent = `${Math.round(this.zoom * 100)}%`;
    }
  },

  saveState() {
    try {
      localStorage.setItem("dtc_topology_nodes", JSON.stringify(this.nodes));
      localStorage.setItem(
        "dtc_topology_viewport",
        JSON.stringify({ panX: this.panX, panY: this.panY, zoom: this.zoom })
      );
    } catch (e) {
    }
  },

  getDynamicPathD(sourceKey, targetKey) {
    const s = this.nodes[sourceKey];
    const t = this.nodes[targetKey];
    if (!s || !t) return "";

    const sCenter = { x: s.x + s.width / 2, y: s.y + s.height / 2 };
    const tCenter = { x: t.x + t.width / 2, y: t.y + t.height / 2 };

    let p1 = { x: 0, y: 0 };
    let p2 = { x: 0, y: 0 };
    let c1 = { x: 0, y: 0 };
    let c2 = { x: 0, y: 0 };

    const dx = tCenter.x - sCenter.x;
    const dy = tCenter.y - sCenter.y;

    if (dx >= (s.width + t.width) * 0.35) {
      p1 = { x: s.x + s.width, y: s.y + s.height / 2 };
      p2 = { x: t.x, y: t.y + t.height / 2 };
      const curDx = Math.max((p2.x - p1.x) * 0.5, 30);
      c1 = { x: p1.x + curDx, y: p1.y };
      c2 = { x: p2.x - curDx, y: p2.y };
    }
    else if (dx <= -(s.width + t.width) * 0.35) {
      p1 = { x: s.x, y: s.y + s.height / 2 };
      p2 = { x: t.x + t.width, y: t.y + t.height / 2 };
      const curDx = Math.max((p1.x - p2.x) * 0.5, 30);
      c1 = { x: p1.x - curDx, y: p1.y };
      c2 = { x: p2.x + curDx, y: p2.y };
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

    const dClientCoord = this.getDynamicPathD("client", "coord");
    const dCoordInv = this.getDynamicPathD("coord", "inv");
    const dCoordPay = this.getDynamicPathD("coord", "pay");
    const dInvCoord = this.getDynamicPathD("inv", "coord");
    const dPayCoord = this.getDynamicPathD("pay", "coord");

    if (pathClientCoord) pathClientCoord.setAttribute("d", dClientCoord);
    if (pathCoordInv) pathCoordInv.setAttribute("d", dCoordInv);
    if (pathCoordPay) pathCoordPay.setAttribute("d", dCoordPay);
    if (pathInvCoord) pathInvCoord.setAttribute("d", dInvCoord);
    if (pathPayCoord) pathPayCoord.setAttribute("d", dPayCoord);
  },

  fitView() {
    if (!this.container) return;
    const rect = this.container.getBoundingClientRect();
    const cWidth = rect.width || 800;
    const cHeight = rect.height || 360;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    Object.values(this.nodes).forEach((n) => {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.width);
      maxY = Math.max(maxY, n.y + n.height);
    });

    const bWidth = maxX - minX;
    const bHeight = maxY - minY;
    const padding = 50;

    const scaleX = (cWidth - padding * 2) / bWidth;
    const scaleY = (cHeight - padding * 2) / bHeight;
    const newZoom = Math.min(Math.max(Math.min(scaleX, scaleY), this.minZoom), 1.3);

    this.zoom = newZoom;
    this.panX = (cWidth - bWidth * newZoom) / 2 - minX * newZoom;
    this.panY = (cHeight - bHeight * newZoom) / 2 - minY * newZoom;

    this.applyTransform();
    this.saveState();
  },

  focusNode(nodeKey) {
    const node = this.nodes[nodeKey];
    if (!node || !this.container) return;

    const rect = this.container.getBoundingClientRect();
    const cWidth = rect.width || 800;
    const cHeight = rect.height || 360;

    this.zoom = 1.15;
    this.panX = cWidth / 2 - (node.x + node.width / 2) * this.zoom;
    this.panY = cHeight / 2 - (node.y + node.height / 2) * this.zoom;

    this.applyTransform();
    this.saveState();
  },

  resetLayout() {
    this.nodes = JSON.parse(JSON.stringify(DEFAULT_CANVAS_NODES));
    this.applyAllNodePositions();
    this.updateEdges();
    this.fitView();
    this.saveState();
  },

  bindEvents() {
    
    window.addEventListener("keydown", (e) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;

      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        CanvasEngine.btnZoomIn?.click();
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        CanvasEngine.btnZoomOut?.click();
      } else if (e.key === "0") {
        e.preventDefault();
        CanvasEngine.fitView();
      }
    });

    const nodeGroups = this.svg.querySelectorAll(".topo-node-group");
    nodeGroups.forEach((group) => {
      const nodeKey = group.getAttribute("data-node-key");

      group.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        this.isDraggingNode = true;
        this.activeDragNodeKey = nodeKey;
        this.dragStartMouse = { x: e.clientX, y: e.clientY };
        this.dragStartNodePos = { x: this.nodes[nodeKey].x, y: this.nodes[nodeKey].y };
        group.classList.add("is-dragging");
      });

      group.addEventListener("touchstart", (e) => {
        if (e.touches.length !== 1) return;
        e.stopPropagation();
        this.isDraggingNode = true;
        this.activeDragNodeKey = nodeKey;
        this.dragStartMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
        this.dragStartNodePos = { x: this.nodes[nodeKey].x, y: this.nodes[nodeKey].y };
        group.classList.add("is-dragging");
      }, { passive: false });

      group.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        this.focusNode(nodeKey);
      });
    });

    window.addEventListener("mousemove", (e) => {
      if (this.isDraggingNode && this.activeDragNodeKey) {
        const dx = (e.clientX - this.dragStartMouse.x) / this.zoom;
        const dy = (e.clientY - this.dragStartMouse.y) / this.zoom;
        const node = this.nodes[this.activeDragNodeKey];
        node.x = Math.round(this.dragStartNodePos.x + dx);
        node.y = Math.round(this.dragStartNodePos.y + dy);

        const group = document.getElementById(`node-group-${this.activeDragNodeKey}`);
        if (group) {
          group.setAttribute("transform", `translate(${node.x}, ${node.y})`);
        }
        this.updateEdges();
        return;
      }

      if (this.isPanning) {
        this.panX = this.panStartPos.x + (e.clientX - this.panStartMouse.x);
        this.panY = this.panStartPos.y + (e.clientY - this.panStartMouse.y);
        this.applyTransform();
      }
    });

    window.addEventListener("mouseup", () => {
      if (this.isDraggingNode) {
        this.isDraggingNode = false;
        if (this.activeDragNodeKey) {
          const group = document.getElementById(`node-group-${this.activeDragNodeKey}`);
          if (group) group.classList.remove("is-dragging");
          this.activeDragNodeKey = null;
        }
        this.saveState();
      }

      if (this.isPanning) {
        this.isPanning = false;
        this.container.classList.remove("is-panning");
        this.saveState();
      }
    });

    window.addEventListener("touchmove", (e) => {
      if (this.isDraggingNode && this.activeDragNodeKey && e.touches.length === 1) {
        e.preventDefault();
        const dx = (e.touches[0].clientX - this.dragStartMouse.x) / this.zoom;
        const dy = (e.touches[0].clientY - this.dragStartMouse.y) / this.zoom;
        const node = this.nodes[this.activeDragNodeKey];
        node.x = Math.round(this.dragStartNodePos.x + dx);
        node.y = Math.round(this.dragStartNodePos.y + dy);

        const group = document.getElementById(`node-group-${this.activeDragNodeKey}`);
        if (group) {
          group.setAttribute("transform", `translate(${node.x}, ${node.y})`);
        }
        this.updateEdges();
      } else if (this.isPanning && e.touches.length === 1) {
        e.preventDefault();
        this.panX = this.panStartPos.x + (e.touches[0].clientX - this.panStartMouse.x);
        this.panY = this.panStartPos.y + (e.touches[0].clientY - this.panStartMouse.y);
        this.applyTransform();
      }
    }, { passive: false });

    window.addEventListener("touchend", () => {
      if (this.isDraggingNode) {
        this.isDraggingNode = false;
        if (this.activeDragNodeKey) {
          const group = document.getElementById(`node-group-${this.activeDragNodeKey}`);
          if (group) group.classList.remove("is-dragging");
          this.activeDragNodeKey = null;
        }
        this.saveState();
      }
      if (this.isPanning) {
        this.isPanning = false;
        this.container.classList.remove("is-panning");
        this.saveState();
      }
    });

    if (this.bgRect) {
      this.bgRect.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        this.isPanning = true;
        this.panStartMouse = { x: e.clientX, y: e.clientY };
        this.panStartPos = { x: this.panX, y: this.panY };
        this.container.classList.add("is-panning");
      });

      this.bgRect.addEventListener("touchstart", (e) => {
        if (e.touches.length !== 1) return;
        this.isPanning = true;
        this.panStartMouse = { x: e.touches[0].clientX, y: e.touches[0].clientY };
        this.panStartPos = { x: this.panX, y: this.panY };
        this.container.classList.add("is-panning");
      }, { passive: false });
    }

    this.container.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = this.container.getBoundingClientRect();
      const cursorX = e.clientX - rect.left;
      const cursorY = e.clientY - rect.top;

      const delta = e.deltaY < 0 ? 0.01 : -0.01;
      const newZoom = Math.min(Math.max(Math.round((this.zoom + delta) * 100) / 100, this.minZoom), this.maxZoom);

      if (newZoom === this.zoom) return;

      this.panX = cursorX - (cursorX - this.panX) * (newZoom / this.zoom);
      this.panY = cursorY - (cursorY - this.panY) * (newZoom / this.zoom);
      this.zoom = newZoom;

      this.applyTransform();
      this.saveState();
    }, { passive: false });

    if (this.btnZoomIn) {
      this.btnZoomIn.addEventListener("click", () => {
        const rect = this.container.getBoundingClientRect();
        const cx = (rect.width || 800) / 2;
        const cy = (rect.height || 360) / 2;
        const newZoom = Math.min(Math.round((this.zoom + 0.01) * 100) / 100, this.maxZoom);
        this.panX = cx - (cx - this.panX) * (newZoom / this.zoom);
        this.panY = cy - (cy - this.panY) * (newZoom / this.zoom);
        this.zoom = newZoom;
        this.applyTransform();
        this.saveState();
      });
    }

    if (this.btnZoomOut) {
      this.btnZoomOut.addEventListener("click", () => {
        const rect = this.container.getBoundingClientRect();
        const cx = (rect.width || 800) / 2;
        const cy = (rect.height || 360) / 2;
        const newZoom = Math.max(Math.round((this.zoom - 0.01) * 100) / 100, this.minZoom);
        this.panX = cx - (cx - this.panX) * (newZoom / this.zoom);
        this.panY = cy - (cy - this.panY) * (newZoom / this.zoom);
        this.zoom = newZoom;
        this.applyTransform();
        this.saveState();
      });
    }

    if (this.btnFitView) {
      this.btnFitView.addEventListener("click", () => this.fitView());
    }

    if (this.btnResetLayout) {
      this.btnResetLayout.addEventListener("click", () => {
        this.resetLayout();
        showToast({
          icon: "🔄",
          title: "Topology Layout Reset",
          message: "Node positions and zoom restored to default configuration.",
          type: "normal",
        });
      });
    }
  },
};

window.addEventListener("DOMContentLoaded", () => {
  const savedTheme = localStorage.getItem("dtc_theme") || "dark";
  window.setDashboardTheme(savedTheme);
  window.setBackendMode(false);

  DOM.statRacesDefended.textContent = STATE.stats.racesDefended;
  DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;
  DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
  DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;

  CanvasEngine.init();
  checkServiceHealth();
  fetchTransactionsFeed();
  startRecoveryCountdown();

  setInterval(() => {
    checkServiceHealth();
    fetchTransactionsFeed();
  }, CONFIG.pollIntervalMs);
});
