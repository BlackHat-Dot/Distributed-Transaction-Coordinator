# Distributed Saga Coordinator (DTC)

A robust, production-grade implementation of the **Distributed Saga Pattern** (Orchestration style) across three independent microservices in Python/Django: **Coordinator Service**, **Inventory Service**, and **Payment Service**.

---

## 1. What This Project Is

### The Core Problem: Why Multi-Service Transactions Are Hard
In a traditional monolithic application with a single relational database, ensuring that multiple steps either all succeed or all fail is straightforward:

```python
# Monolith: Single ACID transaction
with transaction.atomic():
    inventory.reserve(product_id, quantity)  # Step 1
    payment.charge(user_id, amount)          # Step 2
    order.create(order_id)                   # Step 3
    # If payment fails, database rollback restores inventory automatically.
```

In a microservices architecture, this is **impossible**:
- The **Inventory Service** has its own PostgreSQL database.
- The **Payment Service** has its own PostgreSQL database (or an external gateway like Stripe).
- The **Coordinator Service** has its own PostgreSQL database.

There is no shared database transaction across network boundaries. If the Coordinator reserves inventory in Service A and then the payment fails in Service B, the database for Service A has *already committed* the inventory reservation. A standard SQL `ROLLBACK` cannot undo a committed transaction in another service's database across the network.

### The Solution: The Saga Pattern
A **Saga** is an architectural pattern that coordinates distributed transactions across multiple independent services through a sequence of local transactions.

Instead of one global distributed lock (such as Two-Phase Commit / 2PC, which is notoriously slow, brittle, and introduces tight availability coupling), each step commits locally. If a subsequent step fails, the Saga executes **compensating transactions** (forward undo actions) to reverse previous successes:

$$\text{Action: Reserve Inventory} \longrightarrow \text{Action: Charge Payment (FAILS)} \longrightarrow \text{Compensate: Release Inventory}$$

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
        HTTP POST /products/reserve/          HTTP POST /payments/charge/
        HTTP POST /products/release/          HTTP GET  /payments/check/
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

### 2. Configure Database & Environment Variables
Create a PostgreSQL database for each service (or configure your `.env` files in each service directory):
- `dtc_coordinator`
- `dtc_inventory`
- `dtc_payment`

### 3. Run Migrations for All Services

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

### 4. Start the Services

Open 3 separate terminals to run each service on its designated port:

```bash
# Terminal 1: Inventory Service (Port 8000)
cd inventory-service
python manage.py runserver 127.0.0.1:8000

# Terminal 2: Payment Service (Port 8001)
cd payment-service
python manage.py runserver 127.0.0.1:8001

# Terminal 3: Coordinator Service (Port 8002)
cd coordinator-service
python manage.py runserver 127.0.0.1:8002
```

### 5. Start Background Crash Recovery (Celery)

```bash
# In coordinator-service directory:
# Start Celery Worker
celery -A coordinator_service worker -l info

# Start Celery Beat (Schedules recovery sweep every 90 seconds)
celery -A coordinator_service beat -l info
```

### 6. Execute a Test Transaction

```bash
# Run a successful transaction (Quantity 1, Amount 500)
curl -X POST http://127.0.0.1:8002/transactions/ \
  -H "Content-Type: application/json" \
  -d '{"product_id": 1, "quantity": 1, "amount": 500}'

# Inspect the append-only Saga Event Log
curl http://127.0.0.1:8002/transactions/<TRANSACTION_ID>/events/
```

### 7. Run the Full Test Suite

```bash
cd coordinator-service
python manage.py test coordinator --keepdb
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
  │                       │── POST /products/reserve/─>                      │
  │                       │<── 200 OK (Reserved)────│                        │
  │   status: INVENTORY_RESERVED                    │                        │
  │   status: PAYMENT_PROCESSING                    │                        │
  │                       │                                                  │
  │                       │── POST /payments/charge/────────────────────────>│
  │                       │<── 200 OK (Charged)──────────────────────────────│
  │   status: COMPLETED   │                                                  │
  │<── 200 OK (Completed)─│
```

1. **Client Request:** Calls `POST /transactions/` on Coordinator (`create_transaction` in `coordinator/views.py`).
2. **Idempotent Record Creation:** `_create_transaction_idempotent()` atomically inserts a `Transaction` row with `status=STARTED`.
3. **Inventory Reservation:** Coordinator sends `POST /products/reserve/` (`reserve_product` in `inventory/views.py`). Inventory locks the product row via `SELECT FOR UPDATE`, decrements stock, creates `Reservation(status=RESERVED)`, and returns HTTP 200.
4. **State Transition:** Coordinator transitions `STARTED` $\to$ `INVENTORY_RESERVED` $\to$ `PAYMENT_PROCESSING`.
5. **Payment Charge:** Coordinator sends `POST /payments/charge/` (`charge_payment` in `payments/views.py`). Payment service inserts `Payment(status=CHARGED)` and returns HTTP 200.
6. **Completion:** Coordinator transitions `PAYMENT_PROCESSING` $\to$ `COMPLETED` and returns the serialized transaction to the client.

---

### Scenario B: Compensating Path (Payment Declines / Fails)

```
Client               Coordinator             Inventory (:8000)        Payment (:8001)
  │                       │                         │                        │
  │── POST /transactions/─>                         │                        │
  │   status: STARTED     │                         │                        │
  │                       │── POST /products/reserve/─>                      │
  │                       │<── 200 OK (Reserved)────│                        │
  │   status: PAYMENT_PROCESSING                    │                        │
  │                       │── POST /payments/charge/────────────────────────>│
  │                       │<── 400 Declined / 500 Error──────────────────────│
  │   status: COMPENSATING│                                                  │
  │                       │── POST /products/release/─>                      │
  │                       │<── 200 OK (Restored)────│                        │
  │   status: COMPENSATED │
  │<── 400/500 Error──────│
```

1. **Payment Fails:** Payment service returns non-200 or connection error.
2. **Compensation Trigger:** `_charge_payment()` invokes `_compensate()`.
3. **Transition to Compensating:** Coordinator sets `status=COMPENSATING`.
4. **Inventory Release:** Coordinator dispatches `POST /products/release/` (`release_product` in `inventory/views.py`).
5. **Stock Restored:** Inventory locks the `Reservation` row via `SELECT FOR UPDATE`, checks that status is `RESERVED`, restores `product.stock += quantity`, sets `Reservation.status=RELEASED`, and returns HTTP 200.
6. **Compensated State:** Coordinator transitions `COMPENSATING` $\to$ `COMPENSATED`.

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

The test suite covers unit logic, DB constraints, multi-threaded concurrency races, and crash recovery.

### Test Breakdown (33 Tests in Coordinator Service)

| Module | Test Class | Tests | What It Proves |
|---|---|:---:|---|
| `test_saga_log.py` | `SagaEventModelConstraintTests` | 4 | DB uniqueness on `(transaction, seq)`, endpoint formatting. |
| `test_saga_log.py` | `SetStatusWithEventHappyPathTests` | 2 | Atomic status updates, monotonic `seq` increments. |
| `test_saga_log.py` | `SetStatusWithEventCasMissTests` | 3 | CAS rejection on mismatched preconditions. |
| `test_saga_log.py` | `SetStatusWithEventConcurrencyTests` | 1 | Multi-threaded lock contention: exactly 1 winner, zero sequence collisions. |
| `test_retry_compensation_race.py` | `RetryCompensationRaceNewCodeTests` | 1 | Live thread race resolution in `retry_compensation`. |
| `test_retry_compensation_race.py` | `RetryCompensationRaceOldCodeTests` | 1 | Verifies old code lack of audit trail. |
| `test_recovery_race.py` | `RecoveryAndRetryCompensationRaceTests` | 1 | Inter-process race between recovery sweep and client retry. |
| `test_recovery_race.py` | `RecoveryClaimTests` | 5 | Event creation in `_claim()`, concurrent claims, `COMPENSATING` & `COMPENSATION_FAILED` recovery sweeps. |
| `test_crash_recovery.py` | `CrashRecoveryTests` | 2 | Genuine process death simulation during `PAYMENT_PROCESSING` covering both downstream payment outcomes. |
| `tests.py` | Core Coordinator Tests | 13 | Idempotent transaction creation, terminal state guards, sequential duplicate prevention. |

### Running the Tests

```bash
cd coordinator-service
python manage.py test coordinator --keepdb
```

```
Ran 33 tests in 3.326s
OK
```
