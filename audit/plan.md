# System Hardening & Optimization Implementation Plan

This plan is ordered by **Severity** and **Architectural Dependency**. No code will be modified until explicit user approval is granted at the Phase 7 gate.

---

## Remediation Roadmap

### Tier 1: Critical Financial & Authorization Hardening (Zero Downtime)
1. **Remediate `REL-01` (Stranded Payment on Failed Inventory Release)**
   - **Files**: `coordinator-service/coordinator/views.py`, `coordinator-service/coordinator/management/commands/recovery_stuck_transactions.py`
   - **Change**: Uncouple payment refund execution from inventory release status. Both actions must execute independently in `_compensate()` and recovery sweeps.
   - **Verification**: New unit test verifying refund dispatch when inventory release returns 500/timeout.
   - **Risk**: Low. Rollback: Git revert.

2. **Remediate `SEC-01` (BOLA/IDOR in Retry Compensation)**
   - **Files**: `coordinator-service/coordinator/views.py`
   - **Change**: Add ownership verification check: `if not is_staff and user_id and txn.user_id != user_id: return 403`.
   - **Verification**: Test verifying 403 when User B retries compensation on User A's transaction.
   - **Risk**: Low. Rollback: Git revert.

3. **Remediate `SEC-02` (Privilege Escalation on Recovery Sweep)**
   - **Files**: `coordinator-service/coordinator/views.py`
   - **Change**: Enforce `is_staff` check on `POST /recovery/sweep/`.
   - **Verification**: Test verifying non-staff authenticated callers receive 403 Forbidden.
   - **Risk**: Low. Rollback: Git revert.

---

### Tier 2: Inter-Service Trust & Cryptographic Hardening
4. **Remediate `SEC-03` (Unauthenticated Payment Service Ingress)**
   - **Files**: `payment-service/payments/views.py`, `payment-service/payment_service/settings.py`
   - **Change**: Implement `_verify_internal_service_key` across `charge_payment`, `payment_check`, and `refund_payment`.
   - **Verification**: Test verifying that calls to payment endpoints without `X-Internal-Service-Key` return 403.
   - **Risk**: Medium (Coordinator must supply header on all outbound payment calls).

5. **Remediate `SEC-04` (Constant-Time Secret Comparisons)**
   - **Files**: `coordinator-service/coordinator/views.py`, `inventory-service/inventory/views.py`
   - **Change**: Replace `==` and `!=` on `STAFF_API_KEY` and `INTERNAL_SERVICE_KEY` with `secrets.compare_digest()`.
   - **Verification**: Unit tests with valid and invalid keys.
   - **Risk**: Minimal.

6. **Remediate `SEC-08` (Remove `DEBUG` Bypass for Internal Key)**
   - **Files**: `inventory-service/inventory/views.py`
   - **Change**: Disallow `DEBUG=True` from bypassing internal key checks.
   - **Verification**: Test that invalid keys are rejected even when `DEBUG=True`.
   - **Risk**: Low.

---

### Tier 3: Concurrency & Performance Hardening
7. **Remediate `CONC-01` (Hold Quota Race Condition)**
   - **Files**: `inventory-service/inventory/views.py`
   - **Change**: Protect the `active_holds` count check with a per-user atomic lock (Redis lock or DB transaction lock) during reservation.
   - **Verification**: Concurrent thread test confirming hold cap is strictly enforced under parallel requests.
   - **Risk**: Low.

8. **Remediate `PERF-01` & `PERF-02` (Remove Piggybacked Writes & N+1 Redis Calls on `GET /seats/`)**
   - **Files**: `inventory-service/inventory/views.py`
   - **Change**: Remove synchronous `reclaim_expired_holds()` and `Seat.objects.count() == 0` from `seat_list()`. Rely on Celery Beat sweeper and read-time serializer shedding.
   - **Verification**: Benchmark `GET /seats/` query count (reduces to 1 SELECT query).
   - **Risk**: Low.

9. **Remediate `SEC-05` (Secure Client IP Resolution)**
   - **Files**: `coordinator-service/coordinator/auth_views.py`
   - **Change**: Sanitize `_get_client_ip()` to avoid trusting arbitrary client-supplied `X-Forwarded-For` without trusted proxy validation.
   - **Verification**: Test verifying spoofed header does not reset rate limit buckets.
   - **Risk**: Low.
