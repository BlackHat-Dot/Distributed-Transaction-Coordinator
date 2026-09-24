# Implementation Plan Self-Revision Log (10 Iterative Challenge Rounds)

---

### Round 1: Dependency Analysis & Execution Ordering
- **Challenge**: The initial draft placed `SEC-03` (Payment Service Auth) before `REL-01` (Refund Execution).
- **Attack**: If we lock down Payment endpoints with `_verify_internal_service_key` first, any Coordinator calls to refund or charge that miss the header would immediately break. Furthermore, `REL-01` fixes an active customer fund-loss bug and should take absolute priority.
- **Change**: Reordered `REL-01` to Step 1 (Highest business risk). Positioned `SEC-03` after verifying all outbound Coordinator headers already include `X-Internal-Service-Key`.

---

### Round 2: Atomicity & Distributed Saga Invariants in `REL-01`
- **Challenge**: Does executing refund independently of inventory release violate the Saga pattern?
- **Attack**: What if inventory release succeeds, but refund fails? Or what if inventory release fails, and refund succeeds?
- **Analysis**: If refund succeeds while inventory release fails, the customer gets their money back, but the seat remains held until Celery Beat sweeper cleans it up. That is an acceptable, safe failure mode (money safe, seat cleaned later). Conversely, keeping the money while failing release is unacceptable (theft/loss).
- **Change**: Explicitly documented status transitions: If refund succeeds but release fails, set `status = COMPENSATION_FAILED` so the recovery daemon retries the release, but record `was_refunded=True` so it doesn't double-refund.

---

### Round 3: Breaking Changes in `SEC-01` (BOLA/IDOR on Retry Compensation)
- **Challenge**: Will enforcing `txn.user_id == auth_user_id` break automated recovery or Celery Beat?
- **Attack**: Does the Celery Beat sweeper use `retry_compensation`?
- **Analysis**: No. The recovery daemon calls `call_command('recovery_stuck_transactions')`, which acts directly via ORM methods and does not call the HTTP endpoint. However, dev/testing tools might call it with `X-Staff-Key`.
- **Change**: Updated condition to allow staff callers (`is_staff or (user_id and txn.user_id == user_id)`), ensuring admin/staff operations remain unhindered.

---

### Round 4: DoS Risk vs. Frontend Usability on `SEC-02` (Recovery Sweep)
- **Challenge**: The Visualizer UI has a "Manual Poll / Sweep" button. Will restricting `POST /recovery/sweep/` to `is_staff` break the visualizer for demo users?
- **Attack**: If regular demo users can't trigger a sweep, how does the visualizer demonstrate crash recovery?
- **Analysis**: The visualizer app has a demo mode toggle, and the recovery sweep runs automatically every 60–90 seconds via Celery Beat anyway. Allowing arbitrary public web clients to invoke management commands via HTTP is unacceptable in production.
- **Change**: In development/testing (`DEBUG=True` or `IS_TESTING=True`), allow the visualizer to pass a demo staff key or permit the call, but strictly enforce `is_staff` when `DEBUG=False`.

---

### Round 5: Payment Service Key Verification Rollout (`SEC-03`)
- **Challenge**: Does `payment-service` have `INTERNAL_SERVICE_KEY` configured in its environment settings?
- **Attack**: Inspecting `payment-service/payment_service/settings.py` shows it was not previously reading `INTERNAL_SERVICE_KEY`.
- **Analysis**: Adding the check without updating settings or docker-compose would cause 100% 403 errors on all payment operations.
- **Change**: Added `INTERNAL_SERVICE_KEY` declaration to `payment_service/settings.py` and ensured Coordinator's `_charge_payment` and `_refund_payment` explicitly send `X-Internal-Service-Key`.

---

### Round 6: Concurrency Lock Mechanism for `CONC-01`
- **Challenge**: Should `CONC-01` (Max User Holds race condition) be solved with a Redis distributed lock or a PostgreSQL row-level lock?
- **Attack**: Redis introduces an external network failure point. What if Redis is down?
- **Analysis**: In `inventory/views.py`, seats are already locked in PostgreSQL using `select_for_update()`. We can acquire a Postgres transactional advisory lock on the hash of `user_id` (`pg_advisory_xact_lock(hashtext(user_id))`). This is 100% atomic, requires zero extra Redis roundtrips, and automatically releases when the transaction commits or aborts!
- **Change**: Replaced external Redis lock proposal with PostgreSQL transactional advisory lock `hashtext(user_id)`. Simpler, faster, zero new dependencies.

---

### Round 7: Piggyback Sweep Elimination (`PERF-01`)
- **Challenge**: If we remove `reclaim_expired_holds()` from `GET /seats/`, will users see stale expired seats as "HELD"?
- **Attack**: A user opens the page and sees a seat held by someone who abandoned it 20 minutes ago.
- **Analysis**: No! `SeatSerializer.to_representation()` already performs read-time lazy shedding:
  `if rep.get("status") == "HELD" and instance.held_until < now: rep["status"] = "AVAILABLE"`.
  The UI already receives `"AVAILABLE"`, making the synchronous database update during `GET /seats/` completely redundant!
- **Change**: Confirmed removal of `reclaim_expired_holds()` from `GET /seats/`. Rely on `SeatSerializer` for instant presentation and Celery Beat for lazy physical cleanup.

---

### Round 8: Seed Check Elimination (`PERF-02`)
- **Challenge**: Can we remove `if Seat.objects.count() == 0:` without breaking clean demo setups?
- **Attack**: What happens when an engineer runs `docker-compose up` on a fresh database?
- **Analysis**: Running a full table count query on every HTTP request to check if demo data is needed is an anti-pattern. However, we should preserve developer convenience by ensuring `entrypoint.sh` or migrations run seed data.
- **Change**: Guard the seed check behind `if settings.DEBUG and not hasattr(settings, '_SEATS_SEEDED'):` so it executes at most once in memory per process, never touching PostgreSQL count under steady-state load.

---

### Round 9: Re-Verification of Blast Radius & Rollback Simplicity
- **Challenge**: Review all proposed changes for breaking changes to public API contracts.
- **Analysis**:
  - `POST /transactions/`: Unchanged.
  - `GET /seats/`: Unchanged response format (significantly faster latency).
  - `POST /transactions/<id>/retry-compensation/`: Returns 403 instead of 200/500 if caller is unauthorized (correct security boundary).
  - `_compensate()`: Dispatches refunds more reliably.
- **Change**: No modifications needed. All changes are backward-compatible bug fixes and security reinforcements.

---

### Round 10: Final Convergence Check
- **Challenge**: Review rounds 8 and 9. Were there any new architectural flaws or missing mitigations?
- **Analysis**: The plan is clean, dependency-ordered, introduces zero new dependencies, leverages built-in PostgreSQL transactional advisory locking, and fixes all critical and high severity findings.
- **Change**: No material changes. Plan is converged and locked.
