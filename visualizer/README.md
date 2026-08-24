# DTC Mission Control — Real-Time Systems Observability Visualizer

A zero-dependency, single-page observability dashboard and live demo tool for the **Distributed Transaction Coordinator (DTC)**.

Designed specifically for **technical screen-shares and recruiter demos** to visually prove:
1. **Saga Orchestration** across 3 independent microservices (Coordinator, Inventory, Payment).
2. **PostgreSQL Row-Level Locking (`select_for_update`) & `UNIQUE` Idempotency Constraints** preventing double-booking under concurrent load.
3. **Append-Only `SagaEvent` Audit Stream** with immutable forensic event tracing.
4. **Celery Beat Crash Recovery (90s sweep)** automatically reconciling orphaned transactions.

---

## Quick Start (How to Run)

### Option 1: Serve with Python (Recommended)
```bash
cd visualizer
python -m http.server 3000
```
Open **[http://localhost:3000](http://localhost:3000)** in your browser.

### Option 2: Open Directly in Browser
Double-click `index.html` or open `file:///.../DTC/visualizer/index.html` in Chrome/Firefox/Edge.

---

## Pointing at Different Environments (Local vs. Staging vs. Docker)

You can customize the base URLs for all three services dynamically without rebuilding any code:

1. Click the **Gear Icon (⚙️)** in the top-right header of the visualizer.
2. Update the service endpoints:
   - **Local Dev:**
     - Coordinator: `http://127.0.0.1:8002`
     - Inventory: `http://127.0.0.1:8000`
     - Payment: `http://127.0.0.1:8001`
   - **Docker Compose / Staging:**
     - Coordinator: `http://coordinator.staging.internal:8002`
     - Inventory: `http://inventory.staging.internal:8000`
     - Payment: `http://payment.staging.internal:8001`
3. Click **Save & Reconnect**. Settings are persisted to browser `localStorage`.

---

## Key Dashboard Features for Recruiter Demos

### 1. Live Service Topology & RPC Pulses
- Dynamic SVG graph visualizing traffic from **Client $\to$ Coordinator $\to$ Inventory & Payment**.
- Animated color-coded packet pulses trigger whenever RPC calls happen:
  - 🟢 **Green:** Stock Reservation & Payment Charge
  - 🔵 **Blue:** Ground-truth status probe (`/check/`)
  - 🟠 **Orange:** Compensating stock release
  - 🔴 **Red:** Downstream decline or race rejection

### 2. Idempotency & Concurrency Proof Panel (Hero Banner)
- **Live Counters:**
  - `Race Conditions Defended`
  - `Row Locks Acquired`
  - `Total Sagas Driven`
  - `Double-Booking Prevention: 100.0%`
- **One-Click Demo Triggers:**
  - **⚡ Happy Path:** Full saga commits (`STARTED -> INVENTORY_RESERVED -> PAYMENT_PROCESSING -> COMPLETED`).
  - **🔄 Payment Decline & Compensate:** Triggers downstream failure and watches automated forward undo (`COMPENSATING -> COMPENSATED`).
  - **🔥 5x Concurrent Race Attack:** Fires 5 simultaneous requests with the *exact same UUID* to prove that PostgreSQL row locks and `UNIQUE` constraints allow exactly 1 winner while safely blocking 4 duplicates.
  - **💥 Crash Recovery Simulation:** Simulates process death mid-saga and watches the Celery recovery sweep claim and reconcile the orphan.

### 3. Transaction Matrix & State Graph Stepper
- Live polling feed of all transactions from `GET /transactions/all/`.
- Visual 3-stage mini pipeline stepper showing progression.
- Filtering by `In-Flight`, `Completed`, `Compensated`, and `Failed`.

### 4. Forensic `SagaEvent` Audit Stream
- Chronological timeline table showing:
  - Sequence number (`seq`)
  - State transition (`from_status -> to_status`)
  - Actor (`request`, `recovery`, `retry`)
  - HTTP status codes & expandable JSON response payloads.

### 5. Offline Demo / Simulation Mode Fallback
- If backend services are offline or not running during a demo, toggling **"DEMO MODE"** in the top navigation activates an interactive mock engine so the visualization and stress-test buttons work anywhere, anytime.

### 6. "How This Works" 60-Second Architecture Explainer
- Click **"Architecture"** in the top header to show a modal breaking down Saga vs 2PC, Relational Idempotency, Row Locks, and Crash Recovery.
