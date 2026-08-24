/**
 * Distributed Saga Coordinator — Dev Control Panel JS
 * Pure vanilla JavaScript + Fetch API. Zero build steps, zero dependencies.
 */

// ============================================================================
// Global Configuration
// ============================================================================

// Editable single base URL constant. Can be updated live via the UI.
const DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8002";

let coordinatorBaseUrl = localStorage.getItem("dtc_coordinator_url") || DEFAULT_COORDINATOR_URL;
let activeInspectTxId = null;
let autoRefreshTimer = null;
let currentEventList = [];

// ============================================================================
// DOM Element References
// ============================================================================

const elUrlInput = document.getElementById("coordinator-url");
const elBtnSaveUrl = document.getElementById("btn-save-url");
const elBtnPing = document.getElementById("btn-ping");
const elConnectionStatus = document.getElementById("connection-status");

// Create Transaction Form Elements
const elFormCreate = document.getElementById("form-create-tx");
const elCreateTxId = document.getElementById("create-tx-id");
const elCreateProductId = document.getElementById("create-product-id");
const elCreateQuantity = document.getElementById("create-quantity");
const elCreateAmount = document.getElementById("create-amount");
const elFlagFailComp = document.getElementById("flag-fail-compensation");
const elFlagCoordCrash = document.getElementById("flag-coordinator-crash");
const elCreateRespContainer = document.getElementById("create-response-container");
const elCreateStatusCode = document.getElementById("create-status-code");
const elCreateResponseBody = document.getElementById("create-response-body");

// Retry Compensation Form Elements
const elFormRetry = document.getElementById("form-retry-comp");
const elRetryTxId = document.getElementById("retry-tx-id");
const elRetryRespContainer = document.getElementById("retry-response-container");
const elRetryStatusCode = document.getElementById("retry-status-code");
const elRetryResponseBody = document.getElementById("retry-response-body");

// Recent Transactions
const elBtnRefreshAll = document.getElementById("btn-refresh-all");
const elTbodyRecent = document.getElementById("tbody-recent-tx");

// Inspect & SagaEvent Audit Viewer Elements
const elInspectTxId = document.getElementById("inspect-tx-id");
const elBtnInspect = document.getElementById("btn-inspect");
const elBtnRefreshInspect = document.getElementById("btn-refresh-inspect");
const elToggleAutoRefresh = document.getElementById("toggle-auto-refresh");

const elTxMetaCard = document.getElementById("tx-meta-card");
const elMetaTxId = document.getElementById("meta-tx-id");
const elMetaStatusBadge = document.getElementById("meta-status-badge");
const elMetaProductId = document.getElementById("meta-product-id");
const elMetaQuantity = document.getElementById("meta-quantity");
const elMetaAmount = document.getElementById("meta-amount");
const elMetaUpdatedAt = document.getElementById("meta-updated-at");

const elInspectErrorBox = document.getElementById("inspect-error-box");
const elInspectErrorTitle = document.getElementById("inspect-error-title");
const elInspectErrorBody = document.getElementById("inspect-error-body");

const elEventCountBadge = document.getElementById("event-count-badge");
const elTbodyEvents = document.getElementById("tbody-events");
const elPayloadInspector = document.getElementById("payload-inspector");
const elPayloadTitle = document.getElementById("payload-title");
const elPayloadContent = document.getElementById("payload-content");
const elBtnClosePayload = document.getElementById("btn-close-payload");

// ============================================================================
// API Helper Utilities
// ============================================================================

async function apiRequest(path, options = {}) {
  const url = `${coordinatorBaseUrl.replace(/\/+$/, "")}${path}`;
  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(options.headers || {}),
      },
    });

    let data;
    const text = await response.text();
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw_response: text };
    }

    return {
      ok: response.ok,
      status: response.status,
      data: data,
      error: response.ok ? null : (data.error || data.detail || `HTTP ${response.status}`),
    };
  } catch (err) {
    return {
      ok: false,
      status: 0,
      data: null,
      error: `Network Error: Could not reach Coordinator at ${url}. Check if the service is running and CORS is enabled. (${err.message})`,
    };
  }
}

function getStatusBadgeClass(status) {
  if (!status) return "badge-neutral";
  const s = status.toUpperCase();
  if (s.includes("COMPLETED")) return "badge-success";
  if (s.includes("COMPENSATED_ON_RETRY") || s === "COMPENSATED") return "badge-blue";
  if (s.includes("FAILED") || s.includes("ERROR") || s.includes("TIMEOUT")) return "badge-danger";
  if (s.includes("RECOVERY")) return "badge-purple";
  return "badge-warning";
}

function getActorBadgeClass(actor) {
  if (!actor) return "badge-neutral";
  if (actor === "request") return "badge-cyan";
  if (actor === "recovery") return "badge-purple";
  if (actor === "retry") return "badge-warning";
  return "badge-neutral";
}

// ============================================================================
// Connection Health & URL Management
// ============================================================================

function initUrlConfig() {
  elUrlInput.value = coordinatorBaseUrl;
  checkConnection();

  elBtnSaveUrl.addEventListener("click", () => {
    const val = elUrlInput.value.trim();
    if (val) {
      coordinatorBaseUrl = val;
      localStorage.setItem("dtc_coordinator_url", val);
      checkConnection();
    }
  });

  elBtnPing.addEventListener("click", checkConnection);
}

async function checkConnection() {
  elConnectionStatus.className = "status-indicator";
  elConnectionStatus.textContent = "● Checking...";

  const res = await apiRequest("/transactions/all/");
  if (res.ok) {
    elConnectionStatus.className = "status-indicator connected";
    elConnectionStatus.textContent = "● Connected (HTTP 200)";
  } else {
    elConnectionStatus.className = "status-indicator disconnected";
    elConnectionStatus.textContent = `● Offline (${res.status || "Unreachable"})`;
  }
}

// ============================================================================
// Action 1: Create Transaction
// ============================================================================

elFormCreate.addEventListener("submit", async (e) => {
  e.preventDefault();

  const payload = {
    product_id: parseInt(elCreateProductId.value, 10),
    quantity: parseInt(elCreateQuantity.value, 10),
    amount: parseFloat(elCreateAmount.value),
  };

  const customTxId = elCreateTxId.value.trim();
  if (customTxId) {
    payload.transaction_id = customTxId;
  }

  if (elFlagFailComp.checked) payload.fail_compensation = true;
  if (elFlagCoordCrash.checked) payload.coordinator = true;

  elCreateRespContainer.classList.remove("hidden");
  elCreateStatusCode.textContent = "Sending...";
  elCreateStatusCode.className = "badge badge-neutral";
  elCreateResponseBody.textContent = "Awaiting response from coordinator...";

  const res = await apiRequest("/transactions/", {
    method: "POST",
    body: JSON.stringify(payload),
  });

  elCreateStatusCode.textContent = `HTTP ${res.status}`;
  elCreateStatusCode.className = `badge ${res.ok ? "badge-success" : "badge-danger"}`;
  elCreateResponseBody.textContent = JSON.stringify(res.data, null, 2);

  // Auto-inspect the newly created/returned transaction
  const tid = res.data && res.data.transaction_id;
  if (tid) {
    elInspectTxId.value = tid;
    elRetryTxId.value = tid;
    inspectTransaction(tid);
  }

  // Refresh recent list
  fetchRecentTransactions();
});

// ============================================================================
// Action 2: Retry Compensation
// ============================================================================

elFormRetry.addEventListener("submit", async (e) => {
  e.preventDefault();
  const tid = elRetryTxId.value.trim();
  if (!tid) return;

  elRetryRespContainer.classList.remove("hidden");
  elRetryStatusCode.textContent = "Sending...";
  elRetryStatusCode.className = "badge badge-neutral";
  elRetryResponseBody.textContent = "Executing retry compensation...";

  const res = await apiRequest(`/transactions/${tid}/retry-compensation/`, {
    method: "POST",
  });

  elRetryStatusCode.textContent = `HTTP ${res.status}`;
  elRetryStatusCode.className = `badge ${res.ok ? "badge-success" : "badge-danger"}`;
  elRetryResponseBody.textContent = JSON.stringify(res.data, null, 2);

  // Auto-refresh the inspect view for this transaction
  elInspectTxId.value = tid;
  inspectTransaction(tid);
  fetchRecentTransactions();
});

// ============================================================================
// Action 3: Inspect Transaction & SagaEvent Audit Log
// ============================================================================

elBtnInspect.addEventListener("click", () => {
  const tid = elInspectTxId.value.trim();
  if (tid) inspectTransaction(tid);
});

elBtnRefreshInspect.addEventListener("click", () => {
  if (activeInspectTxId) inspectTransaction(activeInspectTxId);
});

async function inspectTransaction(tid) {
  if (!tid) return;
  activeInspectTxId = tid;

  // Hide errors and inspector initially
  elInspectErrorBox.classList.add("hidden");
  elPayloadInspector.classList.add("hidden");

  // 1. Fetch transaction record
  const txRes = await apiRequest(`/transactions/${tid}/`);
  
  // 2. Fetch SagaEvent audit log
  const eventsRes = await apiRequest(`/transactions/${tid}/events/`);

  if (!txRes.ok && !eventsRes.ok) {
    elTxMetaCard.classList.add("hidden");
    elInspectErrorBox.classList.remove("hidden");
    elInspectErrorTitle.textContent = `Transaction Inspection Error (HTTP ${txRes.status}):`;
    elInspectErrorBody.textContent = JSON.stringify(txRes.data || txRes.error, null, 2);
    renderEventsTable([]);
    return;
  }

  // Render Metadata Card
  if (txRes.ok && txRes.data) {
    const tx = txRes.data;
    elTxMetaCard.classList.remove("hidden");
    elMetaTxId.textContent = tx.transaction_id || tid;
    elMetaStatusBadge.textContent = tx.status || "UNKNOWN";
    elMetaStatusBadge.className = `badge ${getStatusBadgeClass(tx.status)}`;
    elMetaProductId.textContent = tx.product_id ?? "-";
    elMetaQuantity.textContent = tx.quantity ?? "-";
    elMetaAmount.textContent = tx.amount ? `$${tx.amount}` : "-";
    elMetaUpdatedAt.textContent = tx.updated_at ? new Date(tx.updated_at).toLocaleTimeString() : "-";
  }

  // Render Events Table
  if (eventsRes.ok && eventsRes.data) {
    const events = eventsRes.data.events || [];
    currentEventList = events;
    elEventCountBadge.textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
    renderEventsTable(events);
  } else {
    renderEventsTable([]);
  }
}

function renderEventsTable(events) {
  if (!events || events.length === 0) {
    elTbodyEvents.innerHTML = `<tr><td colspan="6" class="text-muted text-center">No SagaEvent rows recorded for this transaction yet.</td></tr>`;
    return;
  }

  elTbodyEvents.innerHTML = events.map((ev, index) => {
    const fromBadge = `<span class="badge ${getStatusBadgeClass(ev.from_status)}">${ev.from_status || "START"}</span>`;
    const toBadge = `<span class="badge ${getStatusBadgeClass(ev.to_status)}">${ev.to_status}</span>`;
    const actorBadge = `<span class="badge ${getActorBadgeClass(ev.actor)}">${ev.actor}</span>`;
    
    let httpBadge = `<span class="text-muted">-</span>`;
    if (ev.http_status_code) {
      const codeClass = ev.http_status_code < 400 ? "badge-success" : "badge-danger";
      httpBadge = `<span class="badge ${codeClass}">HTTP ${ev.http_status_code}</span>`;
    }

    const timeStr = ev.created_at ? new Date(ev.created_at).toLocaleTimeString() : "-";
    const hasPayload = ev.http_payload && Object.keys(ev.http_payload).length > 0;

    return `
      <tr>
        <td class="font-mono" style="font-weight: 700;">${ev.seq}</td>
        <td>
          <div class="transition-flow">
            ${fromBadge}
            <span class="arrow">→</span>
            ${toBadge}
          </div>
        </td>
        <td>${actorBadge}</td>
        <td>${httpBadge}</td>
        <td class="text-muted font-mono" style="font-size: 11px;">${timeStr}</td>
        <td>
          ${hasPayload ? `<button class="btn btn-sm btn-secondary" onclick="viewPayload(${index})">JSON</button>` : `<span class="text-muted">-</span>`}
        </td>
      </tr>
    `;
  }).join("");
}

window.viewPayload = function(eventIndex) {
  const ev = currentEventList[eventIndex];
  if (!ev) return;

  elPayloadInspector.classList.remove("hidden");
  elPayloadTitle.textContent = `Payload for Event Seq #${ev.seq} (${ev.from_status} → ${ev.to_status}):`;
  elPayloadContent.textContent = JSON.stringify(ev.http_payload, null, 2);
};

elBtnClosePayload.addEventListener("click", () => {
  elPayloadInspector.classList.add("hidden");
});

// ============================================================================
// Action 4: Recent Transactions List
// ============================================================================

elBtnRefreshAll.addEventListener("click", fetchRecentTransactions);

async function fetchRecentTransactions() {
  elTbodyRecent.innerHTML = `<tr><td colspan="4" class="text-muted text-center">Loading transactions...</td></tr>`;

  const res = await apiRequest("/transactions/all/");
  if (!res.ok || !Array.isArray(res.data)) {
    elTbodyRecent.innerHTML = `<tr><td colspan="4" class="text-danger text-center">Failed to load transactions (HTTP ${res.status})</td></tr>`;
    return;
  }

  const list = res.data.slice(0, 10); // Show latest 10
  if (list.length === 0) {
    elTbodyRecent.innerHTML = `<tr><td colspan="4" class="text-muted text-center">No transactions found</td></tr>`;
    return;
  }

  elTbodyRecent.innerHTML = list.map((tx) => {
    const statusBadge = `<span class="badge ${getStatusBadgeClass(tx.status)}">${tx.status}</span>`;
    const shortId = tx.transaction_id.substring(0, 8) + "...";
    return `
      <tr>
        <td class="font-mono" title="${tx.transaction_id}">${shortId}</td>
        <td>${statusBadge}</td>
        <td class="font-mono">$${tx.amount}</td>
        <td>
          <button class="btn btn-sm btn-secondary" onclick="selectTransaction('${tx.transaction_id}')">Inspect</button>
        </td>
      </tr>
    `;
  }).join("");
}

window.selectTransaction = function(tid) {
  elInspectTxId.value = tid;
  elRetryTxId.value = tid;
  inspectTransaction(tid);
};

// ============================================================================
// Auto-Refresh Loop (2 Seconds)
// ============================================================================

elToggleAutoRefresh.addEventListener("change", () => {
  if (elToggleAutoRefresh.checked) {
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(() => {
    if (activeInspectTxId) {
      inspectTransaction(activeInspectTxId);
    }
  }, 2000);
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

// ============================================================================
// Initialize App
// ============================================================================

window.addEventListener("DOMContentLoaded", () => {
  initUrlConfig();
  fetchRecentTransactions();
});
