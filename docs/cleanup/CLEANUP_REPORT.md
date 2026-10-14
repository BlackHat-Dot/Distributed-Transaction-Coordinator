# DTC Codebase Cleanup Report

This report summarizes the repository-wide refactoring and hygiene campaign across all services in the DTC project (Django + DRF + Celery + Redis: Coordinator, Inventory, and Payment services, scripts, and frontend). All refactorings were conducted under the strict mandate of **ZERO behavior change**.

Every metric, summary line, exit code, and hash in this document is copied directly from verified command output printed in the verification log.

---

## 1. Branch and Commit Verification

- **Branch**: `feasible` (branched cleanly from `learning`)
- **Start Commit (`learning`)**: `f24b471af32bca53feeff0ddb0a478c53548789f`
- **Primary Branch (`master`)**: `6a3b17a7ce6751b3fa8d7cd1453e8f89c92c1a52` (Unchanged)
- **Base Verification**: Both `master` and `learning` were verified unchanged at completion (`git rev-parse master learning`). Zero commits were pushed or made to either branch.

### Git Log (`learning..feasible`)
```text
11bea54 docs: correct verification evidence
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

### Commit Scope and Views Rewrite Analysis
Four commits in `learning..feasible` touched `coordinator-service/coordinator/views.py`:
- `79aa49f`: Initial rewrite of `coordinator/views.py`, introduced `ENV_MAP.md`, `RENAME_MAP.md`, and `test_characterization.py`.
- `39eef32`: Removed backward-compatibility alias shims in `views.py` and migrated mock/patch targets across 16 test files matching `test_` (`test_a2_cas_checks.py`, `test_a3_concurrency.py`, `test_a5_payment_5xx.py`, `test_a6_request_exceptions.py`, `test_a7_reserve_order.py`, `test_a8_cleanup.py`, `test_audit_hardening.py`, `test_b1_ownership.py`, `test_b2_staff_bypass.py`, `test_b3_admin_domain.py`, `test_b4_fault_injection_and_secrets.py`, `test_characterization.py`, `test_coordinator.py`, `test_coverage_boost.py`, `test_exit_gate_gaps.py`, `test_f1_checkout_presence.py`).
- `edd28bb`: Restored original env var names (`REQUEST_CONNECT_TIMEOUT`, `REQUEST_READ_TIMEOUT`) and default fallback behaviors.
- `4d71742`: Converted `STAFF_API_KEY`, `MAX_SEATS_PER_TRANSACTION`, `ADMIN_DOMAIN`, `STAFF_USER_ID`, and `STAFF_EMAIL_ID` to call-time reads to ensure runtime environment overrides in tests are respected.

**Out-of-Scope Files**:
- Commit `5ee7af6`: `.gitignore` (moved cleanup docs into `docs/cleanup/`).
- Commit `a1240c7`: Documentation and audit notes (`COORDINATOR_CHANGE.md`, `README.md`, `audit/*`, `flowchart.mmd`) updated during trailing-whitespace and lint fixes.

---

## 2. Test Execution & Raw Summary Lines

All test suites were executed using both `Set-Location` to the tree directory AND the absolute path to `manage.py`, with `--noinput`.

### Baseline Summary Lines (`..\dtc_baseline` tracking `learning`)
- **Coordinator Service**:
  ```text
  Ran 280 tests in 27.390s
  OK
  EXIT_CODE: 0
  ```
- **Characterization Tests**:
  ```text
  Ran 7 tests in 0.447s
  OK
  EXIT_CODE: 0
  ```
- **Inventory Service**:
  ```text
  Ran 165 tests in 4.843s
  OK
  EXIT_CODE: 0
  ```
- **Payment Service**:
  ```text
  Ran 70 tests in 1.739s
  OK
  EXIT_CODE: 0
  ```

### Feasible Summary Lines (`feasible`)
- **Coordinator Service**:
  ```text
  Ran 280 tests in 26.648s
  OK
  EXIT_CODE: 0
  ```
- **Characterization Tests**:
  ```text
  Ran 7 tests in 0.477s
  OK
  EXIT_CODE: 0
  ```
- **Inventory Service**:
  ```text
  Ran 165 tests in 5.046s
  OK
  EXIT_CODE: 0
  ```
- **Payment Service**:
  ```text
  Ran 70 tests in 1.522s
  OK
  EXIT_CODE: 0
  ```

---

## 3. Characterization Grep Mapping

Mapped against live grep: `git -C ..\dtc_baseline grep -n -E "Status\.(HOLDING|INVENTORY_RESERVE_FAILED|COMPLETED|COMPENSATED|TIMEOUT)|HOLD_EXPIRED|ALLOWED_RETRY_STATUSES|FORBIDDEN" -- coordinator-service/coordinator/views.py`:

1. **`test_1_hold_success`**:
   - HTTP 200: line 1064
   - `Transaction.Status.HOLDING`: line 1048 (`to_status`), line 1067 (`"status": Transaction.Status.HOLDING`)
2. **`test_2_hold_with_reserve_failure`**:
   - HTTP 409: line 1030
   - `code: SEAT_NOT_AVAILABLE`: line 1026
   - `Transaction.Status.INVENTORY_RESERVE_FAILED`: line 1019
3. **`test_3_pay_success`**:
   - HTTP 200: line 2018
   - `Transaction.Status.COMPLETED`: line 1996 (`to_status`), line 2021 (`"status": Transaction.Status.COMPLETED`)
4. **`test_4_pay_with_payment_decline`**:
   - HTTP 400: line 2226
   - `Transaction.Status.COMPENSATED`: line 2207 (`to_status`), line 2228 (`"status": Transaction.Status.COMPENSATED`)
5. **`test_5_pay_on_expired_hold`**:
   - HTTP 409: line 1170
   - `code: HOLD_EXPIRED`: line 1166
   - `Transaction.Status.TIMEOUT`: line 1136 (`to_status`), line 1167 (`"status": Transaction.Status.TIMEOUT`)
6. **`test_6_retry_on_non_eligible_status`**:
   - HTTP 400: line 429 (`if txn.status not in ALLOWED_RETRY_STATUSES:`), line 436 (`status=400`)
7. **`test_7_idor_non_staff_denied_on_other_users_transaction`**:
   - GET HTTP 403: line 179
   - POST pay HTTP 403: line 1097
   - `code: FORBIDDEN`: line 1096
- **Mismatches**: 0

---

## 4. Ruff Lint Counts: Baseline vs. Feasible

- **Baseline Total**: 646 violations (Exit Code: 1)
- **Feasible Total**: 584 violations (Exit Code: 1)
- **Net Reduction**: -62 violations

```text
| Rule       | Baseline   | Feasible   | Delta    |
|:---------- |:---------- |:---------- |:-------- |
| BLE001     | 123        | 123        | 0        |
| C414       | 1          | 1          | 0        |
| DTZ005     | 1          | 1          | 0        |
| F401       | 51         | 2          | -49      |
| F541       | 1          | 0          | -1       |
| F841       | 7          | 0          | -7       |
| FURB157    | 21         | 21         | 0        |
| FURB188    | 1          | 1          | 0        |
| G201       | 3          | 3          | 0        |
| I001       | 144        | 142        | -2       |
| LOG014     | 1          | 1          | 0        |
| PIE807     | 10         | 10         | 0        |
| PIE810     | 1          | 1          | 0        |
| PLE1206    | 1          | 1          | 0        |
| PLR0402    | 2          | 2          | 0        |
| PLR1711    | 1          | 1          | 0        |
| PLW1508    | 1          | 1          | 0        |
| RET501     | 1          | 1          | 0        |
| RUF010     | 8          | 8          | 0        |
| RUF012     | 108        | 108        | 0        |
| RUF013     | 1          | 1          | 0        |
| RUF015     | 4          | 4          | 0        |
| RUF019     | 2          | 0          | -2       |
| RUF059     | 30         | 29         | -1       |
| S110       | 74         | 74         | 0        |
| S112       | 1          | 1          | 0        |
| SIM102     | 7          | 7          | 0        |
| SIM103     | 1          | 1          | 0        |
| SIM114     | 1          | 1          | 0        |
| SIM117     | 25         | 25         | 0        |
| SIM118     | 1          | 1          | 0        |
| TRY002     | 3          | 3          | 0        |
| TRY401     | 6          | 6          | 0        |
| UP012      | 3          | 3          | 0        |
```

---

## 5. Redis Infrastructure Audit

- **`Get-Service *redis*, *memurai*`**: No Redis daemon service found (only `GameInputRedistService`).
- **`Get-Command redis-server, memurai`**: `not found`.
- **`Test-NetConnection 127.0.0.1 -Port 6379`**: `TcpTestSucceeded : False`.
- **Coordinator Warning Counts (`"Timeout connecting to server"`)**:
  - `base_coordinator.log`: **305**
  - `repo_coordinator.log`: **305**
- *Audit*: Live Redis pub/sub broadcasting was not exercised live. Fallback error handling was exercised identically across both trees.

---

## 6. Corrections from Previous Report

1. **Self-Contained Verification**: Removed all references to external `docs/cleanup/evidence/` files; all evidence lines, test summaries, and exit codes are now directly embedded in `VERIFY_LOG.md` from chat output.
2. **Commit Scope Precision**: Detailed the exact 4 commits modifying `coordinator-service/coordinator/views.py` (`79aa49f`, `39eef32`, `edd28bb`, `4d71742`).
3. **Test Files in Commit `39eef32`**: Clarified that 16 test files match the `test_` pattern in `git show --stat 39eef32`.
4. **Call-Time Env Var Audit**: Documented the refactoring of 5 env vars (`STAFF_API_KEY`, `MAX_SEATS_PER_TRANSACTION`, `ADMIN_DOMAIN`, `STAFF_USER_ID`, `STAFF_EMAIL_ID`) to call-time reads to dynamically respect test environment patches without stale module constants.
5. **Exact Output Reproduction**: All test summary lines and ruff violation counts match live execution output verbatim.

---

## 7. UNVERIFIED Items

1. **Redis Live Pub/Sub Tests**: Unverified live because Redis daemon is not reachable on localhost port 6379 (`TcpTestSucceeded : False`). Graceful fallback paths were verified (305 warnings in both suites).
2. **Database Migrations (Skipped)**: Unused `models` imports in `inventory/migrations/0011_remove_held_by_columns.py` and `payments/migrations/0008_remove_payment_held_by_columns.py` remain untouched to preserve migration immutability.
3. **Browser E2E Tests**: `tests/test_gate_scanner.py` and Playwright tests require external active service containers on ports 8000/8002/3001.
