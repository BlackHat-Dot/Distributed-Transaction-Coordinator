# Distributed Saga Coordinator — Dev Control Panel

A lightweight, zero-dependency developer testing UI to interact with the Distributed Saga Coordinator and visually inspect the append-only `SagaEvent` audit trail.

---

## How to Open / Serve

You can run this UI in any of the following ways:

### Option 1: Serve with Python HTTP Server (Recommended)
```bash
cd frontend
python -m http.server 3000
```
Open **http://localhost:3000** in your browser.

### Option 2: Open Directly in Browser
Double-click `index.html` or open `file:///.../DTC/frontend/index.html` in your browser.

---

## Coordinator URL Configuration

The frontend communicates with the Coordinator Service at:
- **Default Base URL:** `http://127.0.0.1:8002`

You can change this URL in two ways:
1. **Via the UI:** Edit the "Coordinator URL" input at the top header and click **Save**. This persists in `localStorage`.
2. **Via Code:** Edit the single `DEFAULT_COORDINATOR_URL` constant at the top of `app.js`.

---

## Features

1. **Create Transaction (Start Saga):**
   - Form for `product_id`, `quantity`, `amount`, and optional custom `transaction_id`.
   - Simulation checkboxes (`fail_compensation`, `coordinator` crash simulation).
   - Automatically inspects the newly created transaction upon completion.

2. **Transaction Status & Saga Event Audit Log:**
   - Visual inspection of `Transaction` metadata (`status`, `product_id`, `quantity`, `amount`, `updated_at`).
   - Ordered timeline table of all `SagaEvent` rows (`seq`, `from_status -> to_status`, `actor`, `http_status_code`, timestamp).
   - Inline JSON viewer for downstream HTTP response payloads.
   - **Auto-refresh toggle (2s)** to watch events append in real-time as sagas execute.

3. **Retry Compensation:**
   - Execute `POST /transactions/<id>/retry-compensation/` and observe real-time audit updates.

4. **Recent Transactions Explorer:**
   - Quick-picker table loaded from `GET /transactions/all/`. Click **Inspect** on any row to load it into the audit viewer.
