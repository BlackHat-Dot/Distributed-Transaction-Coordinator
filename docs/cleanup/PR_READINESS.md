# Pull Request Readiness Assessment

## 1. Commit 2165c20 on master

```
$ git show 2165c20 | grep "^+"
+++ b/coordinator-service/coordinator/views.py
+
```

### Surrounding Context
```python
328:                         if db_txn and db_txn.status in terminal_statuses:
329:                             term_msg = json.dumps({
330:                                 "transaction_id": str(db_txn.transaction_id),
331:                                 "status": db_txn.status,
332:                                 "is_terminal": True,
333:                                 "needs_recovery": db_txn.status in Transaction.RECOVERABLE_STATUSES,
334:                             })
335:                             yield f"data: {term_msg}\n\n"
336:                             break
337: 
338: 
339:         except Exception as exc:
340:             logger.warning("SSE stream error for tx %s: %s", transaction_id, exc)
341:             err_msg = json.dumps({
342:                 "transaction_id": str(transaction_id),
343:                 "status": "STREAM_ERROR",
344:                 "is_terminal": False,
345:                 "error": str(exc),
```

### Analysis & Verdict
The added line in commit `2165c20` is an empty blank line in `coordinator-service/coordinator/views.py` (line 338). In Python syntax, blank lines outside multi-line string literals do not alter the Abstract Syntax Tree (AST), bytecode, or runtime behavior. It is **harmless**.

---

## 2. Payment File in Revert Commit (`payment-service/payments/views.py`)

### Git Show & Diff Outputs
```
$ git show c29c3af --stat -- payment-service/payments/views.py
 payment-service/payments/views.py | 335 +++++++++++++++++++++-----------------
 1 file changed, 187 insertions(+), 148 deletions(-)

$ git diff 5a8aa78 HEAD --stat -- payment-service/payments/views.py
(empty)

$ git diff -w --stat 5a8aa78 HEAD -- payment-service/payments/views.py
(empty)

$ git diff --ignore-all-space --ignore-blank-lines 5a8aa78 HEAD -- payment-service/payments/views.py
(empty)
```

### Line Ending Checks
```
$ git show 5a8aa78:payment-service/payments/views.py | Select-String -Pattern "`r" -SimpleMatch | Measure-Object
Count    : 0

$ git show HEAD:payment-service/payments/views.py | Select-String -Pattern "`r" -SimpleMatch | Measure-Object
Count    : 0
```

The diff between `5a8aa78` and `HEAD` for `payment-service/payments/views.py` is completely empty. The file has been restored to its exact pre-refactor state.

---

## 3. Stray File Audit (`demo.py`)

### Inspection
```
$ git show feasible:demo.py
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)

logger.debug("Debug message")
logger.info("Application started")
logger.warning("Something unexpected happened")
logger.error("An error occurred")

$ git log --oneline -- demo.py
e696d32 NEw branch status

$ git grep -n "demo.py"
docs/cleanup/PR_READINESS.md:127:- `demo.py`
```

### Decision & Action
`demo.py` contained only 13 lines demonstrating standard library logging setup. It was unreferenced by any application service, script, test, or documentation, and contained no credentials or required logic. It was deleted in commit `7568c63` (`chore: remove stray demo.py`).

---

## 4. `DEBUG` and `IS_TESTING` in Settings & Production Config

### Settings Analysis
- **`coordinator-service/coordinator_service/settings.py`**:
  - Line 9: `DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")`
    - Value when unset: `False`
  - Line 15: `IS_TESTING = 'test' in sys.argv`
    - Value when unset / serving: `False`
- **`inventory-service/inventory_service/settings.py`**:
  - Line 8: `DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")`
    - Value when unset: `False`
  - Line 14: `IS_TESTING = 'test' in sys.argv`
    - Value when unset / serving: `False`
- **`payment-service/payment_service/settings.py`**:
  - Line 8: `DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")`
    - Value when unset: `False`
  - Line 14: `IS_TESTING = 'test' in sys.argv`
    - Value when unset / serving: `False`

### Deployment & Container Configuration
- **`docker-compose.yml`**:
  - Line 46 (`inventory`): `DEBUG: ${DEBUG:-False}` (defaults to `False`)
  - Line 77 (`payment`): `DEBUG: ${DEBUG:-False}` (defaults to `False`)
  - Line 110 (`coordinator`): `DEBUG: ${DEBUG:-False}` (defaults to `False`)
- **`Dockerfile` / `Dockerfile.*`**: Zero occurrences setting `DEBUG` or `IS_TESTING`.
- **`.env.example`**:
  - Line 28: `DEBUG=True` (provided for local single-machine development and mock OTP generation).

---

## 5. Full Branch Review Scope (`master...feasible`)

### Commits: `master..feasible`
```
7568c63 chore: remove stray demo.py
0c4ea80 docs: PR readiness check
c29c3af Revert "chore: remove duplicate os.getenv fallbacks in favor of django settings"
6b435a0 Revert "refactor: remove _SEATS_SEEDED in favor of seat count check"
577b2f8 refactor: remove _SEATS_SEEDED in favor of seat count check
dffc696 chore: remove duplicate os.getenv fallbacks in favor of django settings
5a8aa78 chore: remove unused COORDINATOR_SERVICE_URL from .env.example
6592a68 chore: remove unused unittest import in retry gateway test
915e4b7 fix: retry inventory confirm on gateway errors
6cabeea fix: let malformed total_amount fail loudly instead of being ignored
9ef91e3 fix: isolate total_amount parsing from database update and handle InvalidOperation in _run_saga
df10e43 fix: isolate total_amount parsing from database update and handle InvalidOperation in hold branch
154ae12 fix: do not swallow errors from expired-hold transition
c358d6a chore: trim blank lines and unused imports in coordinator views (no behavior change)
65dcfd4 test: mark known confirm retry bug as expected failure
55ac31b Code line reduced
e8cf121 fix: restore tuple-compatible identity and exact action/admin domain handling
6698840 refactor: remove redundant lines in coordinator views (no behavior change)
c73f6b6 docs: regenerate report from live run
9b76d58 fix: restore original service URL resolution
3d3f444 docs: generated verification report
8b8c998 docs: correct verification evidence (round 3)
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
f24b471 learning branch update
e696d32 NEw branch status
```

### Commits: `feasible..master` (1 commit)
```
2165c20 New branch added
```

### Changed Files Breakdown (68 files with non-whitespace changes)

#### Code (13 files)
- `coordinator-service/coordinator/auth_views.py`
- `coordinator-service/coordinator/management/commands/recovery_stuck_transactions.py`
- `coordinator-service/coordinator/middleware.py`
- `coordinator-service/coordinator/recovery.py`
- `coordinator-service/coordinator/saga_log.py`
- `coordinator-service/coordinator/tasks.py`
- `coordinator-service/coordinator/ticket_views.py`
- `coordinator-service/coordinator/views.py` (about 1146 changed lines)
- `inventory-service/inventory/management/commands/seed_demo_seats.py`
- `inventory-service/inventory/views.py`
- `payment-service/payments/views.py`
- `scripts/run_real_flow_e2e.py`
- `docs/cleanup/make_report.py`

#### Tests (41 files)
- `coordinator-service/coordinator/tests/test_a2_cas_checks.py`
- `coordinator-service/coordinator/tests/test_a3_concurrency.py`
- `coordinator-service/coordinator/tests/test_a5_payment_5xx.py`
- `coordinator-service/coordinator/tests/test_a6_request_exceptions.py`
- `coordinator-service/coordinator/tests/test_a7_reserve_order.py`
- `coordinator-service/coordinator/tests/test_a8_cleanup.py`
- `coordinator-service/coordinator/tests/test_audit_hardening.py`
- `coordinator-service/coordinator/tests/test_auth_otp.py`
- `coordinator-service/coordinator/tests/test_b1_ownership.py`
- `coordinator-service/coordinator/tests/test_b2_staff_bypass.py`
- `coordinator-service/coordinator/tests/test_b3_admin_domain.py`
- `coordinator-service/coordinator/tests/test_b4_fault_injection_and_secrets.py`
- `coordinator-service/coordinator/tests/test_characterization.py`
- `coordinator-service/coordinator/tests/test_coordinator.py`
- `coordinator-service/coordinator/tests/test_coverage_boost.py`
- `coordinator-service/coordinator/tests/test_exit_gate_gaps.py`
- `coordinator-service/coordinator/tests/test_expired_hold_errors.py`
- `coordinator-service/coordinator/tests/test_f1_checkout_presence.py`
- `coordinator-service/coordinator/tests/test_g1_real_heartbeat_presence.py`
- `coordinator-service/coordinator/tests/test_g8_coordinator_checkout_presence_resilience.py`
- `coordinator-service/coordinator/tests/test_h5_admin_reset_prohibits_refund.py`
- `coordinator-service/coordinator/tests/test_retry_gateway.py`
- `coordinator-service/coordinator/tests/test_ticket_email.py`
- `coordinator-service/coordinator/tests/test_ticket_endpoints.py`
- `coordinator-service/coordinator/tests/test_ticket_issuance.py`
- `coordinator-service/coordinator/tests/test_ticket_pdf_qr.py`
- `coordinator-service/coordinator/tests/test_total_amount_hold_parse.py`
- `coordinator-service/coordinator/tests/test_total_amount_saga_parse.py`
- `inventory-service/inventory/tests/test_g3_settings_audit.py`
- `inventory-service/inventory/tests/test_g4_throttling_hardening.py`
- `inventory-service/inventory/tests/test_g5_startup_guard.py`
- `inventory-service/inventory/tests/test_g6_released_reason.py`
- `inventory-service/inventory/tests/test_h4_throttle_limits.py`
- `inventory-service/inventory/tests/test_h5_released_reason_legacy.py`
- `inventory-service/inventory/tests/test_hardening_p0.py`
- `inventory-service/inventory/tests/test_hardening_p2.py`
- `inventory-service/inventory/tests/test_phase1.py`
- `inventory-service/inventory/tests/test_phase3.py`
- `scripts/run_playwright_test.py`
- `tests/test_gate_scanner.py`
- `tests/test_nonblocking_load.py`

#### Docs (8 files)
- `COORDINATOR_CHANGE.md`
- `README.md`
- `docs/cleanup/CLEANUP_PLAN.md`
- `docs/cleanup/CLEANUP_REPORT.md`
- `docs/cleanup/ENV_MAP.md`
- `docs/cleanup/PR_READINESS.md`
- `docs/cleanup/RENAME_MAP.md`
- `flowchart.mmd`

#### Config (2 files)
- `.env.example`
- `.gitignore`

#### Audit (4 files)
- `audit/coverage.md`
- `audit/final_report.md`
- `audit/findings.md`
- `audit/plan_revisions.md`

---

## 6. Test Suite Execution Results

```
=== coordinator ===
Ran 284 tests in 26.706s
OK
EXIT=0

=== coordinator.tests.test_characterization ===
Ran 7 tests in 0.457s
OK
EXIT=0

=== inventory ===
Ran 165 tests in 5.011s
OK
EXIT=0

=== payments ===
Ran 70 tests in 1.502s
OK
EXIT=0
```

---

## 7. Open Items

1. **Learning-branch commits in history**: Commits `f24b471` ("learning branch update") and `e696d32` ("NEw branch status") reside on the branch history.
2. **Review scope**: 68 changed files in `master...feasible`, with `coordinator-service/coordinator/views.py` accounting for ~1146 changed lines requiring thorough PR review.
3. **Seat leak after reserve error**: Edge case where errors during downstream reserve operations could leave seats unreleased if the initial atomic block fails mid-transaction.
4. **Retry is single-attempt**: Gateway retry mechanisms currently execute a single retry attempt on 502/503/504 rather than an exponential backoff loop.
5. **SSE path not tested live with Redis**: Server-Sent Events (SSE) streaming paths are mocked with in-process queues in unit tests rather than an end-to-end integration test against a live Redis cluster.

---

VERDICT: READY FOR PULL REQUEST REVIEW, NOT FOR MERGE
