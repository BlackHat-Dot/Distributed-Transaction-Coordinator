# Distributed Saga Coordinator (DTC)

A robust, production-grade implementation of the **Distributed Saga Pattern** (Orchestration style) across three independent microservices in Python/Django: **Coordinator Service**, **Inventory Service**, and **Payment Service**.

---

## 1. What This Project Is

### The Core Problem: Why Multi-Service Transactions Are Hard
In a traditional monolithic application with a single relational database, ensuring that multiple steps either all succeed or all fail is straightforward:

```python
# Monolith: Single ACID transaction
with transaction.atomic():
    seats.reserve(seat_ids, user_id)         # Step 1
    payment.charge(user_id, amount)          # Step 2
    order.create(order_id)                   # Step 3
    # If payment fails, database rollback restores seat availability automatically.
```

In a microservices architecture, this is **impossible**:
- The **Inventory Service** has its own PostgreSQL database.
- The **Payment Service** has its own PostgreSQL database (or an external gateway like Stripe).
- The **Coordinator Service** has its own PostgreSQL database.

There is no shared database transaction across network boundaries. If the Coordinator reserves inventory in Service A and then the payment fails in Service B, the database for Service A has *already committed* the inventory reservation. A standard SQL `ROLLBACK` cannot undo a committed transaction in another service's database across the network.

### The Solution: The Saga Pattern
A **Saga** is an architectural pattern that coordinates distributed transactions across multiple independent services through a sequence of local transactions.

Instead of one global distributed lock (such as Two-Phase Commit / 2PC, which is notoriously slow, brittle, and introduces tight availability coupling), each step commits locally. If a subsequent step fails, the Saga executes **compensating transactions** (forward undo actions) to reverse previous successes:

$$\text{Action: Reserve Seats} \longrightarrow \text{Action: Charge Payment (FAILS)} \longrightarrow \text{Compensate: Release Seats}$$

---

## 2. Architecture Overview

### Service Topology

```
                  ┌──────────────────────────────────────────────────┐
                  │               CLIENT / UPSTREAM                  │
                  └─────────────────────────┬────────────────────────┘
                                            │ HTTP POST /transactions/
                                            ▼
                  ┌──────────────────────────────────────────────────┐
                  │          COORDINATOR SERVICE (:8002)             │
                  │   - Owns Saga State Machine                      │
                  │   - Append-Only SagaEvent Audit Log              │
                  │   - Celery Beat Crash-Recovery Worker            │
                  └──────────────┬────────────────────┬──────────────┘
                                 │                    │
        HTTP POST /seats/pricing/             HTTP POST /payments/charge/
        HTTP POST /seats/reserve/             HTTP GET  /payments/check/
        HTTP POST /seats/confirm/             │
        HTTP POST /seats/release/             │
        HTTP GET  /reserve/check/             │
                                 │                    ▼
                                 │       ┌───────────────────────────┐
                                 │       │  PAYMENT SERVICE (:8001)  │
                                 │       │  - PostgreSQL DB          │
                                 │       │  - Idempotent Charges     │
                                 │       │  - Status Queries         │
                                 │       └───────────────────────────┘
                                 ▼
                  ┌───────────────────────────┐
                  │ INVENTORY SERVICE (:8000) │
                  │  - PostgreSQL DB          │
                  │  - SELECT FOR UPDATE Locks│
                  │  - Idempotent Releases    │
                  └───────────────────────────┘
```

### Orchestration vs. Choreography: Why Orchestration Was Chosen

There are two primary ways to implement a Saga:

| Dimension | Orchestration (This Project) | Choreography (Event-Driven / Kafka) |
|---|---|---|
| **Control** | Central Coordinator drives every step. | Services emit and listen to events without a central coordinator. |
| **Visibility** | **Single Source of Truth:** Querying one coordinator table reveals the exact state of any saga. | Fragmented across distributed broker logs. |
| **Failure Reasoning** | Deterministic state machine with centralized timeouts and recovery. | Complex cycle detection; cascading failure modes are harder to trace. |
| **Coupling** | Services are decoupled from each other, but coupled to the coordinator API. | Services subscribe to peer domain events. |
| **Tradeoff** | The Coordinator is a single point of responsibility. | Cognitive overhead of tracking implicit event flows across services. |

**Why Orchestration was chosen here:**
1. **Centralized Forensic Visibility:** An operator can query `GET /transactions/<id>/events/` and see the complete timeline of the distributed transaction in one place.
2. **Deterministic Crash Recovery:** If a node crashes mid-flight, a centralized recovery sweep knows exactly what step was executing and can query downstream endpoints to reconcile.

### Database Topology & Blast Radius Tradeoff

In this deployment, each microservice connects to its own independent database (`coordinator_db`, `inventory_db`, `payment_db`) hosted within a single shared PostgreSQL container:

- **Logical Isolation (Enforced):** Strict database-per-service isolation is maintained at the application and schema layer. There are zero cross-database queries, zero cross-service foreign keys, and independent connection pools.
- **Physical Blast Radius Tradeoff:** Because all three databases reside on a single PostgreSQL server instance in Docker, a catastrophic crash or hardware outage of the Postgres container takes down storage for all three services simultaneously.
- **Production Architecture:** In a production deployment, this single container is replaced with three physically separate managed database instances (e.g. AWS RDS / GCP Cloud SQL) to eliminate the shared blast radius. For development and portfolio demonstration, the single container optimizes memory footprint and port management without compromising architectural schema boundaries.

---

## 3. How to Run This Project

### Prerequisites
- Python 3.11+ (tested on Python 3.13)
- PostgreSQL running locally (port `5432`)
- Redis running locally (port `6379`, for Celery lock coordination)

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/BlackHat-Dot/Distributed-Transaction-Coordinator.git
cd Distributed-Transaction-Coordinator

# Create and activate virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\Activate.ps1
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### One-Command Quick Start (Docker Compose — Recommended)

Start the entire distributed system (3 microservices, PostgreSQL with isolated databases, Redis distributed lock cache, Celery Worker, Celery Beat recovery sweeper, and Mission Control Visualizer) with a single command:

```bash
docker compose up --build
```

- **Mission Control Visualizer:** [http://localhost:3000](http://localhost:3000)
- **Coordinator Service:** [http://localhost:8002](http://localhost:8002)
- **Inventory Service:** [http://localhost:8000](http://localhost:8000)
- **Payment Service:** [http://localhost:8001](http://localhost:8001)

All database migrations and seed data are applied automatically on startup with zero manual configuration.

---

### Manual Local Setup (Without Docker)

#### 1. Configure Environment Variables
Copy `.env.example` to `.env` or set environment variables:
```bash
cp .env.example .env
```

#### 2. Run Migrations for All Services

```bash
# Terminal 1 - Inventory Service
cd inventory-service
python manage.py migrate

# Terminal 2 - Payment Service
cd payment-service
python manage.py migrate

# Terminal 3 - Coordinator Service
cd coordinator-service
python manage.py migrate
```

#### 3. Start the Services

Open 3 separate terminals to run each service on its designated port:

```bash
# Terminal 1: Inventory Service (Port 8000)
cd inventory-service
python manage.py runserver 0.0.0.0:8000

# Terminal 2: Payment Service (Port 8001)
cd payment-service
python manage.py runserver 0.0.0.0:8001

# Terminal 3: Coordinator Service (Port 8002)
cd coordinator-service
python manage.py runserver 0.0.0.0:8002
```

#### 4. Start Background Crash Recovery (Celery)

```bash
# In coordinator-service directory:
celery -A coordinator_service worker -l info
celery -A coordinator_service beat -l info
```

### 6. Execute a Test Transaction

```bash
# Run a successful transaction (Seat ID 1)
curl -X POST http://127.0.0.1:8002/transactions/ \
  -H "Content-Type: application/json" \
  -d '{"seat_ids": [1]}'

# Inspect the append-only Saga Event Log
curl http://127.0.0.1:8002/transactions/<TRANSACTION_ID>/events/
```

### 7. Run the Full Test Suite

```bash
# Run test suites across all 3 services
python coordinator-service/manage.py test coordinator --keepdb
python inventory-service/manage.py test inventory --keepdb
python payment-service/manage.py test payments --keepdb
```

---

## 4. The Saga Flow, Step by Step

### Scenario A: Happy Path (All Steps Succeed)

```
Client               Coordinator             Inventory (:8000)        Payment (:8001)
  │                       │                         │                        │
  │── POST /transactions/─>                         │                        │
  │   (ID Generated)      │                         │                        │
  │   status: STARTED     │                         │                        │
  │                       │── POST /seats/reserve/──>                        │
  │                       │<── 200 OK (Held)────────│                        │
  │   status: INVENTORY_RESERVED                    │                        │
  │   status: PAYMENT_PROCESSING                    │                        │
  │                       │                                                  │
  │                       │── POST /payments/charge/────────────────────────>│
  │                       │<── 200 OK (Charged)──────────────────────────────│
  │                       │── POST /seats/confirm/──>                        │
  │                       │<── 200 OK (Booked)──────│                        │
  │   status: COMPLETED   │                                                  │
  │<── 200 OK (Completed)─│
```

1. **Client Request:** Calls `POST /transactions/` on Coordinator (`_handle_create_transaction` in `coordinator/views.py`) with `{"seat_ids": [1]}`.
2. **Idempotent Record Creation:** `_create_transaction_idempotent()` atomically inserts a `Transaction` row with `status=STARTED`.
3. **Inventory Hold:** Coordinator sends `POST /seats/reserve/` (`reserve_seat` in `inventory/views.py`). Inventory locks the seat row via `SELECT FOR UPDATE`, sets `status=HELD` with ephemeral expiration, creates `Reservation(status=RESERVED)`, and returns HTTP 200.
4. **State Transition:** Coordinator transitions `STARTED` $\to$ `INVENTORY_RESERVED` $\to$ `PAYMENT_PROCESSING`.
5. **Payment Charge:** Coordinator sends `POST /payments/charge/` (`charge_payment` in `payments/views.py`). Payment service inserts `Payment(status=CHARGED)` and returns HTTP 200.
6. **Seat Confirmation & Completion:** Coordinator sends `POST /seats/confirm/` (`confirm_seat` in `inventory/views.py`), setting `status=BOOKED`. Coordinator transitions `PAYMENT_PROCESSING` $\to$ `COMPLETED` and returns the serialized transaction to the client.

---

### Scenario B: Compensating Path (Payment Declines / Fails)

```
Client               Coordinator             Inventory (:8000)        Payment (:8001)
  │                       │                         │                        │
  │── POST /transactions/─>                         │                        │
  │   status: STARTED     │                         │                        │
  │                       │── POST /seats/reserve/──>                        │
  │                       │<── 200 OK (Held)────────│                        │
  │   status: PAYMENT_PROCESSING                    │                        │
  │                       │── POST /payments/charge/────────────────────────>│
  │                       │<── 422 Declined / Error──────────────────────────│
  │   status: COMPENSATING│                                                  │
  │                       │── POST /seats/release/──>                        │
  │                       │<── 200 OK (Released)────│                        │
  │   status: COMPENSATED │
  │<── 400/422 Error──────│
```

1. **Payment Fails:** Payment service returns non-200 or decline status (e.g. 422).
2. **Compensation Trigger:** `_finalize_payment_outcome()` invokes `_compensate()`.
3. **Transition to Compensating:** Coordinator sets `status=COMPENSATING`.
4. **Seat Release:** Coordinator dispatches `POST /seats/release/` (`release_seat` in `inventory/views.py`).
5. **Seat Availability Restored:** Inventory locks the `Reservation` row via `SELECT FOR UPDATE`, sets `Reservation.status=RELEASED`, resets `Seat.status=AVAILABLE` with `held_by_transaction_id=None`, and returns HTTP 200.
6. **Compensated State:** Coordinator transitions `COMPENSATING` $\to$ `COMPENSATED` and returns the rollback outcome.

---

## 5. Design Decisions: Chosen Paths vs. Rejected Alternatives

### 1. Idempotency: DB UNIQUE Constraint vs. Redis Key/TTL
- **Chosen:** PostgreSQL `UNIQUE (transaction_id)` constraint caught via `django.db.IntegrityError` inside `atomic()`.
- **Considered:** Distributed Redis cache with TTL keys (e.g. `SET transaction:<id> 1 NX EX 3600`).
- **Why Redis TTL was rejected:** In-memory TTL caches treat idempotency as an expiration policy. If a network partition or heavy queue delay causes a retry to arrive 3,601 seconds later, an expired TTL allows a duplicate charge or double-reservation. Relational `UNIQUE` constraints provide permanent, crash-safe, ACID-level idempotency guarantees.

### 2. Concurrency: Conditional State Transitions (CAS) vs. Optimistic Version Columns
- **Chosen:** Compare-And-Set (CAS) queries via `UPDATE transactions SET status = to_status WHERE transaction_id = id AND status = expected_status` under row locks.
- **Considered:** Optimistic concurrency control using an integer `version` column (`WHERE version = current_version`).
- **Why Versioning was rejected:** Version numbers add indirection without semantic meaning. In a finite state machine, the *current status itself* is the exact precondition. Checking `WHERE status = 'PAYMENT_PROCESSING'` directly asserts the valid state-machine edge, avoiding race conditions without managing artificial version numbers.

### 3. Inventory Locking: PostgreSQL `SELECT FOR UPDATE` vs. Distributed Redis Lock
- **Chosen:** Database row locking (`Reservation.objects.select_for_update()`).
- **Considered:** Redlock / Redis-based distributed mutex locks.
- **Why Redis Distributed Locks were rejected:** Distributed locks over network storage (like Redis) suffer from clock drift, lease expiration before long GC pauses, and split-brain risks. Because Inventory state lives in PostgreSQL, acquiring an exclusive row lock at the SQL engine level guarantees that stock increment/decrement occurs strictly inside the same ACID transaction as the reservation record change.

### 4. Crash Recovery: Polling Background Sweep vs. Transactional Outbox Pattern
- **Chosen:** Periodic Celery Beat worker polling stale non-terminal records (`updated_at < threshold`) combined with downstream status probes.
- **Considered:** The Transactional Outbox Pattern (writing pending HTTP requests to an `Outbox` table, relayed by a Debezium / CDC worker).
- **Why Transactional Outbox was rejected for this scope:** An Outbox queue introduces a second source of truth that requires distributed FIFO ordering guarantees per transaction ID across relay workers. Since downstream services already expose idempotent probe endpoints (`/payments/check/`, `/reserve/check/`), the polling sweep directly queries ground truth without the infrastructure overhead of message brokers and CDC pipelines.

### 5. Audit Trail: Dual Materialized Status + `SagaEvent` vs. Full Event Sourcing
- **Chosen:** Materialized `Transaction.status` column updated atomically alongside an immutable `SagaEvent` append-only audit row.
- **Considered:** Pure Event Sourcing (removing `status` entirely and computing current state dynamically from `SagaEvent` stream on every read).
- **Why Pure Event Sourcing was rejected:** Full event sourcing makes bulk queries (`Transaction.objects.filter(status=STARTED)`) and dashboard queries slow and complex, requiring table joins and subqueries. Maintaining the materialized `status` column under the same `SELECT FOR UPDATE` transaction provides $O(1)$ read performance with complete forensic auditability.

### 6. Lease State: Universal `RECOVERY_IN_PROGRESS` vs. Sub-Status Leases
- **Chosen:** Reusing `RECOVERY_IN_PROGRESS` as the universal claim-lease state across all recovery sweeps (`STARTED`, `INVENTORY_RESERVED`, `PAYMENT_PROCESSING`, `COMPENSATING`, `COMPENSATION_FAILED`).
- **Considered:** Introducing fine-grained lease states (`RECOVERY_COMPENSATING_CLAIMED`, `RECOVERY_STARTED_CLAIMED`, etc.).
- **Why fine-grained leases were rejected:** It needlessly multiplies state enum choices. `RECOVERY_IN_PROGRESS` communicates one unambiguous invariant: *a background worker has claimed exclusive execution rights on this row*.

### 7. Event Logging: Initial `_claim()` vs. Heartbeat Lease Renewal
- **Chosen:** `_claim()` logs a `SagaEvent` row (`from_status -> RECOVERY_IN_PROGRESS`), but `recover_in_progress()` re-claims via raw `.update()` without creating an event.
- **The Distinction:**
  - **`_claim()` is a State Graph Edge:** It represents the moment an abandoned transaction enters recovery. If downstream probes fail and the transaction is left in progress, the log preserves when recovery started.
  - **`recover_in_progress()` is a Lease Renewal (Heartbeat):** Touching `updated_at` on an already-claimed transaction renews a soft timeout. Logging an event every 5 minutes during a long downstream outage would cause unbounded table bloat.

### 8. Failure Queue: Auto-Retrying `COMPENSATION_FAILED` vs. Dead-Letter Queue
- **Chosen:** Automated re-claim and retry of `COMPENSATION_FAILED` transactions on a 5-minute backoff sweep.
- **Considered:** Immediate terminal dead-letter queue requiring manual operator review.
- **Why Auto-Retry was chosen:** Transient downstream network timeouts are far more common than permanent logic bugs. Auto-retrying every 5 minutes automatically recovers inventory reservations once network connectivity returns.

---

## 6. Bugs Found and Fixed (The Engineering Investigation)

### 1. The `retry_compensation` Unconditional Overwrite Bug
- **Suspicion:** A Time-of-Check to Time-of-Use (TOCTOU) race where `txn.status` was read before network I/O and used as a CAS filter after network I/O.
- **Code Verification:** In `coordinator/views.py`, the network exception handler called:
  ```python
  except (Timeout, ConnectionError) as e:
      _set_status(transaction_id, Transaction.Status.RETRY_COMPENSATION_FAILED)  # NO expected_status guard!
  ```
- **Real Bug Confirmed:** If a client retried compensation while a recovery worker was actively resolving it, a network timeout during the retry would execute an unconditioned write, overwriting a valid `COMPENSATED` status with `RETRY_COMPENSATION_FAILED`.
- **Fix:** Switched all write paths in `retry_compensation` to `_set_status_with_event(expected_status=txn_status_at_entry)`. If the row changed mid-flight, the write is safely rejected.

### 2. Recovery vs. Live Retry Concurrent Compensation Race
- **Suspicion:** If the background recovery sweep and a live client `retry_compensation` call race on the same transaction, duplicate inventory release calls could cause stock inflation.
- **Investigation & Test Evidence:** Tested via `coordinator/test_recovery_race.py`. Under PostgreSQL `READ COMMITTED`, the first committer won the CAS; the second committer matched 0 rows. In the Inventory service, `SELECT FOR UPDATE` on `Reservation` prevented duplicate stock restoration.
- **Honesty Note:** **Benign Race / No State Corruption.** Stock and status integrity were already protected by PostgreSQL row locks. The real bug was that the recovery command issued raw `.update()` calls with zero audit logging.

### 3. Untracked Initial `_claim()` Transition
- **Suspicion:** When recovery sweeps claimed a stuck transaction (`INVENTORY_RESERVED -> RECOVERY_IN_PROGRESS`), it used raw `.update()`.
- **Real Observability Gap Confirmed:** If recovery claimed a transaction and crashed before resolving it, the transaction was left in `RECOVERY_IN_PROGRESS` with zero entries in `SagaEvent`.
- **Fix:** Replaced raw `.update()` in `_claim()` with `_set_status_with_event(to_status=RECOVERY_IN_PROGRESS, expected_status=from_status, actor="recovery")`.

### 4. Permanently-Stuck `COMPENSATING` / `COMPENSATION_FAILED` Sweep Gap
- **Suspicion:** `Command.handle()` in `recovery_stuck_transactions.py` never swept `COMPENSATING` or `COMPENSATION_FAILED`.
- **Real Bug Confirmed:** An isolated test proved that transactions in `COMPENSATING` or `COMPENSATION_FAILED` were completely ignored by background recovery sweeps. If a coordinator crashed mid-compensation, the transaction was permanently orphaned.
- **Fix:** Added `recover_compensating(threshold)` and `recover_compensation_failed(threshold)` to `Command.handle()`, claiming into `RECOVERY_IN_PROGRESS` and re-executing `_compensate()`.

### 5. The Mid-Flight `PAYMENT_PROCESSING` Crash Recovery
- **Suspicion:** If a coordinator dies during payment processing, recovery must handle two opposite downstream truths without human intervention.
- **Test Evidence (`coordinator/test_crash_recovery.py`):**
  - **Ambiguity Case 1 (Charged):** Coordinator dies during charge $\to$ Recovery queries `/payments/check/` $\to$ sees `CHARGED` $\to$ rolls forward to `COMPLETED`.
  - **Ambiguity Case 2 (Not Charged):** Coordinator dies before charge $\to$ Recovery queries `/payments/check/` $\to$ sees `NOT_CHARGED` $\to$ queries `/reserve/check/` $\to$ releases inventory $\to$ marks `COMPENSATED`.

---

## 7. Known Tradeoffs Not Fixed

1. **Asymmetric Pre-Claim in `retry_compensation`:**
   `Command._compensate()` marks `COMPENSATING` *before* calling Inventory. `retry_compensation` calls Inventory *before* writing status.
   - *Tradeoff:* During a race, both actors dispatch network requests to Inventory.
   - *Why safe:* Inventory's `release_product` is idempotent via row locking.
   - *Why kept:* Adding an intermediate state to the client request path adds complex error handling for transient client retries without adding correctness benefits.

2. **Uncapped Retries on `COMPENSATION_FAILED`:**
   `recover_compensation_failed` retries stale failed compensations indefinitely every 5 minutes.
   - *Production Alternative:* A max-retry counter (`retry_count >= 5` $\to$ `DEAD_LETTER` with PagerDuty alerts). Kept out of scope to avoid unnecessary schema complexity.

---

## 8. What Was Deliberately Not Built & Why

- **Transactional Outbox / Debezium:** Unnecessary architectural complexity for direct synchronous HTTP coordination with status probing.
- **Apache Kafka / RabbitMQ:** Adds eventual consistency lag and complex distributed debugging overhead where direct orchestration is preferred.
- **CQRS Read Replicas:** Read load at current scale does not justify separate read/write database schemas.
- **Circuit Breakers (e.g. Netflix Hystrix / Resilience4j):** Standard timeout classification (`_transport_status`) combined with periodic background recovery handles transient network partition cleanly.
- **Distributed Tracing (OpenTelemetry):** Audit logging via `SagaEvent` provides complete transaction-scoped causality without requiring external collector infrastructure (Jaeger/Zipkin).
- **Full Event Sourcing:** Materialized column + audit table provides identical auditability with vastly superior query ergonomics and $O(1)$ indexed lookups.

---

## 9. Test Suite Overview

The test suite covers unit logic, database constraints, multi-threaded concurrency races, distributed crash recovery, idempotent replay, and security guards across all three microservices.

### Test Breakdown (145 Tests Total)

| Service | Test Module / Focus | Tests | What It Proves |
|---|---|:---:|---|
| **Coordinator** | `test_coordinator.py` | 14 | Idempotent transaction creation, terminal state guards, strict payload allowlist rejection. |
| **Coordinator** | `test_saga_log.py` | 10 | DB uniqueness on `(transaction, seq)`, monotonic increments, atomic CAS status updates under lock contention. |
| **Coordinator** | `test_correlation_id.py` | 3 | End-to-end `X-Correlation-ID` header preservation and propagation across all downstream HTTP calls. |
| **Coordinator** | `test_crash_recovery.py` | 2 | Genuine process crash recovery during `PAYMENT_PROCESSING` covering forward completion and compensating rollback. |
| **Coordinator** | `test_forward_saga_events.py` | 8 | Complete forward and rollback SagaEvent append-only audit trail verification. |
| **Coordinator** | `test_nonblocking_saga.py` | 2 | Asynchronous 202 Accepted worker dispatch vs. synchronous 200 OK execution pipelines. |
| **Coordinator** | `test_recovery_race.py` | 6 | Concurrent claims, `COMPENSATING` and `COMPENSATION_FAILED` recovery sweeps, recovery vs live client retry races. |
| **Coordinator** | `test_retry_compensation_race.py` | 2 | Thread race resolution and CAS guards during live client compensation retries. |
| **Coordinator** | `test_auth_otp.py` & `test_single_device_auth.py` | 21 | Passwordless OTP lifecycle, single-device session invalidation, JWT token verification. |
| **Coordinator** | `test_celery_task.py` | 2 | Celery distributed Redis mutex locking and task invocation. |
| **Inventory** | `test_inventory.py` | 45 | Row locking (`SELECT FOR UPDATE`), seat hold expiration, `POST /seats/pricing/`, quota limits, internal key security. |
| **Inventory** | `test_session_hold_clearance.py` | 2 | Presence-based hold abandonment (dead-man's switch) when user session disconnects. |
| **Payment** | `test_payments.py` | 28 | Idempotent charge capture, simulated decline thresholds, payment status verification, refunds, correlation ID. |

### Running the Full Test Suite

```bash
# Terminal 1 - Inventory Service
python inventory-service/manage.py test inventory --noinput

# Terminal 2 - Coordinator Service
python coordinator-service/manage.py test coordinator --noinput

# Terminal 3 - Payment Service
python payment-service/manage.py test payments --noinput
```

```
--- INVENTORY:   Ran 47 tests  -> OK
--- COORDINATOR: Ran 70 tests  -> OK
--- PAYMENT:     Ran 28 tests  -> OK
Total: 145 tests passed (100% success)
```

---

## 10. E-Ticket Issuance, QR Verification & Gate Check-In

### Overview
The DTC platform issues cryptographic single-use electronic tickets for completed saga transactions. Tickets are generated lazily and idempotently per seat booking, complete with scannable QR codes, pure-Python PDF rendering (ReportLab), iCalendar (.ics) event sync, and a high-concurrency gate check-in mechanism.

### Service Endpoints

| Method | Endpoint | Auth Required | Description |
|---|---|:---:|---|
| `GET` | `/tickets/<uuid:transaction_id>/` | Bearer JWT (Owner) | Returns ticket details, seat labels, event metadata, and base64 PNG QR code for frontend modal display. |
| `GET` | `/tickets/<uuid:transaction_id>/pdf/` | Bearer JWT (Owner) | Downloads a portrait A5 PDF ticket (`application/pdf`) with scannable QR code and Asia/Kolkata timezone formatting. Multi-seat bookings generate multi-page documents (one page per seat). |
| `GET` | `/tickets/<uuid:transaction_id>/calendar.ics` | Bearer JWT (Owner) | Generates and downloads a standard iCalendar event with UTC `DTSTART` and venue details. |
| `POST` | `/tickets/<uuid:transaction_id>/email/` | Bearer JWT (Owner) | Dispatches a Celery background task to send the ticket PDF as an email attachment. Sends exclusively to the owner's registered email; accepts no external recipient override. Rate limited to 3/hour per transaction. |
| `GET` | `/verify/<str:ticket_code>/` | Public | Public read-only verification page/API. Returns `VALID`, `ALREADY USED`, or `INVALID` along with event name and seat. Zero PII: strictly omits email, user ID, and transaction UUID. Rate limited. |
| `POST` | `/gate/redeem/` | `X-Gate-Api-Key` | Atomically marks a ticket as redeemed using PostgreSQL `SELECT FOR UPDATE` row locks. Refuses redemption if transaction is not `COMPLETED`. Idempotent: first scan returns 200, repeated scans return 409 `ALREADY_USED`. |

### Security & Architecture Rules
1. **Ownership & Auth:**
   - Ticket endpoints strictly reject `?token=` query parameters (`HTTP 401`). Authentication is accepted only via `Authorization: Bearer <jwt>`.
   - Accessing a transaction belonging to another user returns `HTTP 404 Not Found` (preventing transaction existence enumeration).
2. **Ground Truth Validation Before Atomic Lock:**
   - Tickets are issued only when transaction status is `COMPLETED` and inventory ground truth confirms the seat is `BOOKED` specifically for the requested `transaction_id`.
   - The HTTP ground-truth query to Inventory Service is executed **before** opening a database transaction to prevent holding DB locks across network boundaries.
3. **Gate Security:**
   - Gate scanner requests must present `X-Gate-Api-Key` matching `GATE_API_KEY`. Verification uses constant-time `hmac.compare_digest`.
4. **Concurrency & Idempotency:**
   - Simultaneous issuance requests on the same transaction catch database unique constraints on `(transaction_id, seat_id)` and idempotently return the existing ticket.
   - Concurrent gate redemption attempts under high load serialize via `SELECT FOR UPDATE`, guaranteeing exactly 1 redemption succeeds while all concurrent competitors receive 409.

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://localhost:8002` (dev/test only) | Base URL encoded into the QR code (`<PUBLIC_BASE_URL>/verify/<ticket_code>/`). **Mandatory in production (`DEBUG=False`)**: raises `ImproperlyConfigured` if unset. Must be reachable from mobile phones and client devices scanning ticket QR codes at the gate. |
| `GATE_API_KEY` | `gate-secret-key-change-in-production` | Shared secret key required for gate staff ticket redemption at `POST /gate/redeem/`. |
| `RESEND_API_KEY` | *(empty)* | Resend API key for emailing ticket PDF attachments via Celery. |

### Current Limitations
- **Apple & Google Wallet:** Native `.pkpass` and Google Wallet Pass integrations are not yet implemented; tickets are currently provided as downloadable PDFs, iCalendar (.ics) files, and scannable web verification badges.

