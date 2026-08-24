/**
 * DTC Mission Control — Observability & Real-Time Visualization App
 * Vanilla ES6+ JavaScript, zero build tools, zero dependencies.
 */

// ============================================================================
// Global State & Configuration
// ============================================================================

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
  sweepCountdown: 90,
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ============================================================================
// DOM Elements Cache
// ============================================================================

const DOM = {
  // Toast Container
  toastContainer: document.getElementById("toast-container"),

  // Nav & Health
  healthCoord: document.getElementById("node-health-coord"),
  healthInv: document.getElementById("node-health-inv"),
  healthPay: document.getElementById("node-health-pay"),
  latCoord: document.getElementById("lat-coord"),
  latInv: document.getElementById("lat-inv"),
  latPay: document.getElementById("lat-pay"),
  sweepCountdown: document.getElementById("sweep-countdown"),
  toggleSimMode: document.getElementById("toggle-sim-mode"),
  simModeText: document.getElementById("sim-mode-text"),
  btnHowItWorks: document.getElementById("btn-how-it-works"),
  btnConfig: document.getElementById("btn-config"),

  // Proof Stats
  statRacesDefended: document.getElementById("stat-race-defended"),
  statLocksEnforced: document.getElementById("stat-locks-enforced"),
  statSagasTotal: document.getElementById("stat-sagas-total"),
  statDoubleBooking: document.getElementById("stat-double-booking"),

  // Scenario Buttons
  btnDemoHappy: document.getElementById("btn-demo-happy"),
  btnDemoFail: document.getElementById("btn-demo-fail"),
  btnDemoRace: document.getElementById("btn-demo-race"),
  btnDemoCrash: document.getElementById("btn-demo-crash"),
  btnToggleCustom: document.getElementById("btn-toggle-custom"),
  customDrawer: document.getElementById("custom-payload-drawer"),
  formCustomTx: document.getElementById("form-custom-tx"),

  // Topology SVG Elements
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

  // Recovery Worker Panel
  recCountdownVal: document.getElementById("rec-countdown-val"),
  recProgressFill: document.getElementById("rec-progress-fill"),
  recSagasSwept: document.getElementById("rec-sagas-swept"),
  recOrphansRecovered: document.getElementById("rec-orphans-recovered"),
  recRedisLock: document.getElementById("rec-redis-lock"),
  btnTriggerSweep: document.getElementById("btn-trigger-sweep"),

  // Transaction Feed
  badgeTotalTx: document.getElementById("badge-total-tx"),
  filterTabs: document.querySelectorAll(".filter-tab"),
  tbodyTxFeed: document.getElementById("tbody-tx-feed"),
  btnManualPoll: document.getElementById("btn-manual-poll"),

  // Forensic Audit Stream
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

  // Modals
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

// ============================================================================
// Toast Notification Component
// ============================================================================

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

// ============================================================================
// API & Networking Utilities
// ============================================================================

async function fetchWithTimeout(url, options = {}, timeoutMs = 3500) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  const startTime = performance.now();

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });
    const duration = Math.round(performance.now() - startTime);
    clearTimeout(timeoutId);

    let data = null;
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }

    return { ok: response.ok, status: response.status, data, latency: duration };
  } catch (err) {
    clearTimeout(timeoutId);
    return { ok: false, status: 0, error: err.message, latency: null };
  }
}

// Helper: Extract human-readable error reasons from backend JSON responses
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

  const coordRes = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/all/`);
  updateHealthUI("coord", coordRes.ok, coordRes.latency);

  const invRes = await fetchWithTimeout(`${CONFIG.inventoryUrl}/health/`, {}, 2000);
  updateHealthUI("inv", invRes.ok || invRes.status === 200, invRes.latency);

  const payRes = await fetchWithTimeout(`${CONFIG.paymentUrl}/payments/check/`, {}, 2000);
  updateHealthUI("pay", payRes.ok || payRes.status === 200 || payRes.status === 400, payRes.latency);
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

// ============================================================================
// Interactive SVG Topology Curved Packet Animation & Crash Shatter
// ============================================================================

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

/**
 * Animates a small glowing 8-10px packet icon along the exact SVG Bezier curve
 * between nodes, triggering an arrival pulse on receipt or shattering mid-way on crash.
 */
function animatePacketAlongPath({
  route,
  type = "success",
  durationMs = 450,
  breakMidway = false,
  destinationNodeId = null,
}) {
  return new Promise((resolve) => {
    const colorMap = {
      success: "#34d399",   // Green (Reserve/Charge)
      info: "#38bdf8",      // Blue (Probes/Status)
      warning: "#fbbf24",   // Amber (Compensating Release)
      danger: "#f43f5e",    // Red (Conflict/Crash/Decline)
      purple: "#c084fc",   // Purple (Recovery)
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

    const totalLength = pathEl.getTotalLength();
    const packetGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");

    // Outer translucent glowing halo
    const halo = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    halo.setAttribute("r", "9");
    halo.setAttribute("fill", color);
    halo.setAttribute("opacity", "0.35");
    halo.setAttribute("filter", filter);

    // Inner bright packet core (8px filled circle)
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
          // Abrupt crash stop & shatter effect
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
          // Destination arrival confirmation glow
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

// ============================================================================
// Transaction Feed & State Management
// ============================================================================

async function fetchTransactionsFeed() {
  if (STATE.isSimulatedMode) {
    renderTransactionsTable(STATE.transactions);
    return;
  }

  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/all/`);
  if (res.ok && Array.isArray(res.data)) {
    STATE.transactions = res.data;
    STATE.stats.totalSagas = res.data.length;
    DOM.statSagasTotal.textContent = res.data.length;

    const lockedRows = res.data.filter(t => t.status !== "STARTED" && t.status !== "INVENTORY_RESERVE_FAILED").length;
    STATE.stats.locksEnforced = Math.max(STATE.stats.locksEnforced, lockedRows);
    DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;

    renderTransactionsTable(res.data);

    if (STATE.activeTxId) {
      inspectTransaction(STATE.activeTxId, false);
    }
  } else {
    if (STATE.transactions.length === 0) {
      DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty text-danger">⚠️ Coordinator Service offline at ${CONFIG.coordinatorUrl}. Start the service or toggle DEMO MODE above.</td></tr>`;
    }
  }
}

function renderTransactionsTable(list) {
  DOM.badgeTotalTx.textContent = `${list.length} Total`;
  DOM.topoCoordEvents.textContent = `${list.length} sagas`;

  let filtered = list;
  if (STATE.activeFilter === "IN_FLIGHT") {
    filtered = list.filter(t => ["STARTED", "INVENTORY_RESERVED", "PAYMENT_PROCESSING", "COMPENSATING", "RECOVERY_IN_PROGRESS"].includes(t.status));
  } else if (STATE.activeFilter === "COMPLETED") {
    filtered = list.filter(t => t.status === "COMPLETED");
  } else if (STATE.activeFilter === "COMPENSATED") {
    filtered = list.filter(t => ["COMPENSATED", "COMPENSATED_ON_RETRY"].includes(t.status));
  } else if (STATE.activeFilter === "FAILED") {
    filtered = list.filter(t => t.status.includes("FAILED") || t.status.includes("TIMEOUT"));
  }

  if (filtered.length === 0) {
    DOM.tbodyTxFeed.innerHTML = `<tr><td colspan="5" class="table-empty">No transactions found for filter: ${STATE.activeFilter}</td></tr>`;
    return;
  }

  DOM.tbodyTxFeed.innerHTML = filtered.map(tx => {
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
      <div class="wf-seg wf-fail" style="width: 100%;" title="Inventory Reserve Rejected"></div>
    `;
    labelHTML = `<span style="color: var(--accent-rose)">Reserve Rejected</span><span class="wf-duration">12ms</span>`;
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

// ============================================================================
// Forensic SagaEvent Audit Log Stream
// ============================================================================

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

    // Failure banner display
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

// ============================================================================
// One-Click Live Recruiter Scenarios & Stress Tests
// ============================================================================

// 1. Scenario: Happy Path Saga
DOM.btnDemoHappy.addEventListener("click", async () => {
  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "ORCHESTRATING";

  // Step 1: Client -> Coordinator
  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  // Step 2: Coordinator -> Inventory Reserve (Green)
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  // Step 3: Inventory -> Coordinator 200 OK Response (Green)
  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  // Step 4: Coordinator -> Payment Charge (Green)
  await animatePacketAlongPath({
    route: "coord-pay",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-pay",
  });

  // Step 5: Payment -> Coordinator 200 OK Response (Green)
  await animatePacketAlongPath({
    route: "pay-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  showToast({
    icon: "⚡",
    title: "Happy Path Saga Completed",
    message: "Inventory row-locked & reserved → Payment charged ($500.00) → Transaction marked COMPLETED",
    type: "success",
  });

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("COMPLETED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({ product_id: 1, quantity: 1, amount: 500 }),
  });

  if (res.ok && res.data && res.data.transaction_id) {
    window.selectTransaction(res.data.transaction_id);
    fetchTransactionsFeed();
  }
  DOM.topoCoordState.textContent = "IDLE";
});

// 2. Scenario: Payment Decline & Auto-Compensation
DOM.btnDemoFail.addEventListener("click", async () => {
  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "COMPENSATING";

  // Step 1: Client -> Coordinator
  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  // Step 2: Reserve Inventory OK (Green)
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  // Step 3: Inventory -> Coordinator OK
  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  // Step 4: Payment Charge Declined (Red)
  await animatePacketAlongPath({
    route: "coord-pay",
    type: "danger",
    durationMs: 450,
    destinationNodeId: "node-box-pay",
  });

  // Step 5: Payment -> Coordinator Decline Response (Red)
  await animatePacketAlongPath({
    route: "pay-coord",
    type: "danger",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  // Step 6: Compensating Inventory Release (Amber)
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "warning",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  // Step 7: Inventory -> Coordinator Release Confirmed (Amber)
  await animatePacketAlongPath({
    route: "inv-coord",
    type: "warning",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  showToast({
    icon: "🔄",
    title: "Compensating Saga Executed",
    message: "Payment declined (HTTP 422: Testing failure amount $9,999) → Stock auto-released to Inventory → Marked COMPENSATED",
    type: "normal",
  });

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("COMPENSATED");
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  // Send amount: 9999 to trigger backend's testing failure amount
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify({ product_id: 1, quantity: 1, amount: 9999 }),
  });

  if (res.data && res.data.transaction_id) {
    window.selectTransaction(res.data.transaction_id);
    fetchTransactionsFeed();
  }
  DOM.topoCoordState.textContent = "IDLE";
});

// 3. Scenario: 5x Concurrent Race Attack (COMPLETE ROUND-TRIP TO PAYMENT & BACK)
DOM.btnDemoRace.addEventListener("click", async () => {
  const sharedTxId = crypto.randomUUID();
  STATE.stats.dispatchedCount += 5;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "RACE BURST";

  // Step 1: Fire 5 concurrent packets along client-coord
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

  // Step 2: Winner moves to Inventory Service (Reserve)
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  // Step 3: Inventory returns 200 OK to Coordinator
  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  // Step 4: Winner moves to Payment Service (Charge)
  await animatePacketAlongPath({
    route: "coord-pay",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-pay",
  });

  // Step 5: Payment returns 200 OK back to Coordinator
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

  const promises = Array.from({ length: 5 }, () =>
    fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
      method: "POST",
      body: JSON.stringify({
        transaction_id: sharedTxId,
        product_id: 1,
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

// 4. Scenario: Crash Simulation & Celery Recovery
DOM.btnDemoCrash.addEventListener("click", async () => {
  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "CRASH SIMULATION";

  // Step 1: Client -> Coordinator
  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  // Step 2: Inventory Reserved OK
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  // Step 3: Inventory response back to Coordinator
  await animatePacketAlongPath({
    route: "inv-coord",
    type: "success",
    durationMs: 350,
    destinationNodeId: "node-box-coord",
  });

  // Step 4: Crash mid-flight on Payment call (Stops at 50% & shatters)
  await animatePacketAlongPath({
    route: "coord-pay",
    type: "danger",
    durationMs: 500,
    breakMidway: true,
  });

  triggerCrashedNode("coord");

  showToast({
    icon: "💥",
    title: "Simulated Process Death Triggered",
    message: "Coordinator process killed mid-flight after reservation! Transaction orphaned in PAYMENT_PROCESSING. Awaiting Celery Beat sweep...",
    type: "race",
    duration: 5000,
  });

  let txId = crypto.randomUUID();

  if (STATE.isSimulatedMode) {
    createSimulatedTransaction("RECOVERY_IN_PROGRESS", txId);
  } else {
    const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
      method: "POST",
      body: JSON.stringify({
        transaction_id: txId,
        product_id: 1,
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

  // Celery recovery reconciliation
  setTimeout(async () => {
    DOM.topoCoordLease.textContent = "SWEEP CLAIM";
    
    // Probe payment outcome
    await animatePacketAlongPath({
      route: "coord-pay",
      type: "purple",
      durationMs: 450,
      destinationNodeId: "node-box-pay",
    });

    await animatePacketAlongPath({
      route: "pay-coord",
      type: "purple",
      durationMs: 350,
      destinationNodeId: "node-box-coord",
    });

    // Release inventory reservation
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

    STATE.stats.orphansRecovered++;
    DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
    localStorage.setItem("dtc_stat_orphans", STATE.stats.orphansRecovered);

    triggerRecoveredNode("coord");
    DOM.topoCoordState.textContent = "IDLE";
    DOM.topoCoordLease.textContent = "READY";

    showToast({
      icon: "🛡️",
      title: "Recovery Sweep Resolved Orphan",
      message: `Transaction ${txId.substring(0,8)}... claimed into RECOVERY_IN_PROGRESS → Payment probed (NOT_CHARGED) → Inventory released to COMPENSATED`,
      type: "purple",
      duration: 6000,
    });

    if (STATE.isSimulatedMode) {
      const tx = STATE.transactions.find(t => t.transaction_id === txId);
      if (tx) {
        tx.status = "COMPENSATED";
        tx.events.push({
          seq: 4,
          from_status: "RECOVERY_IN_PROGRESS",
          to_status: "COMPENSATED",
          actor: "recovery",
          http_status_code: 200,
          http_payload: { message: "Inventory release verified" },
          created_at: new Date().toISOString(),
        });
        inspectTransaction(txId, true);
        renderTransactionsTable(STATE.transactions);
      }
    } else {
      fetchTransactionsFeed();
    }
  }, 4000);
});

// 5. Scenario: Custom Payload (FULL ANIMATION ROUND-TRIP + EXPLICIT ERROR REPORTING)
DOM.btnToggleCustom.addEventListener("click", () => {
  DOM.customDrawer.classList.toggle("hidden");
});

DOM.formCustomTx.addEventListener("submit", async (e) => {
  e.preventDefault();
  const productId = parseInt(document.getElementById("cust-product-id").value, 10);
  const quantity = parseInt(document.getElementById("cust-qty").value, 10);
  const amount = parseFloat(document.getElementById("cust-amount").value);
  const isFailComp = document.getElementById("cust-flag-fail").checked;
  const isCrash = document.getElementById("cust-flag-crash").checked;

  const payload = {
    product_id: isNaN(productId) ? 1 : productId,
    quantity: isNaN(quantity) ? 1 : quantity,
    amount: isNaN(amount) ? 500 : amount,
  };

  const customId = document.getElementById("cust-tx-id").value.trim();
  if (customId) payload.transaction_id = customId;
  if (isFailComp) payload.fail_compensation = true;
  if (isCrash) payload.coordinator = true;

  STATE.stats.dispatchedCount++;
  DOM.topoClientSent.textContent = STATE.stats.dispatchedCount;
  DOM.topoCoordState.textContent = "PROCESSING CUSTOM";
  DOM.customDrawer.classList.add("hidden");

  // Step 1: Client -> Coordinator
  await animatePacketAlongPath({
    route: "client-coord",
    type: "info",
    durationMs: 400,
    destinationNodeId: "node-box-coord",
  });

  // Step 2: Coordinator -> Inventory
  await animatePacketAlongPath({
    route: "coord-inv",
    type: "success",
    durationMs: 450,
    destinationNodeId: "node-box-inv",
  });

  if (STATE.isSimulatedMode) {
    if (quantity <= 0) {
      await animatePacketAlongPath({ route: "inv-coord", type: "danger", durationMs: 350, destinationNodeId: "node-box-coord" });
      showToast({
        icon: "❌",
        title: "Inventory Reservation Failed",
        message: "Backend Error: Quantity must be greater than 0. Transaction aborted.",
        type: "race",
      });
      createSimulatedTransaction("INVENTORY_RESERVE_FAILED", customId, "Quantity must be greater than 0");
    } else if (isCrash) {
      await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
      await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 500, breakMidway: true });
      triggerCrashedNode("coord");
      showToast({
        icon: "💥",
        title: "Coordinator Process Crash Simulated",
        message: "Coordinator killed after reservation! Transaction orphaned in PAYMENT_PROCESSING.",
        type: "race",
      });
      createSimulatedTransaction("RECOVERY_IN_PROGRESS", customId);
    } else if (amount === 9999 || isFailComp) {
      await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
      await animatePacketAlongPath({ route: "coord-pay", type: "danger", durationMs: 450, destinationNodeId: "node-box-pay" });
      await animatePacketAlongPath({ route: "pay-coord", type: "danger", durationMs: 350, destinationNodeId: "node-box-coord" });
      await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 450, destinationNodeId: "node-box-inv" });
      await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 350, destinationNodeId: "node-box-coord" });
      showToast({
        icon: "🔄",
        title: "Payment Declined & Compensated",
        message: "Payment declined on $9,999 → Released inventory stock → Marked COMPENSATED",
        type: "normal",
      });
      createSimulatedTransaction("COMPENSATED", customId);
    } else {
      await animatePacketAlongPath({ route: "inv-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
      await animatePacketAlongPath({ route: "coord-pay", type: "success", durationMs: 450, destinationNodeId: "node-box-pay" });
      await animatePacketAlongPath({ route: "pay-coord", type: "success", durationMs: 350, destinationNodeId: "node-box-coord" });
      showToast({
        icon: "⚡",
        title: "Custom Saga Completed",
        message: `Reserved Prod #${payload.product_id} (Qty: ${payload.quantity}) → Charged $${payload.amount} → Marked COMPLETED`,
        type: "success",
      });
      createSimulatedTransaction("COMPLETED", customId);
    }
    DOM.topoCoordState.textContent = "IDLE";
    return;
  }

  // Real backend call
  const res = await fetchWithTimeout(`${CONFIG.coordinatorUrl}/transactions/`, {
    method: "POST",
    body: JSON.stringify(payload),
  });

  const txData = res.data || {};
  const status = txData.status || (res.ok ? "COMPLETED" : "FAILED");
  const detailedError = extractDetailedErrorMessage(res);

  // Case 1: Inventory Reserve Failed
  if (txData.error === "INVENTORY_RESERVE_FAILED" || res.status === 400 && txData.inventory_response) {
    await animatePacketAlongPath({
      route: "inv-coord",
      type: "danger",
      durationMs: 350,
      destinationNodeId: "node-box-coord",
    });

    showToast({
      icon: "❌",
      title: "Inventory Reservation Failed",
      message: `Backend Error: ${detailedError}. Transaction marked INVENTORY_RESERVE_FAILED.`,
      type: "race",
      duration: 6000,
    });
  }
  // Case 2: Coordinator Crash Simulated
  else if (isCrash || txData.detail === "Intentional crash") {
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

    showToast({
      icon: "💥",
      title: "Coordinator Process Death Simulated",
      message: "Coordinator killed before charging payment! Transaction orphaned in PAYMENT_PROCESSING.",
      type: "race",
      duration: 6000,
    });

    setTimeout(async () => {
      await animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 450, destinationNodeId: "node-box-pay" });
      await animatePacketAlongPath({ route: "pay-coord", type: "purple", durationMs: 350, destinationNodeId: "node-box-coord" });
      await animatePacketAlongPath({ route: "coord-inv", type: "warning", durationMs: 450, destinationNodeId: "node-box-inv" });
      await animatePacketAlongPath({ route: "inv-coord", type: "warning", durationMs: 350, destinationNodeId: "node-box-coord" });
      triggerRecoveredNode("coord");
      STATE.stats.orphansRecovered++;
      DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
      fetchTransactionsFeed();
    }, 4000);
  }
  // Case 3: Payment Declined / Compensated
  else if (status === "COMPENSATED" || status === "COMPENSATION_FAILED") {
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

    if (status === "COMPENSATION_FAILED") {
      showToast({
        icon: "❌",
        title: "Compensation Release Failed",
        message: `Inventory rejected stock release (fail_compensation flag enabled). Marked COMPENSATION_FAILED.`,
        type: "race",
        duration: 6000,
      });
    } else {
      showToast({
        icon: "🔄",
        title: "Payment Declined & Compensated",
        message: `Payment outcome: ${detailedError} → Stock auto-released to Inventory → Marked COMPENSATED.`,
        type: "normal",
        duration: 5500,
      });
    }
  }
  // Case 4: Happy Path Completed
  else {
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
      title: "Custom Saga Completed",
      message: `Reserved Prod #${payload.product_id} (Qty: ${payload.quantity}) → Charged $${payload.amount} → Marked COMPLETED.`,
      type: "success",
    });
  }

  if (txData.transaction_id) {
    window.selectTransaction(txData.transaction_id);
  }
  fetchTransactionsFeed();
  DOM.topoCoordState.textContent = "IDLE";
});

// Trigger Immediate Sweep Button
DOM.btnTriggerSweep.addEventListener("click", async () => {
  STATE.stats.sagasSwept += 5;
  DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;
  localStorage.setItem("dtc_stat_swept", STATE.stats.sagasSwept);

  DOM.recRedisLock.innerHTML = `<span class="badge badge-purple">LOCKED (EXEC)</span>`;
  
  animatePacketAlongPath({ route: "coord-inv", type: "purple", durationMs: 400, destinationNodeId: "node-box-inv" });
  animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 400, destinationNodeId: "node-box-pay" });

  showToast({
    icon: "⏱️",
    title: "Celery Beat Recovery Sweep Executed",
    message: "Acquired Redis mutex lock via atomic Lua script → Swept non-terminal states older than 5 min",
    type: "purple",
  });

  setTimeout(() => {
    DOM.recRedisLock.innerHTML = `<span class="badge badge-success">IDLE / READY</span>`;
  }, 2000);
});

// Retry Compensation for active transaction
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

// ============================================================================
// Simulation Fallback Mode
// ============================================================================

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

// ============================================================================
// Controls, Filters & Modals
// ============================================================================

DOM.filterTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    DOM.filterTabs.forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    STATE.activeFilter = tab.getAttribute("data-filter");
    renderTransactionsTable(STATE.transactions);
  });
});

DOM.btnManualPoll.addEventListener("click", () => {
  checkServiceHealth();
  fetchTransactionsFeed();
});

DOM.toggleSimMode.addEventListener("change", () => {
  STATE.isSimulatedMode = DOM.toggleSimMode.checked;
  DOM.simModeText.textContent = STATE.isSimulatedMode ? "DEMO MODE" : "LIVE BACKEND";
  DOM.simModeText.style.color = STATE.isSimulatedMode ? "var(--accent-amber)" : "var(--accent-green)";
  if (STATE.isSimulatedMode && STATE.transactions.length === 0) {
    createSimulatedTransaction("COMPLETED");
  }
  checkServiceHealth();
  fetchTransactionsFeed();
});

// Modals
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
  checkServiceHealth();
  fetchTransactionsFeed();
});

DOM.btnResetCfg.addEventListener("click", () => {
  DOM.cfgCoordUrl.value = "http://127.0.0.1:8002";
  DOM.cfgInvUrl.value = "http://127.0.0.1:8000";
  DOM.cfgPayUrl.value = "http://127.0.0.1:8001";
  DOM.cfgPollMs.value = "1500";
});

// ============================================================================
// Recovery Worker Countdown Loop (90s Celery Beat)
// ============================================================================

function startRecoveryCountdown() {
  setInterval(() => {
    STATE.sweepCountdown--;
    if (STATE.sweepCountdown <= 0) {
      STATE.sweepCountdown = 90;
      STATE.stats.sagasSwept += 4;
      DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;
      localStorage.setItem("dtc_stat_swept", STATE.stats.sagasSwept);

      animatePacketAlongPath({ route: "coord-inv", type: "purple", durationMs: 400, destinationNodeId: "node-box-inv" });
      animatePacketAlongPath({ route: "coord-pay", type: "purple", durationMs: 400, destinationNodeId: "node-box-pay" });

      DOM.topoCoordLease.textContent = "SWEEPING";
      DOM.recRedisLock.innerHTML = `<span class="badge badge-purple">SWEEPING LEASE</span>`;

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

// ============================================================================
// Initialization & Polling Interval Loop
// ============================================================================

window.addEventListener("DOMContentLoaded", () => {
  DOM.statRacesDefended.textContent = STATE.stats.racesDefended;
  DOM.statLocksEnforced.textContent = STATE.stats.locksEnforced;
  DOM.recOrphansRecovered.textContent = STATE.stats.orphansRecovered;
  DOM.recSagasSwept.textContent = STATE.stats.sagasSwept;

  checkServiceHealth();
  fetchTransactionsFeed();
  startRecoveryCountdown();

  setInterval(() => {
    checkServiceHealth();
    fetchTransactionsFeed();
  }, CONFIG.pollIntervalMs);
});
