# Saga Event Log & Concurrency Hardening: Investigation & Implementation Record

This document records the architectural hardening of the Distributed Saga Coordinator codebase. It details what was investigated, what was broken vs. what was already protected by underlying storage guarantees, what was fixed, and what was deliberately left out.

---

## 1. The Append-Only Saga Event Log (`SagaEvent`)

### Claim / Suspicion
The `Transaction` model previously relied on a single mutable `status` column that was overwritten on every transition. As a result, once a transaction reached a terminal or failure state (such as `COMPENSATION_FAILED`), all historical progression—intermediate states, timestamps, actor identity, and downstream HTTP response bodies—was permanently erased. Additionally, during concurrent execution, there was no forensic mechanism to verify which actor executed a state change or resolve write conflicts.

### Verification
In `coordinator/models.py`, `Transaction` had only:
```python
status = models.CharField(max_length=40, choices=Status.choices)
updated_at = models.DateTimeField(auto_now=True)
```
Every `_set_status()` call executed an `UPDATE coordinator_transaction SET status = %s, updated_at = %s WHERE ...`, destroying the prior state. When inspecting transactions that failed compensation or timed out during payment, operators had zero visibility into whether the failure stemmed from a 500 error, network timeout, or downstream validation error.

### Evidence
Unit and model constraint tests (`coordinator/test_saga_log.py`):
- `test_duplicate_seq_raises_integrity_error`: Confirmed `unique_together = [("transaction", "seq")]` triggers database-level `IntegrityError` upon duplicate sequence insertion.
- `test_happy_path_status_updated_event_created`: Proved a single transition appends a `SagaEvent` record (`seq=1`) capturing `from_status`, `to_status`, `actor`, `http_status_code`, and `http_payload`.
- `test_sequential_calls_produce_incrementing_seq`: Proved sequential transitions produce ordered, monotonically increasing sequence numbers (`seq=1`, `seq=2`).

### Fix
1. Created the `SagaEvent` model in `coordinator/models.py`:
   - `transaction`: Foreign key to `Transaction` (`related_name="events"`).
   - `seq`: 1-based monotonically increasing integer per transaction.
   - `from_status` / `to_status`: State transition bounds.
   - `actor`: Originating process (`"request"`, `"recovery"`, `"retry"`).
   - `http_status_code` / `http_payload`: Downstream service response details.
   - `created_at`: Immutable timestamp.
   - DB constraint: `unique_together = [("transaction", "seq")]`.
2. Applied migration `0007_saga_event_model.py`.
3. Added the atomic CAS + append helper `_set_status_with_event()` in `coordinator/saga_log.py`.
4. Exposed read-only audit endpoint `GET /transactions/<uuid:transaction_id>/events/` (`list_transaction_events`).

### Honesty Note
**Real gap.** The mutable single-column model was not causing silent database corruption on its own, but it made distributed failure diagnosis impossible. Overwriting `status` in place destroyed all forensic evidence required to audit stalled or failed distributed sagas.

---

## 2. The `retry_compensation` TOCTOU & Unconditional Overwrite Fix

### Claim / Suspicion
In `coordinator/views.py`, `retry_compensation` was suspected of having a Time-of-Check to Time-of-Use (TOCTOU) vulnerability due to reading `txn.status` before network I/O and using that stale snapshot as a filter after network I/O. Furthermore, the network exception path was suspected of executing an unconditioned overwrite.

### Verification
Inspecting the original `retry_compensation` revealed two distinct issues:

```python
# 1. Stale snapshot captured before network I/O (up to 5s window)
txn = Transaction.objects.get(transaction_id=transaction_id)
...
release_response = requests.post(INVENTORY_RELEASE_URL, ...)
...
# 2. Network exception path: unconditioned status write
except (Timeout, ConnectionError) as e:
    _set_status(transaction_id, Transaction.Status.RETRY_COMPENSATION_FAILED) # No expected_status guard!
...
# 3. Success path: stale txn.status snapshot passed to filter
updated = Transaction.objects.filter(
    transaction_id=transaction_id,
    status=txn.status,
).update(status=Transaction.Status.COMPENSATED_ON_RETRY, ...)
```

1. **Unconditional overwrite on network error (Real Bug):** If Inventory timed out or failed to connect during a retry attempt, `_set_status(..., RETRY_COMPENSATION_FAILED)` was invoked without `expected_status`. If a concurrent recovery worker had already resolved the transaction to `COMPENSATED`, the retry error handler would blindly overwrite `COMPENSATED` with `RETRY_COMPENSATION_FAILED`.
2. **Success path CAS:** On success, `filter(status=txn.status).update(...)` used the pre-network snapshot.

### Evidence
In `coordinator/test_retry_compensation_race.py`:
- `RetryCompensationRaceOldCodeTests.test_old_code_no_audit_trail`: Running concurrent callers against the old filter+update logic showed that under PostgreSQL `READ COMMITTED`, the second committer updated 0 rows. However, zero `SagaEvent` records were written.
- `RetryCompensationRaceNewCodeTests.test_concurrent_retry_compensation_exactly_one_winner`: Running two concurrent threads through `retry_compensation` with an injected delay on `requests.post`:
  - Exactly one thread transitioned status to `COMPENSATED_ON_RETRY` and returned 200.
  - The racing thread saw `_set_status_with_event` return `False` under row-lock CAS, returning `"Transaction was concurrently resolved by another process"`.
  - Exactly one `SagaEvent` row was created (`seq=1`).

### Fix
Refactored `retry_compensation` in `coordinator/views.py`:
- Replaced the unguarded exception path write with `_set_status_with_event(..., expected_status=txn_status_at_entry, actor="retry")`. If the status changed while network I/O was pending, the failure write is safely rejected.
- Replaced the success and 500 status writes with `_set_status_with_event(..., expected_status=txn_status_at_entry, actor="retry")`.
- Handled `transitioned is False` explicitly by reloading the updated row and returning the concurrent resolution response.

### Honesty Note
**Mixed (Real bug on error path; benign on success path).**
- The network exception path was a **real bug**: calling `_set_status()` without `expected_status` meant a network error on retry could overwrite a valid `COMPENSATED` status committed by a concurrent recovery sweep.
- On the success path, PostgreSQL's `READ COMMITTED` row-level locking on `UPDATE ... WHERE status = ...` already prevented double-updates in practice. The improvement on the success path was adding row-locked atomic event creation and explicit CAS failure detection.

---

## 3. Recovery Sweep vs. Live Retry Concurrent Compensation

### Claim / Suspicion
When a transaction is claimed by the background recovery sweep (`_compensate()`) at the exact moment a client initiates a manual compensation (`retry_compensation`), both processes dispatch `requests.post(INVENTORY_RELEASE_URL, ...)`. We investigated whether this inter-process race could cause double-release of inventory stock or inconsistent coordinator state.

### Verification
1. **Coordinator code:** `_compensate()` in `recovery_stuck_transactions.py` and `retry_compensation` in `views.py` both execute HTTP `POST` to Inventory's release endpoint.
2. **Inventory code:** In `inventory-service/inventory/views.py`, `release_product` uses `Reservation.objects.select_for_update()`:
```python
with transaction.atomic():
    reservation = get_object_or_404(
        Reservation.objects.select_for_update(),
        transaction_id=transaction_id,
    )
    if reservation.status == Reservation.Status.RELEASED:
        return Response({"message": "Reservation already released", ...})

    product = Product.objects.select_for_update().get(id=reservation.product_id)
    product.stock += reservation.quantity
    product.save()

    reservation.status = Reservation.Status.RELEASED
    reservation.save()
```

### Evidence
In `coordinator/test_recovery_race.py` (`RecoveryAndRetryCompensationRaceTests.test_concurrent_recovery_compensate_and_retry_compensation`):
- Two threads executed `Command._compensate()` and `APIClient.post(retry_url)` concurrently with simulated network delays.
- Inventory release endpoint received 2 HTTP calls.
- Simulated inventory stock was restored exactly once (stock went from 8 to 10; did not increment to 12).
- Exactly 2 events were recorded in `SagaEvent`:
  1. `seq=1`: `RECOVERY_IN_PROGRESS` $\to$ `COMPENSATING` (`actor="recovery"`).
  2. `seq=2`: `COMPENSATING` $\to$ `COMPENSATED` (`actor="recovery"`) or `COMPENSATED_ON_RETRY` (`actor="retry"`).
- Final `Transaction.status` remained consistent in a single terminal state.

### Fix
Wired `_set_status_with_event` into `Command._compensate()` and `Command._resolve_transaction()` in `coordinator/management/commands/recovery_stuck_transactions.py`.

### Honesty Note
**Benign race / No corruption bug.**
Double-release was already prevented by the Inventory service's row lock (`SELECT FOR UPDATE` on `Reservation`). Similarly, coordinator state overwrite was prevented by PostgreSQL's `UPDATE WHERE status = ...` evaluation at commit time. The actual value added here was giving the recovery worker durable audit logging (`actor="recovery"`) and capturing downstream HTTP responses in `SagaEvent`.

---

## 4. The `_claim()` Audit Logging vs. Heartbeat Re-Claim

### Claim / Suspicion
When `Command._claim()` in `recovery_stuck_transactions.py` claimed stuck transactions (`STARTED`, `INVENTORY_RESERVED`, `PAYMENT_PROCESSING`, `COMPENSATING`, `COMPENSATION_FAILED` $\to$ `RECOVERY_IN_PROGRESS`), it used a raw `.update()` query, leaving the start of recovery unlogged. Furthermore, `COMPENSATING` and `COMPENSATION_FAILED` states had zero automated sweep coverage.

### Verification
In `coordinator/management/commands/recovery_stuck_transactions.py`:
- `_claim()`: Performed `Transaction.objects.filter(status=from_status, updated_at__lt=threshold).update(status=RECOVERY_IN_PROGRESS)`.
- `recover_in_progress()`: Performed `Transaction.objects.filter(status=RECOVERY_IN_PROGRESS, updated_at__lt=threshold).update(status=RECOVERY_IN_PROGRESS)`.
- `handle()`: Omitted `COMPENSATING` and `COMPENSATION_FAILED` from automated sweeps.

### Evidence
In `coordinator/test_recovery_race.py` (`RecoveryClaimTests`):
- `test_claim_creates_saga_event`: Proved that calling `_claim()` on an `INVENTORY_RESERVED` transaction creates a `SagaEvent` record (`seq=1`, `INVENTORY_RESERVED` $\to$ `RECOVERY_IN_PROGRESS`, `actor="recovery"`).
- Proved that a second sweep skips the transaction and does **not** insert a duplicate claim event.
- `test_concurrent_claim_exactly_one_winner_and_one_event`: Confirmed two concurrent recovery sweeps claiming the same transaction produce exactly one winner and one `SagaEvent`.
- `test_recover_compensating_resolves_to_compensated`: Confirmed a transaction left in `COMPENSATING` is claimed by the sweep and successfully resolved to `COMPENSATED`.
- `test_recover_compensation_failed_resolves_to_compensated`: Confirmed a transaction left in `COMPENSATION_FAILED` is claimed by the sweep and successfully retried to `COMPENSATED`.
- `test_recover_compensation_failed_downstream_reject_leaves_compensation_failed`: Confirmed that if downstream inventory release fails again during a retry, the transaction is safely returned to `COMPENSATION_FAILED` with an audit event.

### Fix
1. Updated `_claim()` in `recovery_stuck_transactions.py` to use `_set_status_with_event(transaction_id, to_status=RECOVERY_IN_PROGRESS, expected_status=from_status, actor=SagaEvent.ACTOR_RECOVERY)`.
2. Added `recover_compensating(threshold)` and `recover_compensation_failed(threshold)` methods to `Command`, calling `self._compensate(transaction_id)` once claimed into `RECOVERY_IN_PROGRESS`.
3. **Left `recover_in_progress()` untouched:** Same-to-same touch (`RECOVERY_IN_PROGRESS` $\to$ `RECOVERY_IN_PROGRESS`) remains a raw `.update(status=RECOVERY_IN_PROGRESS, updated_at=now())`.

### Reasoning on Heartbeat vs. State-Graph Edge
- **`_claim()` represents an edge in the state machine:** Moving from `STARTED`, `INVENTORY_RESERVED`, `COMPENSATING`, or `COMPENSATION_FAILED` to `RECOVERY_IN_PROGRESS` is a state transition. If subsequent resolution checks fail and the transaction is left in progress, the audit log explains how and when the transaction entered recovery.
- **`recover_in_progress()` is a lease renewal (heartbeat):** Touching `updated_at` on an already claimed transaction extends the worker's lock lease. Logging an event on every sweep for persistent downstream outages would generate unbounded event log bloat without state change.

### Honesty Note
**Real gaps closed:**
1. Prior to fixing `_claim()`, if recovery claimed a transaction and immediately failed downstream probes (calling `_leave_in_progress()`), the status column changed in the database with zero rows in `SagaEvent`.
2. Prior to adding `recover_compensating` and `recover_compensation_failed`, transactions that crashed mid-compensation or failed transiently were permanently abandoned by the background worker.

---

## 5. Known Tradeoffs, Not Fixed

### 1. Asymmetric Pre-Claim in `retry_compensation`
- **Observation:** `Command._compensate()` transitions the transaction to `COMPENSATING` in the database *before* making the HTTP call to Inventory. In contrast, `retry_compensation` makes the HTTP call to Inventory *before* writing any status transition.
- **Why it is safe:** Inventory's release endpoint is idempotent via `SELECT FOR UPDATE` on `Reservation`. Multiple calls with the same `transaction_id` do not double-decrement or double-restore stock.
- **The tradeoff:** If a client retries compensation while recovery is actively compensating, both processes dispatch an HTTP request to Inventory across the network. The loser detects concurrency only *after* the network call completes.
- **What a fix would look like:** `retry_compensation` would execute `_set_status_with_event(to_status=COMPENSATING)` before calling Inventory, allowing a concurrent caller to detect the active compensation and short-circuit without network I/O.
- **Why it was not done:** Adding an intermediate `COMPENSATING` state to the client request path introduces additional state handling for retry timeouts and creates client-visible state transitions for transient operations. Because the Inventory service already guarantees idempotency at the storage layer, the duplicate network call is a harmless efficiency tradeoff rather than a correctness flaw.

### 2. Unbounded Retry / Dead-Letter Queue for Persistent Downstream Rejections
- **Observation:** `recover_compensation_failed` retries stale `COMPENSATION_FAILED` transactions every 5 minutes. If Inventory has a persistent bug (e.g. 500 error or database corruption for a specific product ID), recovery will retry every 5 minutes indefinitely.
- **Why it is not fixed here:** Implementing a maximum retry counter (e.g. `retry_count >= 5` $\to$ `COMPENSATION_DEAD_LETTER` with manual operator alerting) requires adding a `retry_count` column to `Transaction` and defining a dead-letter state machine. This is a standard operational enhancement rather than a core distributed correctness bug.

---

## 6. Deliberately Not Built

### 1. Deriving `Transaction.status` Exclusively from `SagaEvent`
- **Considered:** Removing the mutable `Transaction.status` column entirely and computing current status dynamically via `@property` as the latest `SagaEvent.to_status`.
- **Why rejected:** Querying transaction lists (`GET /transactions/all/`) and running bulk state queries (`Transaction.objects.filter(status=STARTED)`) would require subqueries or table joins over `SagaEvent` on every read. Keeping `Transaction.status` as a synchronized materialized column written in the same atomic block as `SagaEvent` provides $O(1)$ indexed reads with zero performance penalty.

### 2. Transactional Outbox Pattern
- **Considered:** Writing state transitions and outbound HTTP dispatch payloads to an `Outbox` table in the database, with a background worker relaying calls to Inventory and Payment.
- **Why rejected:** Direct HTTP dispatch combined with timeout classification, periodic recovery sweep, and idempotent re-probing (`/payments/check/`, `/reserve/check/`) already provides robust crash recovery. Introducing an outbox relay adds queue ordering complexity (per-transaction FIFO serialization across relay workers) without eliminating the need for downstream idempotency.

### 3. TTL-Based Idempotency Key Expiry
- **Considered:** Adding time-to-live expiration to transaction idempotency keys.
- **Why rejected:** Expiry is a retention policy rather than a distributed correctness mechanism. Introducing TTL sweeps introduces race conditions where an expired transaction ID could be reused while a slow recovery worker is actively resolving the previous saga.

---

## Summary of Verification & Test Suite

All changes are covered by 33 automated tests in `coordinator-service`:
- `coordinator/tests.py`: 12 tests (saga execution, idempotency, terminal state guards, sequential duplicate protection).
- `coordinator/test_saga_log.py`: 11 tests (model constraints, sequential sequence ordering, CAS misses, standalone thread concurrency).
- `coordinator/test_retry_compensation_race.py`: 2 tests (before/after regression verification for `retry_compensation`).
- `coordinator/test_recovery_race.py`: 6 tests (inter-process `_compensate()` vs `retry_compensation` race, `_claim()` event creation, concurrent sweep claim, `COMPENSATING` sweep recovery, `COMPENSATION_FAILED` sweep retry & error handling).
- `coordinator/test_crash_recovery.py`: 2 tests (genuine crash recovery under downstream payment ambiguity: charged roll-forward vs uncharged compensation).

```
Ran 33 tests in 3.326s
OK
```
