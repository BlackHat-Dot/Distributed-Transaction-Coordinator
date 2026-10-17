# Assumption Ledger

## A1: requests.Response Truthiness
- **Claim**: `requests.Response` truthiness: `requests.Response` instances with status code 503 evaluate to `False` in boolean contexts (`bool(r) == False`), and `r.ok == False`.
- **Command**:
  ```bash
  /home/vijay/DTC/venv/bin/python -c "import requests; r=requests.Response(); r.status_code=503; print(bool(r), r.ok); print('requests.__version__:', requests.__version__)"
  ```
- **Printed Output**:
  ```text
  False False
  requests.__version__: 2.34.2
  ```
- **Verdict**: CONFIRMED
- **Action**: Replace `if confirm_resp and ...` with `if confirm_resp is not None and ...` in `coordinator/views.py`.

---

## A2: DRF Response Truthiness
- **Claim**: With `DJANGO_SETTINGS_MODULE` set and `django.setup()` called, `bool(rest_framework.response.Response({}))` evaluates to `True`.
- **Command**:
  ```bash
  /home/vijay/DTC/venv/bin/python -c "import os; os.environ['DJANGO_SETTINGS_MODULE']='coordinator_service.settings'; os.environ['DEBUG']='True'; import django; django.setup(); import rest_framework.response; r=rest_framework.response.Response({}); print('bool(Response({})):', bool(r))"
  ```
- **Printed Output**:
  ```text
  bool(Response({})): True
  ```
- **Verdict**: CONFIRMED
- **Action**: Replace truthiness checks with `is not None` only where the value is confirmed to be a Response object.

---

## A3: saga_log._set_status_with_event Expected Status Handling
- **Claim**: `saga_log._set_status_with_event` accepts both `list` and `tuple` for `expected_status` without error.
- **Command**:
  ```bash
  grep -n -A 30 "def _set_status_with_event" coordinator-service/coordinator/saga_log.py
  ```
- **Printed Output**:
  ```python
  177:def _set_status_with_event(
  178-    transaction_id,
  179-    to_status,
  180-    expected_status=None,
  181-    actor=SagaEvent.ACTOR_REQUEST,
  182-    http_status_code=None,
  183-    http_payload=None,
  184-):
  185-    with db_transaction.atomic():
  186-        qs = Transaction.objects.select_for_update().filter(
  187-            transaction_id=transaction_id
  188-        )
  189-        if expected_status:
  190-            if isinstance(expected_status, (list, tuple, set)):
  191-                qs = qs.filter(status__in=expected_status)
  192-            else:
  193-                qs = qs.filter(status=expected_status)
  ```
- **Verdict**: REFUTED (The assumption that passing a list breaks something is refuted; line 190 explicitly checks `isinstance(expected_status, (list, tuple, set))` and handles all three identically).
- **Action**: Leave `expected_status` as-is; do not change list to tuple since `(list, tuple, set)` are all natively supported.

---

## A4: recovery.execute_compensation_sweep Return Dict
- **Claim**: `recovery.execute_compensation_sweep` returns a dict with `"status_code"` on every return path, so `outcome["status_code"]` in `views.py` never raises `KeyError`.
- **Command**:
  ```bash
  grep -n -A 100 "def execute_compensation_sweep" coordinator-service/coordinator/recovery.py
  sed -n '120,370p' coordinator-service/coordinator/recovery.py
  grep -n -C 5 'outcome\["status_code"\]' coordinator-service/coordinator/views.py
  ```
- **Printed Output**:
  All 7 return statements in `execute_compensation_sweep` return `"status_code"`:
  - Line 92: `"status_code": 503`
  - Line 155: `"status_code": 503`
  - Line 198: `"status_code": 200`
  - Line 219: `"status_code": 503`
  - Line 260: `"status_code": 409`
  - Line 339: `"status_code": 200`
  - Line 358: `"status_code": 500`
  In `views.py`:
  - Line 466: `return Response(response_data, status=outcome["status_code"])`
- **Verdict**: CONFIRMED
- **Action**: Safe as-is.

---

## A5: settings.py Defaults and Overrides
- **Claim**: Settings defaults and development/testing bypass flags across `settings.py`, `docker-compose.yml`, and `.env.example`.
- **Command**:
  ```bash
  grep -nE "IS_TESTING|DEBUG|INTERNAL_SERVICE_KEY|ALLOW_DEV_STAFF_BYPASS|CELERY_BROKER_URL|ENABLE_TEST_PAYMENT_FAILURE|ENABLE_FAULT_INJECTION|INVENTORY_SERVICE_URL|PAYMENT_SERVICE_URL" coordinator-service/coordinator_service/settings.py
  grep -nE "DEBUG|IS_TESTING" docker-compose* .env.example
  ```
- **Printed Output**:
  ```text
  coordinator-service/coordinator_service/settings.py:
  9:DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")
  15:IS_TESTING = 'test' in sys.argv
  20:    if DEBUG or IS_TESTING:
  48:INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
  51:if not INTERNAL_SERVICE_KEY:
  52:    if DEBUG or IS_TESTING:
  53:        INTERNAL_SERVICE_KEY = "dtc-internal-coordinator-secret-key-2026"
  64:ALLOW_DEV_STAFF_BYPASS = os.getenv("ALLOW_DEV_STAFF_BYPASS", "False").lower() in ("true", "1", "t", "yes")
  70:ENABLE_TEST_PAYMENT_FAILURE = os.getenv("ENABLE_TEST_PAYMENT_FAILURE", "False").lower() in ("true", "1", "t")
  270:CELERY_BROKER_URL = os.getenv(...)
  276:            os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")

  docker-compose.yml:
  46:      DEBUG: ${DEBUG:-False}
  77:      DEBUG: ${DEBUG:-False}
  110:      DEBUG: ${DEBUG:-False}

  .env.example:
  27:# In development, DEBUG=True enables auto-mocked OTPs (no external email needed).
  28:# In production, DEBUG=False strictly requires unique SECRET_KEY and INTERNAL_SERVICE_KEY.
  29:DEBUG=True
  ```
- **Verdict**: CONFIRMED
- **Action**: Report identity bypasses under Part 3.

---

## A6: Circular Imports between views.py and tasks.py / recovery.py
- **Claim**: If tasks.py and recovery.py have no imports from views, local imports of execute_saga_task and get_redis_client in views are not required.
- **Command**:
  ```bash
  grep -nE "views|coordinator\.views" coordinator-service/coordinator/tasks.py coordinator-service/coordinator/recovery.py
  ```
- **Printed Output**:
  ```text
  coordinator-service/coordinator/tasks.py:67:    from coordinator.views import _run_saga
  ```
- **Verdict**: REFUTED (The condition "If none" is false; `coordinator/tasks.py` line 67 imports `from coordinator.views import _run_saga`).
- **Action**: Preserve local imports in functions where needed to prevent circular import problems.

---

## A7: Dead Code Analysis
- **Claim**: `list_transactions`, `transactions_endpoint`, `_run_saga`, `is_sync=`, and `, **kwargs` are dead code.
- **Command**:
  ```bash
  grep -rnE "list_transactions|transactions_endpoint|_run_saga|is_sync=|,\s*\*\*kwargs" coordinator-service/
  ```
- **Printed Output**:
  - `transactions_endpoint`: routed in `coordinator_service/urls.py:42: path("transactions/", transactions_endpoint)`
  - `list_transactions`: routed in `coordinator_service/urls.py:48, 49: path("transactions/all/", list_transactions)` and `path("transactions/feed/", list_transactions)`; tested in `test_audit_hardening.py` and `test_coverage_boost.py`
  - `_run_saga`: called in `tasks.py:77` (`response = _run_saga(...)`), `views.py:1025`, `views.py:1121`, and tested in `test_a3_concurrency.py`, `test_audit_hardening.py`, `test_coverage_boost.py`
  - `is_sync=`: passed in `test_coverage_boost.py:426: is_sync=True`
  - `, **kwargs`: used across multiple handlers and wrappers
- **Verdict**: REFUTED (None of these are dead code).
- **Action**: Do not delete these items. In C1, only clean up blank lines and formatting.

---

## A8: Mocks in Test Suite
- **Claim**: Tests mock `_get_caller` returning plain tuples. Tests mock `requests.post`/`get` under `IS_TESTING=False`.
- **Command**:
  ```bash
  grep -rnE "_get_caller" coordinator-service/coordinator/tests/
  grep -rn -B 3 -A 5 "IS_TESTING=False" coordinator-service/coordinator/tests/
  ```
- **Printed Output**:
  - `_get_caller` plain tuple returns:
    - `test_b1_ownership.py:50`: `return_value=(True, "usr_alice", False)`
    - `test_audit_hardening.py:700`: `return_value=(True, "user_eve", True)`
    - `test_coverage_boost.py:435, 459, 482, 492`: `return_value=(False, None, False)`
  - `requests.post` / `get` mocks under `IS_TESTING=False`: 0 hits found in test suite.
- **Verdict**: CONFIRMED for `_get_caller` plain tuple mocks; 0 hits for requests mocks under `IS_TESTING=False`.
- **Action**: Ensure `RequesterIdentity` retains backward compatibility with 3-tuple unpacking.

---

## A9: Environment Overrides in Tests and Dynamic Call-Time Reads
- **Claim**: `ALLOW_DEV_STAFF_BYPASS`, `ENABLE_FAULT_INJECTION`, `MAX_SEATS_PER_TRANSACTION`, `RECOVERY_MIN_THRESHOLD_SECONDS`, `ADMIN_DOMAIN`, `STAFF_API_KEY` are tested in tests and read at call time in `views.py`.
- **Command**:
  ```bash
  grep -rnE "ALLOW_DEV_STAFF_BYPASS|ENABLE_FAULT_INJECTION|MAX_SEATS_PER_TRANSACTION|RECOVERY_MIN_THRESHOLD_SECONDS|ADMIN_DOMAIN|STAFF_API_KEY" coordinator-service/coordinator/tests/
  ```
- **Printed Output**:
  Tested in `test_b3_admin_domain.py`, `test_a1_retry_compensation.py`, `test_auth_otp.py`, `test_audit_hardening.py`, `test_b2_staff_bypass.py`, `test_b4_fault_injection_and_secrets.py`.
  Read dynamically at call time in `coordinator/views.py`:
  - `ALLOW_DEV_STAFF_BYPASS`: line 2365
  - `ENABLE_FAULT_INJECTION`: line 80
  - `MAX_SEATS_PER_TRANSACTION`: line 649
  - `RECOVERY_MIN_THRESHOLD_SECONDS`: line 503
  - `ADMIN_DOMAIN`: line 64
  - `STAFF_API_KEY`: line 52
- **Verdict**: CONFIRMED
- **Action**: Retain dynamic call-time reading for these environment variables.

---

## A10: Confirm Retry Gateway Bug
- **Claim**: In `coordinator/views.py:1682`, `confirm_resp and confirm_resp.status_code in (502, 503, 504)` evaluates `bool(confirm_resp)`, which is `False` for `requests.Response` with status 503. Thus confirm is never retried on gateway error.
- **Command**:
  ```bash
  /home/vijay/DTC/venv/bin/python /home/vijay/DTC/coordinator-service/manage.py test coordinator.tests.test_retry_gateway --noinput
  ```
- **Printed Output**:
  ```text
  FAIL: test_confirm_retry_on_503_gateway_error (coordinator.tests.test_retry_gateway.RetryGatewayTests.test_confirm_retry_on_503_gateway_error)
  AssertionError: 1 != 2 : Expected confirm called twice, but called 1 time(s)
  Ran 1 test in 0.043s
  FAILED (failures=1)
  EXIT=1
  ```
- **Verdict**: CONFIRMED
- **Action**: Commit failing test in C3. Fix check to `confirm_resp is not None and confirm_resp.status_code in (502, 503, 504)` in C4.

---

## A11: Expired-Hold Exception Swallowing on Pay Path
- **Claim**: The `try/except` block around `held_until` in `views.py` encloses date parsing, status transition, inventory release, and return 409 response. Any exception is caught and swallowed by `except Exception: pass`, falling through to charge the user.
- **Command**:
  ```bash
  sed -n '921,949p' coordinator-service/coordinator/views.py
  ```
- **Printed Output**:
  ```python
            if holding_event and holding_event.http_payload:
                held_until_raw = holding_event.http_payload.get("held_until")
                if held_until_raw:
                    try:
                        held_until_dt = parse_datetime(held_until_raw) if isinstance(held_until_raw, str) else held_until_raw
                        if held_until_dt and timezone.is_naive(held_until_dt):
                            held_until_dt = timezone.make_aware(held_until_dt)
                        if held_until_dt and held_until_dt < timezone.now():
                            _set_status_with_event(
                                txn.transaction_id,
                                Transaction.Status.TIMEOUT,
                                expected_status=Transaction.Status.HOLDING,
                                actor=SagaEvent.ACTOR_REQUEST,
                                http_status_code=409,
                                http_payload={"error": "Seat hold expired before payment attempt"},
                            )
                            _release_inventory(txn.transaction_id, txn.seat_ids, correlation_id, "for expired hold")
                            return Response(
                                {
                                    "error": "Seat hold has expired. Please select seats and try again.",
                                    "code": "HOLD_EXPIRED",
                                    "status": Transaction.Status.TIMEOUT,
                                    "transaction_id": str(txn.transaction_id),
                                },
                                status=409,
                            )
                    except Exception:
                        pass
  ```
- **Verdict**: CONFIRMED
- **Action**: In C5, write a test verifying that if `_set_status_with_event` raises during timeout handling, charge is not reached. In C6, narrow the try block to cover only date parsing.

---

## A12: One-Shot Reserve Failure Without Inventory Release
- **Claim**: `_create_txn` (hold) calls `_release_inventory` on reserve failure, but `_run_saga` (one-shot) does not call `_release_inventory` on reserve failure. Tests do not expect release on one-shot reserve failure.
- **Command**:
  ```bash
  sed -n '800,835p' coordinator-service/coordinator/views.py
  sed -n '1300,1330p' coordinator-service/coordinator/views.py
  grep -rnE "release.*reserve|reserve.*release" coordinator-service/coordinator/tests/
  ```
- **Printed Output**:
  - `_create_txn`: lines 806 and 821 call `_release_inventory`
  - `_run_saga`: lines 1307 and 1310 return immediately without calling `_release_inventory`
  - Tests: 0 hits for one-shot release on reserve failure
- **Verdict**: CONFIRMED
- **Action**: Report under Part 3 (do not change).
