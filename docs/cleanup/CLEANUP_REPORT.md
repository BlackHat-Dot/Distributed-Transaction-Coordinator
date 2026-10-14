# DTC Codebase Cleanup Report

This report summarizes the repository-wide refactoring and hygiene campaign across all services in the DTC project (Django + DRF + Celery + Redis: Coordinator, Inventory, and Payment services, scripts, and frontend). All refactorings were conducted under the strict mandate of **ZERO behavior change**.

Every metric, summary line, exit code, and hash in this document is copied directly from an evidence file in `docs/cleanup/evidence/`.

---

## 1. Branch and Commit Verification

- **Branch**: `feasible` (branched cleanly from `learning`)
- **Start Commit (`learning`)**: `f24b471af32bca53feeff0ddb0a478c53548789f`
- **Current HEAD (`feasible`)**: `4d71742a770a05bc54d0a0215053ca02b897e68b`
- **Primary Branch (`master`)**: `6a3b17a7ce6751b3fa8d7cd1453e8f89c92c1a52` (Unchanged)
- **Base Verification**: Both `master` and `learning` were verified unchanged at completion (`git rev-parse master learning`). Zero commits were pushed or made to either branch.

### Git Log (`learning..feasible`)
```text
4d71742 fix: read env at call time where tests change it
d3deec4 docs: correct cleanup report
4e08903 test: align characterization tests with original behavior
edd28bb fix: restore original env var names and defaults
af16e3e docs: cleanup report
a1240c7 chore: fix ruff findings
9dd6865 chore: frontend cleanup (unused imports, dead files)
78bf77b chore: payment cleanup; no behavior change
7791b0f chore: inventory cleanup; no behavior change
4c3e3b2 chore: coordinator modules cleanup (dead code, comments, imports)
39eef32 refactor: simplify coordinator views (names, env vars, comments); no behavior change
5ee7af6 chore: move cleanup docs, remove junk files
79aa49f refactor: simplify coordinator views (names, env vars, comments); no behavior change
```

### Git Diff Summary (`learning..feasible`)
- **Git Shortstat**: `62 files changed, 1732 insertions(+), 1104 deletions(-)`
- **Total Changed Python Files**: 49 files (all compiled via `py_compile` with EXIT_CODE 0)

### Code Rewrite Commits & Scope Analysis
- **Two Identical Commit Messages**:
  - `79aa49fe28ba00fc492f7750e90c36d7957aaeb7`: Initial rewrite of `coordinator/views.py`, introduced `ENV_MAP.md`, `RENAME_MAP.md`, and `test_characterization.py`.
  - `39eef325e07464a3360d07403f1c8c0845a617ea`: Removed compatibility alias shims in `coordinator/views.py` and updated unit test mock/patch targets across 18 test files to target the new helper names.
- **Out-of-Scope Files**:
  - Commit `5ee7af6`: `.gitignore` (moved cleanup docs to `docs/cleanup/`).
  - Commit `a1240c7`: Documentation and audit notes (`COORDINATOR_CHANGE.md`, `README.md`, `audit/coverage.md`, `audit/final_report.md`, `audit/findings.md`, `audit/plan_revisions.md`, `flowchart.mmd`) touched during trailing-whitespace and lint passes.

---

## 2. Code Trees Identity Verification

From `docs/cleanup/evidence/identity_repo.log` and `docs/cleanup/evidence/identity_base.log`:

- **Feasible Repo Root (`C:\Users\MADHAVAN\Downloads\DTC`)**:
  - HEAD: `4d71742a770a05bc54d0a0215053ca02b897e68b`
  - `coordinator/views.py` Blob: `3ab473fde8afd5d14d9c51e9af2ba071e110b912`
- **Baseline Worktree (`C:\Users\MADHAVAN\Downloads\dtc_baseline`)**:
  - HEAD: `f24b471af32bca53feeff0ddb0a478c53548789f`
  - `coordinator/views.py` Blob: `16f16d3d37d3f5f263dbf4a5372f2167b6c9fdb3`

The blob hashes differ, proving the two trees run distinct revisions of `coordinator/views.py`.

---

## 3. Test Execution & Raw Summary Lines

All test suites were executed using both `Set-Location` to the tree AND the absolute path to `manage.py`, with `--noinput`.

### Baseline Summary Lines (`..\dtc_baseline` tracking `learning`)
- **Coordinator Service** (`evidence/base_coordinator.log`):
  ```text
  Ran 280 tests in 27.390s
  OK
  EXIT_CODE: 0
  ```
- **Characterization Tests** (`evidence/base_characterization.log`):
  ```text
  Ran 7 tests in 0.447s
  OK
  EXIT_CODE: 0
  ```
- **Inventory Service** (`evidence/base_inventory.log`):
  ```text
  Ran 165 tests in 4.843s
  OK
  EXIT_CODE: 0
  ```
- **Payment Service** (`evidence/base_payments.log`):
  ```text
  Ran 70 tests in 1.739s
  OK
  EXIT_CODE: 0
  ```

### Feasible Summary Lines (`feasible`)
- **Coordinator Service** (`evidence/repo_coordinator.log`):
  ```text
  Ran 280 tests in 26.648s
  OK
  EXIT_CODE: 0
  ```
- **Characterization Tests** (`evidence/repo_characterization.log`):
  ```text
  Ran 7 tests in 0.477s
  OK
  EXIT_CODE: 0
  ```
- **Inventory Service** (`evidence/repo_inventory.log`):
  ```text
  Ran 165 tests in 5.046s
  OK
  EXIT_CODE: 0
  ```
- **Payment Service** (`evidence/repo_payments.log`):
  ```text
  Ran 70 tests in 1.522s
  OK
  EXIT_CODE: 0
  ```

---

## 4. Redis Infrastructure Audit

From `docs/cleanup/evidence/redis.log` and `docs/cleanup/evidence/redis_timeouts.log`:

- **Connection Test (`Test-NetConnection 127.0.0.1 -Port 6379`)**:
  - `PingSucceeded : True`
  - `TcpTestSucceeded : False`
- **Installed Daemons (`Get-Command redis-server, memurai`)**:
  - None found on system PATH.
- **SSE Connection Timeout Warning Counts**:
  - `base_coordinator.log`: **305** lines containing `"Timeout connecting to server"`
  - `repo_coordinator.log`: **305** lines containing `"Timeout connecting to server"`

*Assessment*: Redis was not running. Live Redis pub/sub broadcasting was not exercised. The fallback/mock exception handling path was exercised identically in both baseline and feasible (305 timeouts each).

---

## 5. Characterization Scenarios & Original Views Code Mapping

From `docs/cleanup/evidence/characterization_mapping.log`:

1. **Test 1 (`test_1_hold_success`)**:
   - Assertions: HTTP 200, `status=Transaction.Status.HOLDING`, `held_until` present
   - Baseline Lines: `coordinator-service/coordinator/views.py:1048` (`_set_status_with_event` to `HOLDING`), `views.py:1067` (Response with `status: Transaction.Status.HOLDING`, `held_until`)
2. **Test 2 (`test_2_hold_with_reserve_failure`)**:
   - Assertions: HTTP 409, `code="SEAT_NOT_AVAILABLE"`, `status=Transaction.Status.INVENTORY_RESERVE_FAILED`
   - Baseline Lines: `coordinator-service/coordinator/views.py:1019` (`_set_status_with_event` to `INVENTORY_RESERVE_FAILED`), `views.py:1025-1031` (Response with `resp_json` and status 409)
3. **Test 3 (`test_3_pay_success`)**:
   - Assertions: HTTP 200, `status=Transaction.Status.COMPLETED`
   - Baseline Lines: `coordinator-service/coordinator/views.py:1996` (`_set_status_with_event` to `COMPLETED`), `views.py:2021` (Response with `status: Transaction.Status.COMPLETED`)
4. **Test 4 (`test_4_pay_with_payment_decline`)**:
   - Assertions: HTTP 400, `status=Transaction.Status.COMPENSATED`
   - Baseline Lines: `coordinator-service/coordinator/views.py:2207-2209` (`_set_status_with_event` to `COMPENSATED`), `views.py:2228` (Response with `status: Transaction.Status.COMPENSATED`, HTTP 400). *(Note: `PAYMENT_FAILED` status is not set anywhere in `coordinator/views.py`).*
5. **Test 5 (`test_5_pay_on_expired_hold`)**:
   - Assertions: HTTP 409, `code="HOLD_EXPIRED"`, `status=Transaction.Status.TIMEOUT`
   - Baseline Lines: `coordinator-service/coordinator/views.py:1136` (`_set_status_with_event` to `TIMEOUT`), `views.py:1163-1171` (Response with `code: "HOLD_EXPIRED"`, `status: Transaction.Status.TIMEOUT`, HTTP 409)
6. **Test 6 (`test_6_retry_on_non_eligible_status`)**:
   - Assertions: HTTP 400
   - Baseline Lines: `coordinator-service/coordinator/views.py:429-437` (Status allowlist check: if `txn.status not in ALLOWED_RETRY_STATUSES` -> HTTP 400)
7. **Test 7 (`test_7_idor_non_staff_denied_on_other_users_transaction`)**:
   - Assertions: GET -> HTTP 403; POST pay -> HTTP 403, `code="FORBIDDEN"`
   - Baseline Lines: `coordinator-service/coordinator/views.py:179` (GET ownership check -> HTTP 403), `views.py:1094-1098` (POST pay ownership check -> HTTP 403 with `code: "FORBIDDEN"`)

---

## 6. Ruff Lint Counts & Compilation

From `docs/cleanup/evidence/base_ruff.json`, `docs/cleanup/evidence/repo_ruff.json`, and `docs/cleanup/evidence/compile.log`:

- **Baseline Total Violations**: 646 (EXIT_CODE: 1)
- **Feasible Total Violations**: 584 (EXIT_CODE: 1)
- **Net Reduction**: -62 violations (0 new findings introduced)
- **Python Compilation**: 49 modified Python files in `learning..feasible` compiled with `python -m py_compile`.
  - All 49 exited with code 0 (`OVERALL_ALL_ZERO: True`).

---

## 7. Environment Variables Consolidation Map

Verified against `docs/cleanup/evidence/env_*.log` and `docs/cleanup/evidence/views_top_*.log`:

| Env Variable Name | Original Views Usage | Resolved Configuration in `views.py` | Call-Time vs Import-Time Audit |
| :--- | :--- | :--- | :--- |
| `REQUEST_CONNECT_TIMEOUT` | `views.py:29` | `float(os.getenv("REQUEST_CONNECT_TIMEOUT", "2.0"))` | Import-time module constant; default `"2.0"`. |
| `REQUEST_READ_TIMEOUT` | `views.py:30` | `float(os.getenv("REQUEST_READ_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "5.0")))` | Import-time module constant; falls back to `REQUEST_TIMEOUT`, else `"5.0"`. |
| `REQUEST_TIMEOUT` | `views.py:31` | `(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)` | Python tuple constant for `requests` calls. |
| `INVENTORY_SERVICE_URL` | `views.py:17` | `getattr(settings, "INVENTORY_SERVICE_URL", os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000")).rstrip("/")` | Import-time module constant with `getattr` fallback. |
| `PAYMENT_SERVICE_URL` | `views.py:18` | `getattr(settings, "PAYMENT_SERVICE_URL", os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001")).rstrip("/")` | Import-time module constant with `getattr` fallback. |
| `CELERY_BROKER_URL` | `views.py:270` | `getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")` | Call-time in SSE generator function. |
| `INTERNAL_SERVICE_KEY` | `views.py:32` | `getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")` | Module constant with dual fallback. |
| `STAFF_API_KEY` | `views.py:86` | `os.getenv("STAFF_API_KEY", "").strip()` | Read at call time in `_verify_staff_key` (no stale module fallback). |
| `RECOVERY_MIN_THRESHOLD_SECONDS` | `views.py:552` | `5 if getattr(settings, "DEBUG", False) else int(os.getenv("RECOVERY_MIN_THRESHOLD_SECONDS", "30"))` | Read at call time in sweep function. |
| `MAX_SEATS_PER_TRANSACTION` | `views.py:832, 868` | `int(os.getenv("MAX_SEATS_PER_TRANSACTION", "10"))` | Read at call time in `_validate_seats`. |
| `ADMIN_DOMAIN` | `views.py:2297` | `os.getenv("ADMIN_DOMAIN", "").strip().lower()` | Read at call time in `_is_admin_email` (no stale module fallback). |
| `STAFF_USER_ID` | `views.py:2417` | `request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or "system_staff"` | Read at call time in `_get_caller`. |
| `STAFF_EMAIL_ID` | `views.py:2418` | `request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"` | Read at call time in `_get_caller`. |
| `ALLOW_DEV_STAFF_BYPASS` | `views.py:2444-2445` | `getattr(settings, "ALLOW_DEV_STAFF_BYPASS", False) or (os.getenv("ALLOW_DEV_STAFF_BYPASS", "").lower() in ("true", "1", "yes", "t"))` | Read at call time in `_get_caller`. |
| `ENABLE_FAULT_INJECTION` | `views.py:91-94` | `getattr(settings, "ENABLE_FAULT_INJECTION", False) or (os.getenv("ENABLE_FAULT_INJECTION", "").lower() in ("true", "1", "yes", "t"))` | Read at call time in `_faults_on`. |

*(Note: `COORDINATOR_SERVICE_URL` is absent from both learning and feasible's `views.py` and is excluded from the views env map).*

---

## 8. Function and Helper Renaming Map

Detailed in [`docs/cleanup/RENAME_MAP.md`](file:///c:/Users/MADHAVAN/Downloads/DTC/docs/cleanup/RENAME_MAP.md):

| Original Identifier | New Identifier | Files Touched |
| :--- | :--- | :--- |
| `_get_active_correlation_id` | `_corr_id` | `coordinator/views.py`, `tests/test_audit_hardening.py` |
| `_fault_injection_allowed` | `_faults_on` | `coordinator/views.py`, `tests/test_b4_fault_injection_and_secrets.py` |
| `_request_payload_verify` | `_check_body` | `coordinator/views.py`, `tests/test_audit_hardening.py`, `tests/test_coverage_boost.py`, `flowchart.mmd` |
| `_resolve_seat_pricing` | `_get_prices` | `coordinator/views.py`, `tests/test_a7_reserve_order.py`, `tests/test_coverage_boost.py`, `flowchart.mmd` |
| `_handle_create_transaction` | `_create_txn` | `coordinator/views.py`, `tests/test_audit_hardening.py`, `README.md` |
| `_handle_list_transactions` | `_list_txns` | `coordinator/views.py` |
| `_run_saga_pipeline` | `_run_saga` | `coordinator/views.py`, `coordinator/tasks.py`, `tests/test_a3_concurrency.py`, `tests/test_coverage_boost.py`, `audit/coverage.md`, `flowchart.mmd` |
| `_reserve_inventory` | `_reserve` | `coordinator/views.py`, `tests/test_a6_request_exceptions.py`, `tests/test_a7_reserve_order.py`, `audit/coverage.md`, `flowchart.mmd` |
| `_confirm_inventory` | `_confirm` | `coordinator/views.py`, `tests/test_a2_cas_checks.py`, `tests/test_a6_request_exceptions.py`, `tests/test_a8_cleanup.py`, `tests/test_audit_hardening.py`, `tests/test_exit_gate_gaps.py`, `audit/coverage.md`, `flowchart.mmd` |
| `_charge_payment` | `_charge` | `coordinator/views.py`, `tests/test_a3_concurrency.py`, `tests/test_a5_payment_5xx.py`, `tests/test_a6_request_exceptions.py`, `tests/test_audit_hardening.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py`, `tests/test_f1_checkout_presence.py`, `tests/test_g8_coordinator_checkout_presence_resilience.py`, `audit/coverage.md`, `audit/final_report.md`, `audit/plan_revisions.md`, `COORDINATOR_CHANGE.md`, `flowchart.mmd` |
| `_do_charge_payment` | `_do_charge` | `coordinator/views.py` |
| `_finalize_payment_outcome` | `_finish_payment` | `coordinator/views.py`, `tests/test_a2_cas_checks.py`, `tests/test_a8_cleanup.py`, `tests/test_audit_hardening.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py`, `README.md`, `flowchart.mmd` |
| `_get_requester_identity` | `_get_caller` | `coordinator/views.py`, `tests/test_audit_hardening.py`, `tests/test_b1_ownership.py`, `tests/test_b2_staff_bypass.py`, `tests/test_b3_admin_domain.py`, `tests/test_characterization.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py`, `audit/coverage.md`, `audit/findings.md`, `flowchart.mmd` |
| `_serialize_transaction` | `_txn_json` | `coordinator/views.py`, `tests/test_audit_hardening.py` |
| `_serialize_event` | `_event_json` | `coordinator/views.py`, `tests/test_coverage_boost.py` |
| `_transport_status` | `_err_status` | `coordinator/views.py`, `README.md` |

---

## 9. Forbidden Pattern Verification

- `_create_transaction_idempotent`: **0 matches** in codebase
- `mock_calls`: **0 matches** in code (only in `docs/cleanup/CLEANUP_PLAN.md`)
- `# I know`: **0 matches** across entire repository
- `resp_json`: **0 matches** in code (only in `docs/cleanup/RENAME_MAP.md`)
- `git grep` on all 16 renamed functions: **0 matches** outside `docs/cleanup/RENAME_MAP.md`

---

## 10. UNVERIFIED Items

1. **Redis Live Pub/Sub Tests (UNVERIFIED)**:
   - Live Redis broadcasting on port 6379 could not be exercised live because no Redis or Memurai service is installed or running on localhost (`TcpTestSucceeded : False`). Timeout fallback exception paths were verified (305 timeout lines in each coordinator run).
2. **Database Migrations (Skipped)**:
   - Two unused `models` imports in migrations remain untouched per migration immutability rules:
     - `inventory-service/inventory/migrations/0011_remove_held_by_columns.py:3`
     - `payment-service/payments/migrations/0008_remove_payment_held_by_columns.py:3`
3. **Browser E2E Tests (UNVERIFIED)**:
   - `tests/test_gate_scanner.py` and Playwright tests require external active web server containers on ports 8000/8002/3001.
