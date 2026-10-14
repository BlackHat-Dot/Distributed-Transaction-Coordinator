# Verification Log: DTC Codebase Cleanup (Round 3)

This log records the exact commands, working directories, exit codes, and raw output blocks printed in chat during verification on branch `feasible`.

---

## Step 1: Verify the Env Commit (4d71742)

### 1a. Git Show of Commit 4d71742
- **Command**: `git show 4d71742 -- coordinator-service/coordinator/views.py`
- **Working Directory**: `C:\Users\MADHAVAN\Downloads\DTC`
- **Exit Code**: 0
- **Output**:
```diff
commit 4d71742a770a05bc54d0a0215053ca02b897e68b
Author: BlackHat-Dot <vijayzidane321@gmail.com>
Date:   Wed Oct 14 18:40:35 2026 +0530

    fix: read env at call time where tests change it

diff --git a/coordinator-service/coordinator/views.py b/coordinator-service/coordinator/views.py
index 9d86ab2..3ab473f 100644
--- a/coordinator-service/coordinator/views.py
+++ b/coordinator-service/coordinator/views.py
@@ -52,19 +52,6 @@ PAYMENT_VOID_URL = f"{PAYMENT_BASE_URL}/payments/void/"
 
 # Shared secret for service-to-service internal authentication (X-Internal-Service-Key)
 INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")
-# Secret key for human administrative staff access (X-Staff-Key)
-STAFF_API_KEY = os.getenv("STAFF_API_KEY", "").strip()
-
-# Minimum age in seconds before stuck transactions are swept in production
-RECOVERY_MIN_THRESHOLD = int(os.getenv("RECOVERY_MIN_THRESHOLD_SECONDS", "30"))
-# Maximum seats permitted per transaction
-MAX_SEATS = int(os.getenv("MAX_SEATS_PER_TRANSACTION", "10"))
-# Domain treated as staff when authenticated via email
-ADMIN_DOMAIN = os.getenv("ADMIN_DOMAIN", "").strip().lower()
-# Default user ID for staff API key requests
-DEFAULT_STAFF_USER_ID = os.getenv("STAFF_USER_ID", "system_staff")
-# Default user email for staff API key requests
-DEFAULT_STAFF_EMAIL = os.getenv("STAFF_EMAIL_ID", "admin@dtc.internal")
 
 
 def _corr_id(correlation_id=None):
@@ -78,7 +65,7 @@ def _verify_staff_key(header_val):
     """Constant-time verification of X-Staff-Key header."""
     if not header_val or not isinstance(header_val, str) or not header_val.strip():
         return False
-    staff_key = os.getenv("STAFF_API_KEY", "").strip() or STAFF_API_KEY
+    staff_key = os.getenv("STAFF_API_KEY", "").strip()
     if not staff_key:
         return False
     return hmac.compare_digest(header_val.strip().encode("utf-8"), staff_key.encode("utf-8"))
@@ -659,9 +646,10 @@ def _validate_seats(seats):
         return Response({"error": "seat_ids must be a list of integers.", "code": "INVALID_SEAT_IDS"}, status=400)
     if len(seats) != len(set(seats)):
         return Response({"error": "Duplicate seat_ids are not allowed.", "code": "DUPLICATE_SEATS"}, status=400)
-    if len(seats) > MAX_SEATS:
+    max_seats = int(os.getenv("MAX_SEATS_PER_TRANSACTION", "10"))
+    if len(seats) > max_seats:
         return Response(
-            {"error": f"Cannot reserve more than {MAX_SEATS} seats in a single transaction.", "code": "EXCEEDED_MAX_SEATS"},
+            {"error": f"Cannot reserve more than {max_seats} seats in a single transaction.", "code": "EXCEEDED_MAX_SEATS"},
             status=400,
         )
     return None
@@ -1997,7 +1985,7 @@ def _is_admin_email(email):
     """Check if email belongs to the configured ADMIN_DOMAIN."""
     if not email or not isinstance(email, str):
         return False
-    admin_domain = os.getenv("ADMIN_DOMAIN", "").strip().lower() or ADMIN_DOMAIN
+    admin_domain = os.getenv("ADMIN_DOMAIN", "").strip().lower()
     if not admin_domain:
         return False
     if admin_domain.startswith("@"):
@@ -2026,8 +2014,8 @@ class RequesterIdentity(tuple):
 def _get_caller(request):
     """Resolve caller authentication and staff privileges."""
     if _verify_staff_key(request.headers.get("X-Staff-Key")):
-        user_id = request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or DEFAULT_STAFF_USER_ID
-        user_email = request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or DEFAULT_STAFF_EMAIL
+        user_id = request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or "system_staff"
+        user_email = request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"
         return RequesterIdentity(True, user_id, True, user_email=user_email)
 
     user_info = get_user_from_request(request)
```

### 1b. Call-Time vs Import-Time Audit of Changed Env Reads
1. `STAFF_API_KEY`: Line 68 `staff_key = os.getenv("STAFF_API_KEY", "").strip()` in `_verify_staff_key` -> **call-time** (removed import-time line 56).
2. `RECOVERY_MIN_THRESHOLD_SECONDS`: Line 503 `min_floor = 5 if getattr(settings, "DEBUG", False) else int(os.getenv("RECOVERY_MIN_THRESHOLD_SECONDS", "30"))` -> **call-time** (removed import-time line 59).
3. `MAX_SEATS_PER_TRANSACTION`: Line 649 `max_seats = int(os.getenv("MAX_SEATS_PER_TRANSACTION", "10"))` in `_validate_seats` -> **call-time** (removed import-time line 61).
4. `ADMIN_DOMAIN`: Line 1988 `admin_domain = os.getenv("ADMIN_DOMAIN", "").strip().lower()` in `_is_admin_email` -> **call-time** (removed import-time line 63).
5. `STAFF_USER_ID`: Line 2017 `user_id = request.headers.get("X-User-ID") or os.getenv("STAFF_USER_ID") or "system_staff"` in `_get_caller` -> **call-time** (removed import-time line 65).
6. `STAFF_EMAIL_ID`: Line 2018 `user_email = request.headers.get("X-User-Email") or os.getenv("STAFF_EMAIL_ID") or "admin@dtc.internal"` in `_get_caller` -> **call-time** (removed import-time line 67).

### 1c. Feasible Coordinator Test Run (Post-Commit)
- **Command**: `python "C:\Users\MADHAVAN\Downloads\DTC\coordinator-service\manage.py" test coordinator --noinput`
- **Working Directory**: `C:\Users\MADHAVAN\Downloads\DTC`
- **Exit Code**: 0
- **Output (last lines and Ran line)**:
```text
Recovery sweep complete.
  -> a9f4d1e7-f016-4fc2-8919-56240b4d43a9: compensated successfully.
  STARTED 7d31c884-362b-4214-a673-fda6ce8c2ecc: already claimed, skipping.
Recovering f09c11be-90c7-4276-91cd-cf1cf83411aa claimed from COMPENSATING...
  -> f09c11be-90c7-4276-91cd-cf1cf83411aa: compensated successfully.
Recovery sweep complete.
Recovering 2374a97c-c9c2-4d44-bc38-805c7e8f7dee claimed from COMPENSATION_FAILED...
  -> 2374a97c-c9c2-4d44-bc38-805c7e8f7dee: compensation partially failed; will retry next sweep.
Recovery sweep complete.
Recovering 14a976f4-7d98-47e7-8621-ed440fd6a765 claimed from COMPENSATION_FAILED...
  -> 14a976f4-7d98-47e7-8621-ed440fd6a765: compensated successfully.
Recovery sweep complete.

[OLD CODE] thread results=[1, 0], final status='COMPENSATED_ON_RETRY', saga_events=0. No audit trail. Loser detected by 0-rows-updated, not by a lock.

Ran 280 tests in 28.230s
OK
```

---

## Step 2: Characterization Mapping From Grep

### 2b. Baseline Grep Output
- **Command**: `git -C ..\dtc_baseline grep -n -E "Status\.(HOLDING|INVENTORY_RESERVE_FAILED|COMPLETED|COMPENSATED|TIMEOUT)|HOLD_EXPIRED|ALLOWED_RETRY_STATUSES|FORBIDDEN" -- coordinator-service/coordinator/views.py`
- **Working Directory**: `C:\Users\MADHAVAN\Downloads\DTC`
- **Exit Code**: 0
- **Output**:
```text
coordinator-service/coordinator/views.py:391:ALLOWED_RETRY_STATUSES = (
coordinator-service/coordinator/views.py:421:    if txn.status in (Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY):
coordinator-service/coordinator/views.py:429:    if txn.status not in ALLOWED_RETRY_STATUSES:
coordinator-service/coordinator/views.py:984:                Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1019:                Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1048:            Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1067:                "status": Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1096:                    {"error": "You do not have permission to pay for this transaction.", "code": "FORBIDDEN"},
coordinator-service/coordinator/views.py:1101:            if txn.status == Transaction.Status.COMPLETED:
coordinator-service/coordinator/views.py:1105:            if txn.status != Transaction.Status.HOLDING:
coordinator-service/coordinator/views.py:1120:                    to_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1136:                                Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1137:                                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1166:                                    "code": "HOLD_EXPIRED",
coordinator-service/coordinator/views.py:1167:                                    "status": Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1188:                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1236:                    to_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1245:                        "status": Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1366:                to_status=Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1375:                    "status": Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1501:        if current_status == Transaction.Status.HOLDING:
coordinator-service/coordinator/views.py:1507:                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1579:            Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1596:                "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1996:                Transaction.Status.COMPLETED,
coordinator-service/coordinator/views.py:2021:                "status": Transaction.Status.COMPLETED,
coordinator-service/coordinator/views.py:2207:            Transaction.Status.COMPENSATED,
coordinator-service/coordinator/views.py:2228:            "status": Transaction.Status.COMPENSATED,
coordinator-service/coordinator/views.py:2273:        cause = _transport_status(active_err, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
```

### 2c. Feasible Grep Output
- **Command**: `git -C "C:\Users\MADHAVAN\Downloads\DTC" grep -n -E "Status\.(HOLDING|INVENTORY_RESERVE_FAILED|COMPLETED|COMPENSATED|TIMEOUT)|HOLD_EXPIRED|ALLOWED_RETRY_STATUSES|FORBIDDEN" -- coordinator-service/coordinator/views.py`
- **Working Directory**: `C:\Users\MADHAVAN\Downloads\DTC`
- **Exit Code**: 0
- **Output**:
```text
coordinator-service/coordinator/views.py:360:ALLOWED_RETRY_STATUSES = (
coordinator-service/coordinator/views.py:386:    if txn.status in (Transaction.Status.COMPENSATED, Transaction.Status.COMPENSATED_ON_RETRY):
coordinator-service/coordinator/views.py:393:    if txn.status not in ALLOWED_RETRY_STATUSES:
coordinator-service/coordinator/views.py:808:                Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:823:                Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:851:            Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:870:                "status": Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:895:                    {"error": "You do not have permission to pay for this transaction.", "code": "FORBIDDEN"},
coordinator-service/coordinator/views.py:899:            if txn.status == Transaction.Status.COMPLETED:
coordinator-service/coordinator/views.py:902:            if txn.status != Transaction.Status.HOLDING:
coordinator-service/coordinator/views.py:916:                    to_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:931:                                Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:932:                                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:941:                                    "code": "HOLD_EXPIRED",
coordinator-service/coordinator/views.py:942:                                    "status": Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:962:                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1010:                    to_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1019:                        "status": Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1106:                to_status=Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1115:                    "status": Transaction.Status.TIMEOUT,
coordinator-service/coordinator/views.py:1224:        if current_status == Transaction.Status.HOLDING:
coordinator-service/coordinator/views.py:1228:                expected_status=Transaction.Status.HOLDING,
coordinator-service/coordinator/views.py:1297:            Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1314:                "error": Transaction.Status.INVENTORY_RESERVE_FAILED,
coordinator-service/coordinator/views.py:1699:                Transaction.Status.COMPLETED,
coordinator-service/coordinator/views.py:1724:                "status": Transaction.Status.COMPLETED,
coordinator-service/coordinator/views.py:1905:            Transaction.Status.COMPENSATED,
coordinator-service/coordinator/views.py:1926:            "status": Transaction.Status.COMPENSATED,
coordinator-service/coordinator/views.py:1971:        cause = _err_status(active_err, Transaction.Status.TIMEOUT, Transaction.Status.CONNECTION_FAILED)
```

### 2d. Assertion to Baseline Grep Line Mapping
1. `test_1_hold_success`:
   - `HTTP 200`: line 1064
   - `Transaction.Status.HOLDING`: line 1048 (`to_status`), line 1067 (`"status": Transaction.Status.HOLDING`)
2. `test_2_hold_with_reserve_failure`:
   - `HTTP 409`: line 1030
   - `code: SEAT_NOT_AVAILABLE`: line 1026
   - `Transaction.Status.INVENTORY_RESERVE_FAILED`: line 1019
3. `test_3_pay_success`:
   - `HTTP 200`: line 2018
   - `Transaction.Status.COMPLETED`: line 1996 (`to_status`), line 2021 (`"status": Transaction.Status.COMPLETED`)
4. `test_4_pay_with_payment_decline`:
   - `HTTP 400`: line 2226
   - `Transaction.Status.COMPENSATED`: line 2207 (`to_status`), line 2228 (`"status": Transaction.Status.COMPENSATED`)
5. `test_5_pay_on_expired_hold`:
   - `HTTP 409`: line 1170
   - `code: HOLD_EXPIRED`: line 1166
   - `Transaction.Status.TIMEOUT`: line 1136 (`to_status`), line 1167 (`"status": Transaction.Status.TIMEOUT`)
6. `test_6_retry_on_non_eligible_status`:
   - `HTTP 400`: line 429 (`if txn.status not in ALLOWED_RETRY_STATUSES:`), line 436 (`status=400`)
7. `test_7_idor_non_staff_denied_on_other_users_transaction`:
   - `GET HTTP 403`: line 179
   - `POST pay HTTP 403`: line 1097
   - `code: FORBIDDEN`: line 1096
- **Mismatches**: 0

### 2e. Characterization Test Runs in Baseline and Feasible
- **BASE Characterization Test**:
  - **Command**: `python "C:\Users\MADHAVAN\Downloads\dtc_baseline\coordinator-service\manage.py" test coordinator.tests.test_characterization --noinput`
  - **Working Directory**: `C:\Users\MADHAVAN\Downloads\dtc_baseline`
  - **Exit Code**: 0
  - **Output Summary**:
    ```text
    Ran 7 tests in 0.466s
    OK
    ```
- **FEASIBLE Characterization Test**:
  - **Command**: `python "C:\Users\MADHAVAN\Downloads\DTC\coordinator-service\manage.py" test coordinator.tests.test_characterization --noinput`
  - **Working Directory**: `C:\Users\MADHAVAN\Downloads\DTC`
  - **Exit Code**: 0
  - **Output Summary**:
    ```text
    Ran 7 tests in 0.471s
    OK
    ```

---

## Step 3: Ruff Linter

### 3a. Ruff Execution Exit Codes
- **BASE Ruff Check Exit Code**: `1`
- **FEASIBLE Ruff Check Exit Code**: `1`

### 3b. Ruff Violations Table (Parsed via Python utf-8-sig)
```text
Total Baseline Violations: 646
Total Feasible Violations: 584

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

## Step 4: Test Summaries (Get-Content -Tail 6 and Ran line)

### 1. BASE Coordinator (`base_coordinator.log`)
```text
Recovering a57ae8d9-b792-435b-b928-5f79388e54fd claimed from COMPENSATION_FAILED...
  -> a57ae8d9-b792-435b-b928-5f79388e54fd: compensated successfully.
Recovery sweep complete.

[OLD CODE] thread results=[1, 0], final status='COMPENSATED_ON_RETRY', saga_events=0. No audit trail. Loser detected by 0-rows-updated, not by a lock.
EXIT_CODE: 0
```
Ran line: `Ran 280 tests in 27.390s`

### 2. BASE Characterization (`base_characterization.log`)
```text
OK
Destroying test database for alias 'default'...

Found 7 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
Ran line: `Ran 7 tests in 0.447s`

### 3. BASE Inventory (`base_inventory.log`)
```text
Found 165 test(s).
System check identified no issues (0 silenced).
Created event: Coldplay: Music of the Spheres World Tour
Created 30 demo seats.
Swept 1 expired seat hold(s), released 1 reservation(s).
EXIT_CODE: 0
```
Ran line: `Ran 165 tests in 4.843s`

### 4. BASE Payments (`base_payments.log`)
```text
OK
Destroying test database for alias 'default'...

Found 70 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
Ran line: `Ran 70 tests in 1.739s`

### 5. REPO Coordinator (`repo_coordinator.log`)
```text
Recovering 5dc8c873-4e5f-4636-a90d-fc6acdc00316 claimed from COMPENSATION_FAILED...
  -> 5dc8c873-4e5f-4636-a90d-fc6acdc00316: compensated successfully.
Recovery sweep complete.

[OLD CODE] thread results=[1, 0], final status='COMPENSATED_ON_RETRY', saga_events=0. No audit trail. Loser detected by 0-rows-updated, not by a lock.
EXIT_CODE: 0
```
Ran line: `Ran 280 tests in 26.648s`

### 6. REPO Characterization (`repo_characterization.log`)
```text
OK
Destroying test database for alias 'default'...

Found 7 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
Ran line: `Ran 7 tests in 0.477s`

### 7. REPO Inventory (`repo_inventory.log`)
```text
Found 165 test(s).
System check identified no issues (0 silenced).
Created event: Coldplay: Music of the Spheres World Tour
Created 30 demo seats.
Swept 1 expired seat hold(s), released 1 reservation(s).
EXIT_CODE: 0
```
Ran line: `Ran 165 tests in 5.046s`

### 8. REPO Payments (`repo_payments.log`)
```text
OK
Destroying test database for alias 'default'...

Found 70 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
Ran line: `Ran 70 tests in 1.522s`

---

## Step 5: Redis Checks

### 5a. Services Check
- **Command**: `Get-Service *redis*, *memurai*`
- **Output**:
```text
Status  Name                   DisplayName             
------  ----                   -----------             
Running GameInputRedistService GameInput Redist Service
```

### 5b. Daemons Check
- **Command**: `Get-Command redis-server, memurai`
- **Output**:
```text
not found
```

### 5c. TCP Port 6379 Connection
- **Command**: `Test-NetConnection 127.0.0.1 -Port 6379 | Select-Object TcpTestSucceeded`
- **Output**:
```text
TcpTestSucceeded
----------------
           False
```

### 5d. SSE Timeout Warning Counts
- `base_coordinator.log`: **305**
- `repo_coordinator.log`: **305**

---

## Step 6: Commit Scope

### 6a. Git Log (`learning..feasible`)
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

### 6b. Commit Stats for All Commits
```text
79aa49f refactor: simplify coordinator views (names, env vars, comments); no behavior change

 ENV_MAP.md                                         |   28 +
 RENAME_MAP.md                                      |   30 +
 .../coordinator/tests/test_auth_otp.py             |    6 +-
 .../coordinator/tests/test_characterization.py     |  209 ++++
 coordinator-service/coordinator/views.py           | 1076 +++++++-------------
 5 files changed, 618 insertions(+), 731 deletions(-)
---
5ee7af6 chore: move cleanup docs, remove junk files

 .gitignore                                  |  9 +++--
 ENV_MAP.md                                  | 28 ---------------
 docs/cleanup/CLEANUP_PLAN.md                | 55 +++++++++++++++++++++++++++++
 docs/cleanup/ENV_MAP.md                     | 53 +++++++++++++++++++++++++++
 RENAME_MAP.md => docs/cleanup/RENAME_MAP.md |  0
 5 files changed, 114 insertions(+), 31 deletions(-)
---
39eef32 refactor: simplify coordinator views (names, env vars, comments); no behavior change

 coordinator-service/coordinator/tasks.py           |   4 +-
 .../coordinator/tests/test_a2_cas_checks.py        |   8 +-
 .../coordinator/tests/test_a3_concurrency.py       |  12 +-
 .../coordinator/tests/test_a5_payment_5xx.py       |   6 +-
 .../tests/test_a6_request_exceptions.py            |  14 +-
 .../coordinator/tests/test_a7_reserve_order.py     |  14 +-
 .../coordinator/tests/test_a8_cleanup.py           |  12 +-
 .../coordinator/tests/test_audit_hardening.py      |  84 ++++++------
 .../coordinator/tests/test_b1_ownership.py         |   2 +-
 .../coordinator/tests/test_b2_staff_bypass.py      |   8 +-
 .../coordinator/tests/test_b3_admin_domain.py      |  12 +-
 .../tests/test_b4_fault_injection_and_secrets.py   |   8 +-
 .../coordinator/tests/test_characterization.py     |   2 +-
 .../coordinator/tests/test_coordinator.py          |  16 +--
 .../coordinator/tests/test_coverage_boost.py       |  40 +++---
 .../coordinator/tests/test_exit_gate_gaps.py       |  46 +++----
 .../coordinator/tests/test_f1_checkout_presence.py |   6 +-
 ..._g8_coordinator_checkout_presence_resilience.py |   6 +-
 coordinator-service/coordinator/views.py           | 144 +++++++++------------
 docs/cleanup/RENAME_MAP.md                         |  43 +++---
 20 files changed, 234 insertions(+), 253 deletions(-)
---
4c3e3b2 chore: coordinator modules cleanup (dead code, comments, imports)

 coordinator-service/coordinator/auth_views.py      | 12 -----
 .../commands/recovery_stuck_transactions.py        |  2 +-
 coordinator-service/coordinator/middleware.py      | 51 ++++------------------
 coordinator-service/coordinator/recovery.py        |  2 +-
 coordinator-service/coordinator/saga_log.py        |  1 -
 coordinator-service/coordinator/tasks.py           |  3 +-
 coordinator-service/coordinator/ticket_views.py    |  1 -
 7 files changed, 12 insertions(+), 60 deletions(-)
---
7791b0f chore: inventory cleanup; no behavior change

 inventory-service/inventory/views.py | 18 +-----------------
 1 file changed, 1 insertion(+), 17 deletions(-)
---
78bf77b chore: payment cleanup; no behavior change

 payment-service/payments/views.py | 12 +++++-------
 1 file changed, 5 insertions(+), 7 deletions(-)
---
9dd6865 chore: frontend cleanup (unused imports, dead files)

 scripts/run_real_flow_e2e.py   |  7 +++----
 tests/test_gate_scanner.py     | 15 +++++----------
 tests/test_nonblocking_load.py |  3 +--
 3 files changed, 9 insertions(+), 16 deletions(-)
---
a1240c7 chore: fix ruff findings

 COORDINATOR_CHANGE.md                                |  4 ++--
 README.md                                            |  8 ++++----
 audit/coverage.md                                    | 10 +++++-----
 audit/final_report.md                                |  2 +-
 audit/findings.md                                    |  2 +-
 audit/plan_revisions.md                              |  2 +-
 .../coordinator/tests/test_a2_cas_checks.py          |  2 +-
 .../coordinator/tests/test_a3_concurrency.py         |  4 ++--
 .../coordinator/tests/test_a6_request_exceptions.py  |  6 +++---
 .../coordinator/tests/test_a8_cleanup.py             |  2 +-
 .../tests/test_b4_fault_injection_and_secrets.py     |  2 +-
 .../coordinator/tests/test_characterization.py       | 10 ++++++----
 .../coordinator/tests/test_coverage_boost.py         | 10 +++++-----
 .../coordinator/tests/test_f1_checkout_presence.py   |  6 +++---
 .../tests/test_g1_real_heartbeat_presence.py         |  2 +-
 ...st_g8_coordinator_checkout_presence_resilience.py |  2 +-
 .../tests/test_h5_admin_reset_prohibits_refund.py    |  2 +-
 .../coordinator/tests/test_ticket_email.py           |  2 +-
 .../coordinator/tests/test_ticket_endpoints.py       |  3 +--
 .../coordinator/tests/test_ticket_issuance.py        |  1 -
 .../coordinator/tests/test_ticket_pdf_qr.py          |  4 +---
 flowchart.mmd                                        | 20 ++++++++++----------
 .../inventory/management/commands/seed_demo_seats.py |  3 +--
 .../inventory/tests/test_g3_settings_audit.py        |  1 -
 .../inventory/tests/test_g4_throttling_hardening.py  |  2 +-
 .../inventory/tests/test_g5_startup_guard.py         |  1 -
 .../inventory/tests/test_g6_released_reason.py       |  2 +-
 .../inventory/tests/test_h4_throttle_limits.py       |  1 -
 .../tests/test_h5_released_reason_legacy.py          |  1 -
 .../inventory/tests/test_hardening_p0.py             |  2 --
 .../inventory/tests/test_hardening_p2.py             |  1 -
 inventory-service/inventory/tests/test_phase1.py     |  2 +-
 inventory-service/inventory/tests/test_phase3.py     |  7 ++-----
 scripts/run_playwright_test.py                       |  2 --
 scripts/run_real_flow_e2e.py                         |  2 --
 tests/test_gate_scanner.py                           |  3 +++
 36 files changed, 61 insertions(+), 75 deletions(-)
---
af16e3e docs: cleanup report

 docs/cleanup/CLEANUP_REPORT.md | 310 +++++++++++++++++++++++++++++++++++++++++
 1 file changed, 310 insertions(+)
---
edd28bb fix: restore original env var names and defaults

 coordinator-service/coordinator/views.py | 16 ++++++++--------
 1 file changed, 8 insertions(+), 8 deletions(-)
---
4e08903 test: align characterization tests with original behavior

 coordinator-service/coordinator/tests/test_characterization.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
---
d3deec4 docs: correct cleanup report

 docs/cleanup/CLEANUP_REPORT.md | 158 ++++++++-------
 docs/cleanup/VERIFY_LOG.md     | 440 +++++++++++++++++++++++++++++++++++++++++
 2 files changed, 532 insertions(+), 66 deletions(-)
---
4d71742 fix: read env at call time where tests change it

 coordinator-service/coordinator/views.py | 26 +++++++-------------------
 1 file changed, 7 insertions(+), 19 deletions(-)
---
11bea54 docs: correct verification evidence

 docs/cleanup/CLEANUP_REPORT.md | 368 +++++++++++----------------
 docs/cleanup/VERIFY_LOG.md     | 547 ++++++++++++++++++++---------------------
 2 files changed, 410 insertions(+), 505 deletions(-)
---
```

### 6c. Test Files in Commit 39eef32
- **Command**: `git show --stat 39eef32 | Select-String "test_"`
- **Output**:
```text
 .../coordinator/tests/test_a2_cas_checks.py        |   8 +-
 .../coordinator/tests/test_a3_concurrency.py       |  12 +-
 .../coordinator/tests/test_a5_payment_5xx.py       |   6 +-
 .../tests/test_a6_request_exceptions.py            |  14 +-
 .../coordinator/tests/test_a7_reserve_order.py     |  14 +-
 .../coordinator/tests/test_a8_cleanup.py           |  12 +-
 .../coordinator/tests/test_audit_hardening.py      |  84 ++++++------
 .../coordinator/tests/test_b1_ownership.py         |   2 +-
 .../coordinator/tests/test_b2_staff_bypass.py      |   8 +-
 .../coordinator/tests/test_b3_admin_domain.py      |  12 +-
 .../tests/test_b4_fault_injection_and_secrets.py   |   8 +-
 .../coordinator/tests/test_characterization.py     |   2 +-
 .../coordinator/tests/test_coordinator.py          |  16 +--
 .../coordinator/tests/test_coverage_boost.py       |  40 +++---
 .../coordinator/tests/test_exit_gate_gaps.py       |  46 +++----
 .../coordinator/tests/test_f1_checkout_presence.py |   6 +-
```
- **Line Count**: **16** printed lines matching `"test_"`.

### 6d. Commits Modifying `coordinator/views.py`
- **Command**: `git log --oneline learning..feasible -- coordinator-service/coordinator/views.py`
- **Output**:
```text
4d71742 fix: read env at call time where tests change it
edd28bb fix: restore original env var names and defaults
39eef32 refactor: simplify coordinator views (names, env vars, comments); no behavior change
79aa49f refactor: simplify coordinator views (names, env vars, comments); no behavior change
```
