# Verification Log: DTC Codebase Cleanup

This log records the exact commands, exit codes, and raw output copied directly from the evidence files in `docs/cleanup/evidence/` written during this run.

---

## Step 1: Prove Which Code Each Tree Runs

### Command 1.1: Prove REPO Identity
**Command**:
```powershell
$repoHead = git -C "C:\Users\MADHAVAN\Downloads\DTC" rev-parse HEAD
$repoViews = git -C "C:\Users\MADHAVAN\Downloads\DTC" rev-parse HEAD:coordinator-service/coordinator/views.py
"HEAD: $repoHead`nviews.py: $repoViews"
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/identity_repo.log`  
**Output**:
```text
HEAD: 4d71742a770a05bc54d0a0215053ca02b897e68b
views.py: 3ab473fde8afd5d14d9c51e9af2ba071e110b912
```

### Command 1.2: Prove BASE Identity
**Command**:
```powershell
$baseHead = git -C "C:\Users\MADHAVAN\Downloads\dtc_baseline" rev-parse HEAD
$baseViews = git -C "C:\Users\MADHAVAN\Downloads\dtc_baseline" rev-parse HEAD:coordinator-service/coordinator/views.py
"HEAD: $baseHead`nviews.py: $baseViews"
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/identity_base.log`  
**Output**:
```text
HEAD: f24b471af32bca53feeff0ddb0a478c53548789f
views.py: 16f16d3d37d3f5f263dbf4a5372f2167b6c9fdb3
```
*Verification*: Blob hashes differ (`3ab473fde8afd5d14d9c51e9af2ba071e110b912` vs `16f16d3d37d3f5f263dbf4a5372f2167b6c9fdb3`).

---

## Step 2: Raw Test Runs

All test commands were executed using `Set-Location` to the tree directory AND the absolute path to `manage.py`, with `--noinput`.

### Command 2.1: BASE Coordinator Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\dtc_baseline"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\dtc_baseline\coordinator-service\manage.py" test coordinator --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/base_coordinator.log`  
**Output (final lines)**:
```text
Recovery sweep complete.
  -> 3c4afdf3-785d-492d-a2e6-bc7f0ca975a7: compensated successfully.
  STARTED f7c33a46-f059-4f4a-bb92-bf4d3c1dac14: already claimed, skipping.
Recovering 2112a548-ab64-48b3-aea1-a59e9dde0b8e claimed from COMPENSATING...
  -> 2112a548-ab64-48b3-aea1-a59e9dde0b8e: compensated successfully.
Recovery sweep complete.
Recovering 5e9bf4d3-c11d-4579-9e85-0c194545fc58 claimed from COMPENSATION_FAILED...
  -> 5e9bf4d3-c11d-4579-9e85-0c194545fc58: compensation partially failed; will retry next sweep.
Recovery sweep complete.
Recovering a57ae8d9-b792-435b-b928-5f79388e54fd claimed from COMPENSATION_FAILED...
  -> a57ae8d9-b792-435b-b928-5f79388e54fd: compensated successfully.
Recovery sweep complete.

[OLD CODE] thread results=[1, 0], final status='COMPENSATED_ON_RETRY', saga_events=0. No audit trail. Loser detected by 0-rows-updated, not by a lock.
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 280 tests in 27.390s
OK
```

### Command 2.2: BASE Characterization Tests
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\dtc_baseline"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\dtc_baseline\coordinator-service\manage.py" test coordinator.tests.test_characterization --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/base_characterization.log`  
**Output (final lines)**:
```text
.
----------------------------------------------------------------------
Ran 7 tests in 0.447s

OK
Destroying test database for alias 'default'...

Found 7 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 7 tests in 0.447s
OK
```

### Command 2.3: BASE Inventory Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\dtc_baseline"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\dtc_baseline\inventory-service\manage.py" test inventory --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/base_inventory.log`  
**Output (final lines)**:
```text
OK
Destroying test database for alias 'default'...

Found 165 test(s).
System check identified no issues (0 silenced).
Created event: Coldplay: Music of the Spheres World Tour
Created 30 demo seats.
Swept 1 expired seat hold(s), released 1 reservation(s).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 165 tests in 4.843s
OK
```

### Command 2.4: BASE Payment Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\dtc_baseline"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\dtc_baseline\payment-service\manage.py" test payments --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/base_payments.log`  
**Output (final lines)**:
```text
.
----------------------------------------------------------------------
Ran 70 tests in 1.739s

OK
Destroying test database for alias 'default'...

Found 70 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 70 tests in 1.739s
OK
```

### Command 2.5: REPO Coordinator Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\DTC"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\DTC\coordinator-service\manage.py" test coordinator --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/repo_coordinator.log`  
**Output (final lines)**:
```text
Recovery sweep complete.
  -> ad3bcf7d-5359-4f06-8027-a5d7ca57ec80: compensated successfully.
  STARTED 90109263-b88a-4a13-b540-f5ff0be59334: already claimed, skipping.
Recovering b10daf34-2a90-48d5-947d-bfc18591bf3d claimed from COMPENSATING...
  -> b10daf34-2a90-48d5-947d-bfc18591bf3d: compensated successfully.
Recovery sweep complete.
Recovering d1a76cea-9014-4580-bbca-3bfcc25f10fb claimed from COMPENSATION_FAILED...
  -> d1a76cea-9014-4580-bbca-3bfcc25f10fb: compensation partially failed; will retry next sweep.
Recovery sweep complete.
Recovering 5dc8c873-4e5f-4636-a90d-fc6acdc00316 claimed from COMPENSATION_FAILED...
  -> 5dc8c873-4e5f-4636-a90d-fc6acdc00316: compensated successfully.
Recovery sweep complete.

[OLD CODE] thread results=[1, 0], final status='COMPENSATED_ON_RETRY', saga_events=0. No audit trail. Loser detected by 0-rows-updated, not by a lock.
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 280 tests in 26.648s
OK
```

### Command 2.6: REPO Characterization Tests
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\DTC"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\DTC\coordinator-service\manage.py" test coordinator.tests.test_characterization --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/repo_characterization.log`  
**Output (final lines)**:
```text
.
----------------------------------------------------------------------
Ran 7 tests in 0.477s

OK
Destroying test database for alias 'default'...

Found 7 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 7 tests in 0.477s
OK
```

### Command 2.7: REPO Inventory Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\DTC"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\DTC\inventory-service\manage.py" test inventory --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/repo_inventory.log`  
**Output (final lines)**:
```text
OK
Destroying test database for alias 'default'...

Found 165 test(s).
System check identified no issues (0 silenced).
Created event: Coldplay: Music of the Spheres World Tour
Created 30 demo seats.
Swept 1 expired seat hold(s), released 1 reservation(s).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 165 tests in 5.046s
OK
```

### Command 2.8: REPO Payment Test Suite
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\DTC"
& "C:\Users\MADHAVAN\Downloads\DTC\venv\Scripts\python.exe" "C:\Users\MADHAVAN\Downloads\DTC\payment-service\manage.py" test payments --noinput
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/repo_payments.log`  
**Output (final lines)**:
```text
.
----------------------------------------------------------------------
Ran 70 tests in 1.522s

OK
Destroying test database for alias 'default'...

Found 70 test(s).
System check identified no issues (0 silenced).
EXIT_CODE: 0
```
**Summary (from Select-String)**:
```text
Ran 70 tests in 1.522s
OK
```

---

## Step 3: Redis Check

### Command 3.1: Redis TCP Connection and Binary Check
**Command**:
```powershell
Test-NetConnection 127.0.0.1 -Port 6379
Get-Command redis-server, memurai -ErrorAction SilentlyContinue
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/redis.log`  
**Output**:
```text
ComputerName           : 127.0.0.1
RemoteAddress          : 127.0.0.1
RemotePort             : 6379
InterfaceAlias         : Loopback Pseudo-Interface 1
SourceAddress          : 127.0.0.1
PingSucceeded          : True
PingReplyDetails (RTT) : 0 ms
TcpTestSucceeded       : False
```
*Redis status*: Port 6379 closed; neither `redis-server` nor `memurai` is installed on host. Redis success path was not exercised live.

### Command 3.2: Timeout Warning Counts
**Command**:
```powershell
(Select-String -Path "docs\cleanup\evidence\base_coordinator.log" -Pattern "Timeout connecting to server").Count
(Select-String -Path "docs\cleanup\evidence\repo_coordinator.log" -Pattern "Timeout connecting to server").Count
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/redis_timeouts.log`  
**Output**:
```text
base_coordinator Timeout connecting to server count: 305
repo_coordinator Timeout connecting to server count: 305
```

---

## Step 4: Environment Variables Audit & Call-Time Refactor

### Command 4.1: Grep COORDINATOR_SERVICE_URL on learning and feasible
**Command**:
```powershell
git grep -n COORDINATOR_SERVICE_URL learning
git grep -n COORDINATOR_SERVICE_URL feasible
```
**Exit Code**: 0  
**Evidence Sources**: `docs/cleanup/evidence/env_COORDINATOR_SERVICE_URL_learning.log`, `docs/cleanup/evidence/env_COORDINATOR_SERVICE_URL_feasible.log`  
**Output**:
```text
learning:.env.example:24:COORDINATOR_SERVICE_URL=http://coordinator:8002
feasible:.env.example:24:COORDINATOR_SERVICE_URL=http://coordinator:8002
```
*Note*: `COORDINATOR_SERVICE_URL` is absent from both learning's `views.py` and feasible's `views.py`.

### Command 4.2: Audit and Commit Call-Time Env Reads
**Command**:
```powershell
git add coordinator-service/coordinator/views.py
git commit -m "fix: read env at call time where tests change it"
```
**Exit Code**: 0  
**Output**:
```text
[feasible 4d71742] fix: read env at call time where tests change it
 1 file changed, 7 insertions(+), 19 deletions(-)
```

---

## Step 5: Characterization Audit Mapping

**Evidence Source**: `docs/cleanup/evidence/characterization_mapping.log`  
**Content**:
```text
Test 1 (test_1_hold_success):
- Assertions: HTTP 200, status=HOLDING, held_until present
- Baseline lines: coordinator-service/coordinator/views.py:1048 (_set_status_with_event to HOLDING), views.py:1067 (Response with status: HOLDING, held_until)

Test 2 (test_2_hold_with_reserve_failure):
- Assertions: HTTP 409, code=SEAT_NOT_AVAILABLE, status=INVENTORY_RESERVE_FAILED
- Baseline lines: coordinator-service/coordinator/views.py:1019 (_set_status_with_event to INVENTORY_RESERVE_FAILED), views.py:1025-1031 (Response with resp_json and status 409)

Test 3 (test_3_pay_success):
- Assertions: HTTP 200, status=COMPLETED
- Baseline lines: coordinator-service/coordinator/views.py:1996 (_set_status_with_event to COMPLETED), views.py:2021 (Response with status: COMPLETED)

Test 4 (test_4_pay_with_payment_decline):
- Assertions: HTTP 400, status=COMPENSATED
- Baseline lines: coordinator-service/coordinator/views.py:2207-2209 (_set_status_with_event to COMPENSATED), views.py:2228 (Response with status: COMPENSATED, HTTP 400)

Test 5 (test_5_pay_on_expired_hold):
- Assertions: HTTP 409, code=HOLD_EXPIRED, status=TIMEOUT
- Baseline lines: coordinator-service/coordinator/views.py:1136 (_set_status_with_event to TIMEOUT), views.py:1163-1171 (Response with code: HOLD_EXPIRED, status: TIMEOUT, HTTP 409)

Test 6 (test_6_retry_on_non_eligible_status):
- Assertions: HTTP 400
- Baseline lines: coordinator-service/coordinator/views.py:429-437 (Status allowlist check: if txn.status not in ALLOWED_RETRY_STATUSES -> HTTP 400)

Test 7 (test_7_idor_non_staff_denied_on_other_users_transaction):
- Assertions: GET -> HTTP 403; POST pay -> HTTP 403, code=FORBIDDEN
- Baseline lines: coordinator-service/coordinator/views.py:179 (GET ownership check -> HTTP 403), views.py:1094-1098 (POST pay ownership check -> HTTP 403 with code: FORBIDDEN)
```

---

## Step 6: Ruff Linter & Compilation Check

### Command 6.1: Ruff Linter Violations
**Command**:
```powershell
Set-Location "C:\Users\MADHAVAN\Downloads\dtc_baseline"; ruff check --output-format json .
Set-Location "C:\Users\MADHAVAN\Downloads\DTC"; ruff check --output-format json .
```
**Exit Code**: 1 (both BASE and REPO)  
**Evidence Sources**: `docs/cleanup/evidence/base_ruff.json`, `docs/cleanup/evidence/repo_ruff.json`  
- **Base Violations**: 646 violations
- **Repo Violations**: 584 violations
- **Net Reduction**: -62 violations

### Command 6.2: Python Compilation Check
**Command**:
```powershell
$files = (git diff --name-only learning..feasible -- "*.py").Split("`r`n", [System.StringSplitOptions]::RemoveEmptyEntries)
foreach ($f in $files) { python -m py_compile "$f" }
```
**Exit Code**: 0  
**Evidence Source**: `docs/cleanup/evidence/compile.log`  
**Output (final lines)**:
```text
inventory-service/inventory/tests/test_hardening_p2.py : EXIT_CODE 0
inventory-service/inventory/tests/test_phase1.py : EXIT_CODE 0
inventory-service/inventory/tests/test_phase3.py : EXIT_CODE 0
inventory-service/inventory/views.py : EXIT_CODE 0
payment-service/payments/views.py : EXIT_CODE 0
scripts/run_playwright_test.py : EXIT_CODE 0
scripts/run_real_flow_e2e.py : EXIT_CODE 0
tests/test_gate_scanner.py : EXIT_CODE 0
tests/test_nonblocking_load.py : EXIT_CODE 0
OVERALL_ALL_ZERO: True
```

---

## Step 7: Commits Scope Audit

**Evidence Source**: `docs/cleanup/evidence/commits.log`  
**Full Commit Log (`learning..feasible`)**:
1. `79aa49fe28ba00fc492f7750e90c36d7957aaeb7`: `refactor: simplify coordinator views (names, env vars, comments); no behavior change` (Initial rewrite of `coordinator/views.py`, created `ENV_MAP.md`, `RENAME_MAP.md`, and `test_characterization.py`)
2. `5ee7af6170c952911663b3f94e0f063e7c81f1bb`: `chore: move cleanup docs, remove junk files` (Moved docs into `docs/cleanup/`, modified `.gitignore`)
3. `39eef325e07464a3360d07403f1c8c0845a617ea`: `refactor: simplify coordinator views (names, env vars, comments); no behavior change` (Removed compatibility shims in `views.py`, updated mock targets across 18 test files)
4. `4c3e3b23a3198010eea7d5a085d4265e9e6d673e`: `chore: coordinator modules cleanup (dead code, comments, imports)`
5. `7791b0f083ae62d76007eeef90579f24e0fde26c`: `chore: inventory cleanup; no behavior change`
6. `78bf77b57a0637b631d6e2210134b9f2929673ac`: `chore: payment cleanup; no behavior change`
7. `9dd68653081e42c23bea4ec6e2171866154a1df6`: `chore: frontend cleanup (unused imports, dead files)`
8. `a1240c7b9c93fa5ddb414faf36eb38d4e7427622`: `chore: fix ruff findings` (Fixed unused imports/variables; touched markdown/docs: `COORDINATOR_CHANGE.md`, `README.md`, `audit/*`, `flowchart.mmd`)
9. `af16e3ec8d6430d3079275f567a1d936569ab52f`: `docs: cleanup report`
10. `edd28bbda59be7aef350acb9213e16241e73eec3`: `fix: restore original env var names and defaults`
11. `4e08903bc6f22baf626a1d383da0645250880118`: `test: align characterization tests with original behavior`
12. `d3deec41770899ca362665a73b1e6833205a00f9`: `docs: correct cleanup report`
13. `4d71742a770a05bc54d0a0215053ca02b897e68b`: `fix: read env at call time where tests change it`
