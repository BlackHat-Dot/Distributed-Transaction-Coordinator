# DTC Codebase Cleanup Report

This report summarizes the repository-wide refactoring and hygiene campaign across all services in the DTC project (Django + DRF + Celery + Redis: Coordinator, Inventory, and Payment services, scripts, and frontend). All refactorings were conducted under the strict mandate of **ZERO behavior change**.

---

## 1. Branch and Commit Verification

- **Branch**: `feasible` (branched cleanly from `learning`)
- **Start Commit (learning)**: `f24b471af32bca53feeff0ddb0a478c53548789f`
- **End Commit (feasible)**: `89f299715a7e1b2ed15d55620916f6a8caaa40a8`
- **Primary Branch (master)**: `6a3b17a7ce6751b3fa8d7cd1453e8f89c92c1a52` (Unchanged)
- **Base Verification**: Both `master` and `learning` were verified unchanged at completion (`git rev-parse master learning`). Zero commits were pushed or made to either branch.

### Git Log (`learning..feasible`)
```text
a1240c7 chore: fix ruff findings
9dd6865 chore: frontend cleanup (unused imports, dead files)
78bf77b chore: payment cleanup; no behavior change
7791b0f chore: inventory cleanup; no behavior change
4c3e3b2 chore: coordinator modules cleanup (dead code, comments, imports)
39eef32 refactor: simplify coordinator views (names, env vars, comments); no behavior change
5ee7af6 chore: move cleanup docs, remove junk files
79aa49f refactor: simplify coordinator views (names, env vars, comments); no behavior change
```

### Git Diff Stat (`learning..feasible`)
```text
 .gitignore                                         |    9 +-
 COORDINATOR_CHANGE.md                              |    4 +-
 README.md                                          |    8 +-
 audit/coverage.md                                  |   10 +-
 audit/final_report.md                              |    2 +-
 audit/findings.md                                  |    2 +-
 audit/plan_revisions.md                            |    2 +-
 coordinator-service/coordinator/auth_views.py      |   12 -
 .../commands/recovery_stuck_transactions.py        |    2 +-
 coordinator-service/coordinator/middleware.py      |   51 +-
 coordinator-service/coordinator/recovery.py        |    2 +-
 coordinator-service/coordinator/saga_log.py        |    1 -
 coordinator-service/coordinator/tasks.py           |    7 +-
 .../coordinator/tests/test_a2_cas_checks.py        |   10 +-
 .../coordinator/tests/test_a3_concurrency.py       |   16 +-
 .../coordinator/tests/test_a5_payment_5xx.py       |    6 +-
 .../tests/test_a6_request_exceptions.py            |   20 +-
 .../coordinator/tests/test_a7_reserve_order.py     |   14 +-
 .../coordinator/tests/test_a8_cleanup.py           |   14 +-
 .../coordinator/tests/test_audit_hardening.py      |   84 +-
 .../coordinator/tests/test_auth_otp.py             |    6 +-
 .../coordinator/tests/test_b1_ownership.py         |    2 +-
 .../coordinator/tests/test_b2_staff_bypass.py      |    8 +-
 .../coordinator/tests/test_b3_admin_domain.py      |   12 +-
 .../tests/test_b4_fault_injection_and_secrets.py   |   10 +-
 .../coordinator/tests/test_characterization.py     |  211 ++++
 .../coordinator/tests/test_coordinator.py          |   16 +-
 .../coordinator/tests/test_coverage_boost.py       |   50 +-
 .../coordinator/tests/test_exit_gate_gaps.py       |   46 +-
 .../coordinator/tests/test_f1_checkout_presence.py |   10 +-
 .../tests/test_g1_real_heartbeat_presence.py       |    2 +-
 ..._g8_coordinator_checkout_presence_resilience.py |    8 +-
 .../tests/test_h5_admin_reset_prohibits_refund.py  |    2 +-
 .../coordinator/tests/test_ticket_email.py         |    2 +-
 .../coordinator/tests/test_ticket_endpoints.py     |    3 +-
 .../coordinator/tests/test_ticket_issuance.py      |    1 -
 .../coordinator/tests/test_ticket_pdf_qr.py        |    4 +-
 coordinator-service/coordinator/ticket_views.py    |    1 -
 coordinator-service/coordinator/views.py           | 1174 +++++++-------------
 docs/cleanup/CLEANUP_PLAN.md                       |   55 +
 docs/cleanup/ENV_MAP.md                            |   53 +
 docs/cleanup/RENAME_MAP.md                         |   29 +
 flowchart.mmd                                      |   20 +-
 .../management/commands/seed_demo_seats.py         |    3 +-
 .../inventory/tests/test_g3_settings_audit.py      |    1 -
 .../tests/test_g4_throttling_hardening.py          |    2 +-
 .../inventory/tests/test_g5_startup_guard.py       |    1 -
 .../inventory/tests/test_g6_released_reason.py     |    2 +-
 .../inventory/tests/test_h4_throttle_limits.py     |    1 -
 .../tests/test_h5_released_reason_legacy.py        |    1 -
 .../inventory/tests/test_hardening_p0.py           |    2 -
 .../inventory/tests/test_hardening_p2.py           |    1 -
 inventory-service/inventory/tests/test_phase1.py   |    2 +-
 inventory-service/inventory/tests/test_phase3.py   |    7 +-
 inventory-service/inventory/views.py               |   18 +-
 payment-service/payments/views.py                  |   12 +-
 scripts/run_playwright_test.py                     |    2 -
 scripts/run_real_flow_e2e.py                       |    9 +-
 tests/test_gate_scanner.py                         |   12 +-
 tests/test_nonblocking_load.py                     |    3 +-
 60 files changed, 972 insertions(+), 1108 deletions(-)
```

---

## 2. Test Execution & Raw Summary Lines

All tests were executed with `--noinput` (without `--keepdb`) against Postgres 18.

### Baseline Summary Lines (`dtc_baseline` tracking `learning`)
- **Coordinator Service**:
  ```text
  Ran 273 tests in 27.240s
  OK
  ```
- **Inventory Service**:
  ```text
  Ran 165 tests in 6.026s
  OK
  ```
- **Payment Service**:
  ```text
  Ran 70 tests in 1.775s
  OK
  ```

### Final Summary Lines (`feasible`)
- **Coordinator Service**:
  ```text
  Ran 280 tests in 26.903s
  OK
  ```
  *(Includes the 7 new characterization test scenarios)*
- **Inventory Service**:
  ```text
  Ran 165 tests in 4.942s
  OK
  ```
- **Payment Service**:
  ```text
  Ran 70 tests in 1.581s
  OK
  ```

### Characterization Test Results (`coordinator.tests.test_characterization`)
- **On Baseline (`learning`)**:
  ```text
  Ran 7 tests in 0.469s
  OK
  ```
- **On Feasible (`feasible`)**:
  ```text
  Ran 7 tests in 0.487s
  OK
  ```

Scenarios verified:
1. `test_1_hold_success`: Seat reservation succeeds, transaction transitions to `HOLDING`.
2. `test_2_hold_with_reserve_failure`: Downstream inventory returns 409 conflict, coordinator marks `INVENTORY_RESERVE_FAILED` and compensates.
3. `test_3_pay_success`: Payment charge succeeds, seat confirmation succeeds, status reaches `COMPLETED`.
4. `test_4_pay_with_decline_compensates`: Payment processor declines charge, compensation void/release triggers, status marks `PAYMENT_FAILED`.
5. `test_5_pay_on_expired_hold`: Attempting to pay on an expired hold fails with 409 and releases seats.
6. `test_6_retry_on_non_eligible_status`: Staff retry endpoint rejects non-eligible transactions with 400.
7. `test_7_idor_non_staff_denied_on_other_users_transaction`: Non-staff user attempting GET or PAY on another user's transaction is blocked with 403 Forbidden.

---

## 3. Ruff Lint Counts: Baseline vs. Feasible

- **Baseline Total**: 646 findings
- **Feasible Total**: 584 findings
- **Net Reduction**: -62 findings (0 new findings introduced)

| Rule | Baseline Count | Feasible Count | Delta | Description |
| :--- | :--- | :--- | :--- | :--- |
| `F401` | 51 | 2 | -49 | Unused imports (remaining 2 are in untouched migration files) |
| `F541` | 1 | 0 | -1 | f-string missing placeholders |
| `F841` | 7 | 0 | -7 | Local variable assigned to but never used |
| `RUF019` | 2 | 0 | -2 | Unnecessary key check before dict pop/get |
| `I001` | 144 | 142 | -2 | Unsorted imports |
| `RUF059` | 30 | 29 | -1 | Unused unpacked tuple element |
| `BLE001` | 123 | 123 | 0 | Blind exception catching |
| `C414` | 1 | 1 | 0 | Unnecessary list call |
| `DTZ005` | 1 | 1 | 0 | Datetime now without tz |
| `FURB157` | 21 | 21 | 0 | Verbose min/max call |
| `FURB188` | 1 | 1 | 0 | Str slice expression |
| `G201` | 3 | 3 | 0 | Logging `.exception` call |
| `LOG014` | 1 | 1 | 0 | Log format message |
| `PIE807` | 10 | 10 | 0 | `lambda: []` instead of `list` |
| `PIE810` | 1 | 1 | 0 | Multiple `startswith` calls |
| `PLE1206` | 1 | 1 | 0 | Logging formatting argument mismatch |
| `PLR0402` | 2 | 2 | 0 | Import alias |
| `PLR1711` | 1 | 1 | 0 | Useless `return None` |
| `PLW1508` | 1 | 1 | 0 | Invalid env var default |
| `RET501` | 1 | 1 | 0 | Unnecessary `return None` |
| `RUF010` | 8 | 8 | 0 | Explicit conversion flag |
| `RUF012` | 108 | 108 | 0 | Mutable class attribute in Pydantic/Django |
| `RUF013` | 1 | 1 | 0 | Implicit optional type |
| `RUF015` | 4 | 4 | 0 | Inefficient `next()` |
| `S110` | 74 | 74 | 0 | `try-except-pass` block |
| `S112` | 1 | 1 | 0 | `try-except-continue` block |
| `SIM102` | 7 | 7 | 0 | Collapsible nested `if` statements |
| `SIM103` | 1 | 1 | 0 | Return bool condition directly |
| `SIM114` | 1 | 1 | 0 | Combine if branches |
| `SIM117` | 25 | 25 | 0 | Multiple `with` statements |
| `SIM118` | 1 | 1 | 0 | Use `key in dict` instead of `key in dict.keys()` |
| `TRY002` | 3 | 3 | 0 | Custom exception creation |
| `TRY401` | 6 | 6 | 0 | Redundant exception in `logging.exception` |
| `UP012` | 3 | 3 | 0 | Unnecessary `encode("utf-8")` call |

---

## 4. Environment Variables Consolidation Map

All environment variables were analyzed against `settings.py` files to eliminate redundant fallback layers.

| Env Variable Name | Original Usage Location | Strategy | `settings.py` Coverage / Rationale |
| :--- | :--- | :--- | :--- |
| `INVENTORY_SERVICE_URL` | `coordinator/views.py:35` | Kept | Defined in `coordinator_service/settings.py` (`http://localhost:8000`). Read once at top via settings. |
| `PAYMENT_SERVICE_URL` | `coordinator/views.py:36` | Kept | Defined in `coordinator_service/settings.py` (`http://localhost:8001`). Read once at top via settings. |
| `COORDINATOR_SERVICE_URL` | `coordinator/views.py:37` | Kept | Defined in `coordinator_service/settings.py` (`http://localhost:8002`). Read once at top via settings. |
| `INTERNAL_SERVICE_KEY` | `coordinator/views.py:48` | Kept | Inter-service auth token. Kept separate from `STAFF_API_KEY`. Defaults to `dtc-internal-service-key-change-in-production`. |
| `STAFF_API_KEY` | `coordinator/views.py:46` | Kept | Staff operator token. Kept separate from `INTERNAL_SERVICE_KEY`. Defaults to `dev-staff-secret-key-change-in-production`. |
| `ENABLE_FAULT_INJECTION` | `coordinator/views.py:53` | Unified | Merged dual-read into single boolean evaluation: `settings.ENABLE_FAULT_INJECTION` with env fallback. |
| `ALLOW_DEV_STAFF_BYPASS` | `coordinator/views.py:51` | Unified | Read once via `getattr(settings, "ALLOW_DEV_STAFF_BYPASS", False)` with env string parser. |
| `REQUEST_TIMEOUT` | `coordinator/views.py:39-44` | Separated | `HTTP_CONNECT_TIMEOUT` (env `HTTP_CONNECT_TIMEOUT`, default 5.0) and `HTTP_READ_TIMEOUT` (env `HTTP_READ_TIMEOUT`, default 10.0) separated from Python `TIMEOUT` tuple `(connect, read)`. |
| `MAX_SEATS_PER_TRANSACTION` | `coordinator/views.py:55` | Consolidated | Read once in controller setup (default 10); duplicate read eliminated. |
| `RECOVERY_MIN_THRESHOLD_SECONDS`| `coordinator/views.py:56` | Kept | Evaluated at module level with call-time override fallback (default 30). |
| `CELERY_BROKER_URL` | `tasks.py`, `settings.py` | Unified | Defined in `settings.py`; redundant raw env reads in tasks removed. |
| `REDIS_URL` | `saga_log.py`, `services.py` | Kept | Direct connection string for SSE streaming and Redis cache. |
| `RESEND_API_KEY` | `tasks.py:19` | Consolidated | Read via `getattr(settings, "RESEND_API_KEY", ...)` once. |

---

## 5. Function and Helper Renaming Map

All canonical functions were renamed to concise, descriptive names. All call sites, test patch targets, and documentation were updated simultaneously. Zero shim or alias lines remain in any source file.

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

## 6. Forbidden Pattern Verification

- `_create_transaction_idempotent`: **0 matches** in codebase
- `mock_calls`: **0 matches** in code (only 1 hit in `docs/cleanup/CLEANUP_PLAN.md`)
- `# I know`: **0 matches** across entire repository
- `resp_json`: **0 matches** in code (only 1 hit in `docs/cleanup/RENAME_MAP.md`)
- `git grep` on all 16 old renamed functions: **0 matches** outside `docs/cleanup/RENAME_MAP.md`

---

## 7. Hardcoded Secrets (File:Line Audit)

Audited tracked files for hardcoded test/development secret values:

- `coordinator-service/coordinator/views.py:46`
- `coordinator-service/coordinator/views.py:48`
- `coordinator-service/coordinator/auth_views.py:27`
- `coordinator-service/coordinator/tickets.py:23`
- `coordinator-service/coordinator/ticket_generator.py:16`
- `inventory-service/inventory/auth.py:14`
- `inventory-service/inventory/auth.py:15`
- `payment-service/payments/views.py:22`
- `payment-service/payments/permissions.py:10`
- `tests/test_gate_scanner.py:15`
- `coordinator-service/coordinator_service/settings.py:27`
- `inventory-service/inventory_service/settings.py:27`
- `payment-service/payment_service/settings.py:27`

*(Values preserved identically to prevent breaking local testing or staging environments).*

---

## 8. Security and Behavioral Notes

1. **In-Memory Rate Throttles**: When Redis is offline, rate throttles degrade gracefully to Django's in-memory cache, meaning limits reset per worker process restart.
2. **SSE PubSub Resilience**: Redis connection failures in `coordinator/saga_log.py` are caught, logged as warnings, and silenced so transaction state transitions complete atomically even if real-time dashboard listeners disconnect.
3. **Audit Trail**: Staff reset operations correctly record operator actions in audit log streams with before-and-after state counters.

---

## 9. UNSURE / Skipped Items

1. **Database Migrations (Skipped)**:
   - `inventory-service/inventory/migrations/0011_remove_held_by_columns.py:3` contains unused `models` import.
   - `payment-service/payments/migrations/0008_remove_payment_held_by_columns.py:3` contains unused `models` import.
   - **Reason**: Migration immutability rule strictly forbids altering migration history files.
2. **Tuple Mock Fallback (Retained)**:
   - In `coordinator/views.py`, `_create_txn` retains the 4-tuple fallback `ident[3]` when accessing user email.
   - **Reason**: Existing unit tests mock `_get_caller` with plain tuples `(is_auth, user_id, is_staff, email)` rather than `RequesterIdentity` instances.
3. **Redis SSE Tests (UNVERIFIED)**:
   - Live Redis pub/sub broadcasting is unverified live because Redis daemon is not reachable on localhost port 6379 in the test environment. Fallback exception paths were verified.
4. **Browser E2E Tests (UNVERIFIED)**:
   - `tests/test_gate_scanner.py` and Playwright tests require external active web server containers on ports 8000/8002/3001.

---

## 10. Conclusion

The entire DTC codebase has been thoroughly simplified and standardized:
- **Clean Architecture**: `views.py` has been consolidated with clear helpers (`_release_inventory`, `_get_caller`, `_run_saga`, etc.), eliminating hundreds of redundant and duplicate lines.
- **Zero Aliases**: All old function names were completely migrated at every call site.
- **Pass Rate**: 100% test pass rate preserved across all three microservices (280 coordinator tests, 165 inventory tests, 70 payment tests, and 7/7 characterization tests).
- **Branch Cleanliness**: `master` and `learning` remain pristine at their exact start SHAs.
