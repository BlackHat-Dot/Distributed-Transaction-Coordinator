# DTC System Security & Performance Audit Findings

---

### Finding `SEC-01` [Critical]
**Broken Object-Level Authorization (BOLA/IDOR) in Retry Compensation**
- **File & Line**: `coordinator-service/coordinator/views.py:116-128`
- **Evidence Status**: DEMONSTRABLE (Definitive code path inspection)
- **Vulnerability Mechanism**:
  `POST /transactions/<uuid:transaction_id>/retry-compensation/` verifies that the caller has a valid session (`is_authenticated`), but fails to check whether the authenticated user owns the transaction (`txn.user_id == auth_user_id`) or has staff privileges (`is_staff`).
- **Attack Narrative**:
  1. Attacker registers any valid free email via OTP to receive a legitimate JWT bearer token.
  2. Attacker inspects or discovers target transaction UUID `00000000-0000-0000-0000-000000000001` belonging to a victim.
  3. Attacker sends `POST /transactions/00000000-0000-0000-0000-000000000001/retry-compensation/` with their own JWT.
  4. The Coordinator executes `_release_inventory()`, canceling the victim's seat holds, and marks the transaction as `COMPENSATED_ON_RETRY`.
- **Impact**: Any user can cancel and disrupt other users' ticket reservations. Furthermore, because `retry_compensation` does not issue refunds, the victim's money remains stranded if the transaction was charged.
- **Defensive Regression Test Specification**:
  ```python
  def test_retry_compensation_idor_rejected(self):
      # User A owns txn
      txn = Transaction.objects.create(
          transaction_id=uuid.uuid4(),
          user_id="usr_victim",
          status=Transaction.Status.COMPENSATION_FAILED
      )
      # User B attempts to trigger compensation
      headers = {"HTTP_AUTHORIZATION": f"Bearer {user_b_jwt}"}
      response = self.client.post(f"/transactions/{txn.transaction_id}/retry-compensation/", **headers)
      self.assertEqual(response.status_code, 403)
  ```
- **Concrete Remediation**:
  In `coordinator-service/coordinator/views.py`:
  ```python
  # IDOR Protection: Caller must own the transaction or be staff
  if not is_staff and user_id and txn.user_id and txn.user_id != user_id:
      return Response(
          {"error": "You do not have permission to retry compensation for this transaction."},
          status=403,
      )
  ```

---

### Finding `REL-01` [Critical]
**Stranded Customer Payment on Failed Seat Release in Compensation & Recovery**
- **File & Line**: `coordinator-service/coordinator/views.py:1059-1185` and `coordinator/management/commands/recovery_stuck_transactions.py:310-345`
- **Evidence Status**: DEMONSTRABLE (Deterministic execution branch)
- **Vulnerability Mechanism**:
  In both `_compensate()` and `recovery_stuck_transactions.py`, the compensation flow attempts to release inventory *first*. If `INVENTORY_RELEASE_URL` times out or returns non-200, the functions record `COMPENSATION_FAILED` and immediately return. They completely skip the `if was_charged:` block, meaning no refund request is ever dispatched to `PAYMENT_REFUND_URL`.
- **Failure Narrative**:
  1. Coordinator successfully reserves Seat 10 and successfully charges the customer $500.
  2. Inventory confirmation call times out (network drop).
  3. Coordinator enters `_compensate(was_charged=True)`.
  4. Outbound inventory release call suffers a socket timeout (HTTP 504 / ConnectionError).
  5. The code exits on line 1083. The customer's $500 is never refunded, and the transaction is left permanently stranded in `COMPENSATION_FAILED`.
- **Impact**: Direct financial loss for customers. High compliance and legal liability.
- **Defensive Regression Test Specification**:
  ```python
  @patch("coordinator.views.requests.post")
  def test_refund_dispatched_even_if_inventory_release_fails(self, mock_post):
      # Mock inventory release returning 500
      # Mock payment refund returning 200
      # Verify that payment refund was called despite release failure
  ```
- **Concrete Remediation**:
  Uncouple refund execution from inventory release success. Both compensation actions must be attempted independently:
  ```python
  # Always execute refund if charged, regardless of whether inventory release succeeded
  if was_charged:
      try:
          refund_response = requests.post(PAYMENT_REFUND_URL, json={"transaction_id": str(transaction_id)}, headers=headers, timeout=REQUEST_TIMEOUT)
          if refund_response.status_code == 200:
              refund_data = refund_response.json()
      except Exception:
          refund_data = None
  ```

---

### Finding `SEC-02` [High]
**Missing Role-Based Access Control / Privilege Escalation on `trigger_recovery_sweep`**
- **File & Line**: `coordinator-service/coordinator/views.py:222-230`
- **Evidence Status**: DEMONSTRABLE (Code inspection)
- **Vulnerability Mechanism**:
  `POST /recovery/sweep/` only checks `if not is_authenticated: return 401`. It completely omits checking `if not is_staff: return 403`.
- **Attack Narrative**:
  Any registered public customer can send `POST /recovery/sweep/` to trigger a synchronous, resource-intensive transaction recovery run on demand.
- **Impact**:
  Denial of Service (DoS): Callers can spam the endpoint to lock Postgres tables, monopolize worker threads, and trigger cascades of outbound HTTP calls.
- **Defensive Regression Test Specification**:
  ```python
  def test_recovery_sweep_forbidden_for_regular_user(self):
      headers = {"HTTP_AUTHORIZATION": f"Bearer {regular_user_jwt}"}
      response = self.client.post("/recovery/sweep/", **headers)
      self.assertEqual(response.status_code, 403)
  ```
- **Concrete Remediation**:
  In `coordinator-service/coordinator/views.py`:
  ```python
  is_authenticated, auth_user_id, is_staff = _get_requester_identity(request)
  if not is_authenticated:
      return Response({"error": "Authentication required."}, status=401)
  if not is_staff:
      return Response({"error": "Staff authorization required to trigger recovery sweep."}, status=403)
  ```

---

### Finding `SEC-03` [High]
**Unauthenticated Ingress & Missing Shared Secret on Payment Service**
- **File & Line**: `payment-service/payments/views.py:100, 147, 160`, `docker-compose.yml:81`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `charge_payment`, `payment_check`, and `refund_payment` in `payments/views.py` perform zero authorization or service-token checks. In `docker-compose.yml`, port 8001 is bound directly to host port 8001.
- **Attack Narrative**:
  An attacker with local or network access to port 8001 can directly dispatch `POST /payments/refund/` with any transaction ID and trigger unauthorized refunds, or forge charges without coordinator orchestration.
- **Impact**: Unauthorized financial transactions and account tampering.
- **Concrete Remediation**:
  Add `_verify_internal_service_key(request)` to `payment-service/payments/views.py`, matching the pattern in Inventory Service, and do not expose port 8001 on public network interfaces.

---

### Finding `SEC-04` [Medium]
**Non-Constant-Time String Comparison on Secrets (Timing Attack)**
- **File & Line**: `coordinator-service/coordinator/views.py:501`, `inventory-service/inventory/views.py:147`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  The code compares secrets using standard Python string equality:
  `request.headers.get("X-Staff-Key") == staff_api_key`
  `key != INTERNAL_SERVICE_KEY`
  Standard string equality exits on the first byte mismatch, leaking timing information.
- **Impact**: Potential byte-by-byte reconstruction of API keys via high-resolution timing measurements.
- **Concrete Remediation**:
  Use `secrets.compare_digest`:
  ```python
  import secrets

  if staff_api_key and request.headers.get("X-Staff-Key"):
      has_staff_key = secrets.compare_digest(request.headers.get("X-Staff-Key"), staff_api_key)
  ```

---

### Finding `SEC-05` [High]
**Client IP Spoofing & Rate-Limiting Bypass via Unsanitized `X-Forwarded-For`**
- **File & Line**: `coordinator-service/coordinator/auth_views.py:173-189`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `_get_client_ip()` trusts the leftmost value in the client-supplied `X-Forwarded-For` header:
  `return xff.split(",")[0].strip()`
- **Attack Narrative**:
  An attacker cycles fake IPs (`X-Forwarded-For: 10.0.0.1`, `X-Forwarded-For: 10.0.0.2`) on every request to `POST /auth/send-otp/`, completely circumventing IP-based rate limiting, cooldowns, and brute-force defenses.
- **Impact**: Email bombing, third-party Resend API credit depletion, and brute-force OTP attempts.
- **Concrete Remediation**:
  Only read `X-Forwarded-For` if behind a verified reverse proxy, or use the rightmost non-private proxy hop. In local/container environments, bind to `REMOTE_ADDR` by default unless configured with a trusted proxy CIDR.

---

### Finding `CONC-01` [High]
**Race Condition Bypassing `MAX_USER_HOLDS` Under Concurrent Reservations**
- **File & Line**: `inventory-service/inventory/views.py:290-327`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `reserve_seat` acquires row-level locks on the *target* seats (`Seat.objects.select_for_update().filter(id__in=seat_ids)`), but does not lock the user's active hold count.
  Two simultaneous requests for different seats under the same `user_id` will both read `active_holds = 0` at the same instant and both succeed, allowing a user to hold $2 \times \text{limit}$ or more seats.
- **Impact**: Circumvention of the business limit on seat hoarding / scalping.
- **Concrete Remediation**:
  Acquire an advisory lock in Postgres or an atomic Redis distributed lock on the user's ID during reservation:
  ```python
  user_lock_key = f"dtc:lock:reserve:{user_id}"
  # Lock acquired before checking active_holds and released after transaction commits.
  ```

---

### Finding `SEC-06` [Medium]
**Insecure Default Secrets and Wildcard Host Configuration**
- **File & Line**: `docker-compose.yml:47-49, 78-79, 109-112`, `settings.py`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `ALLOWED_HOSTS: *` and hardcoded default fallback secrets (`django-insecure-coordinator-secret`, `dtc-internal-coordinator-secret-key-2026`) allow DNS rebinding and forged tokens if deployed without explicit environment overrides.
- **Concrete Remediation**:
  Enforce strict non-default secrets when `DEBUG=False`, and restrict `ALLOWED_HOSTS` to designated domain names.

---

### Finding `SEC-07` [Medium]
**Potential Cross-Tenant Transaction Leak on Falsy User ID in `list_transactions`**
- **File & Line**: `coordinator-service/coordinator/views.py:686-688`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `if not is_staff and auth_user_id: queryset = queryset.filter(user_id=auth_user_id)`.
  If `auth_user_id` evaluates to empty string or None for a non-staff caller, the filter is skipped, returning all transactions.
- **Concrete Remediation**:
  Explicitly return an empty list or 403 if `not is_staff and not auth_user_id`.

---

### Finding `SEC-08` [Medium]
**Internal Service Key Verification Bypass When `DEBUG=True`**
- **File & Line**: `inventory-service/inventory/views.py:157-158`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `if getattr(settings, "IS_TESTING", False) or settings.DEBUG: return None, True`
  When `DEBUG=True` (the default in docker-compose.yml), missing internal keys are accepted as valid internal requests.
- **Concrete Remediation**:
  Remove `settings.DEBUG` from the internal key bypass; only permit bypass during automated test runs (`IS_TESTING`) when mocks are intentionally used.

---

### Finding `PERF-01` [High]
**Synchronous Database Write & N+1 Redis Network Calls on Read Requests (`GET /seats/`)**
- **File & Line**: `inventory-service/inventory/views.py:44-98`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  Every single `GET /seats/` request executes `reclaim_expired_holds()`. This function loops over every held seat in the database and performs a synchronous `redis_client.exists()` call per seat, followed by an atomic SQL update.
- **Impact**:
  Severe read latency degradation under high traffic. For 500 held seats, every user loading the seatmap causes 500 separate Redis network calls and database write contention on an HTTP GET endpoint.
- **Concrete Remediation**:
  Rely on the background Celery/Celery Beat periodic task (`sweep_expired_holds_task`) for database reclamation. On read, utilize the existing lazy shedding in `SeatSerializer.to_representation()` without executing synchronous DB updates.

---

### Finding `PERF-02` [Medium]
**Redundant `SELECT COUNT(*)` on Every Seatmap Query**
- **File & Line**: `inventory-service/inventory/views.py:102`
- **Evidence Status**: DEMONSTRABLE
- **Vulnerability Mechanism**:
  `if Seat.objects.count() == 0:` is evaluated on every single `GET /seats/` call, forcing a full table count scan even when the table is heavily populated.
- **Concrete Remediation**:
  Use a startup migration or `manage.py` seed command rather than checking row count during live HTTP traffic.
