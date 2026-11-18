# Imports standard OS library to access process environment variables for service endpoints and timeouts.
import os
# Imports standard UUID library to parse and validate 128-bit transaction identifier strings.
import uuid
# Imports requests library to perform outbound HTTP calls to downstream Inventory and Payment microservices during compensation.
import requests
# Imports Django runtime settings to read microservice connection URLs and shared authentication secrets.
from django.conf import settings
# Imports Transaction model for state definitions and SagaEvent model for compensation audit trail actors.
from coordinator.models import Transaction, SagaEvent
# Imports Compare-And-Swap (CAS) state transition helper to record state updates and append-only audit events in the database.
from coordinator.saga_log import _set_status_with_event

# Base URL for Inventory Service, resolving dynamically from Django settings, environment variables, or localhost default.
INVENTORY_BASE_URL = getattr(settings, "INVENTORY_SERVICE_URL", None) or os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
# Base URL for Payment Service, resolving dynamically from Django settings, environment variables, or localhost default.
PAYMENT_BASE_URL = getattr(settings, "PAYMENT_SERVICE_URL", None) or os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

# Separate socket connect timeout (default 2.0s) and read timeout (default 5.0s) to prevent thread hangs during TCP SYN packet drops.
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "2.0"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "5.0")))
# Separate connect and read timeouts prevent indefinite socket hanging during network partitions
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
# Shared secret token transmitted in HTTP headers (X-Internal-Service-Key) to authenticate coordinator requests with downstream services.
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")

# Single source of truth from Transaction model
# Exposes immutable terminal states from Transaction model to guide sweeper eligibility decisions.
TERMINAL_STATUSES = Transaction.TERMINAL_STATUSES
# Exposes non-terminal ambiguous states from Transaction model that require active background recovery.
RECOVERABLE_STATUSES = Transaction.RECOVERABLE_STATUSES


# Flexible HTTP invocation adapter supporting both requests signatures (url, **kw) and custom coordinator wrapper signatures (method, url, **kw).
def _safe_call_http(func, method, url, **kwargs):
    """
    Invokes an HTTP helper function regardless of whether it expects:
    1. (url, **kwargs)          -- e.g. requests.get, requests.post
    2. (method, url, **kwargs)  -- e.g. coordinator.views._http
    """
    # Fast-path optimization: directly calls standard requests functions that follow the (url, **kwargs) signature.
    if func is requests.get or func is requests.post:
        return func(url, **kwargs)
    # Introspects custom HTTP callable parameters using inspect module to match signature automatically.
    try:
        import inspect
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())
        if params and params[0] in ("url", "uri"):
            return func(url, **kwargs)
        if params and params[0] in ("method", "verb"):
            return func(method, url, **kwargs)
    except (ValueError, TypeError):
        pass
    try:
        return func(url, **kwargs)
    except TypeError:
        return func(method, url, **kwargs)


# Dynamically constructs target endpoint URLs at runtime to guarantee latest environment/settings overrides are immediately respected.
def _get_urls():
    """Dynamically resolves downstream URLs with runtime settings/env overrides."""
    # Resolves current Inventory Service base URL dynamically with runtime settings and environment fallback.
    inv_base = (getattr(settings, "INVENTORY_SERVICE_URL", None) or os.getenv("INVENTORY_SERVICE_URL", INVENTORY_BASE_URL)).rstrip("/")
    pay_base = (getattr(settings, "PAYMENT_SERVICE_URL", None) or os.getenv("PAYMENT_SERVICE_URL", PAYMENT_BASE_URL)).rstrip("/")
    # Returns dictionary mapping operational downstream action keys to their fully-qualified HTTP endpoint URLs.
    return {
        "payment_check": f"{pay_base}/payments/check/",
        "payment_refund": f"{pay_base}/payments/refund/",
        "payment_void": f"{pay_base}/payments/void/",
        "inventory_release": f"{inv_base}/seats/release/",
        "inventory_confirm": f"{inv_base}/seats/confirm/",
        "inventory_check": f"{inv_base}/reserve/check/",
    }


# Unified compensation workflow orchestrating downstream payment refund/void and seat release when transactions abort or get stuck.
def execute_compensation_sweep(
    transaction_id,
    actor=SagaEvent.ACTOR_RECOVERY,
    original_status=None,
    get_func=None,
    post_func=None,
):
    """
    Shared compensation workflow used by both the recovery sweeper and retry_compensation.
    Expects transaction to already be claimed in RECOVERY_IN_PROGRESS.

    1. Checks payment status via PAYMENT_CHECK_URL.
       - If timeout or 5xx: releases claim back to original_status, returns 503, does NOT release inventory.
       - If NOT_CHARGED or REFUNDED: refund is considered complete.
       - If CHARGED: issues refund via PAYMENT_REFUND_URL.
    2. Issues inventory release via INVENTORY_RELEASE_URL.
    3. Final state resolution:
       - If refund_ok and release_ok -> COMPENSATED (or COMPENSATED_ON_RETRY)
       - Else -> COMPENSATION_FAILED (or RETRY_COMPENSATION_FAILED)
    """
    try:
        # Parses the transaction ID strictly as a 128-bit UUID to prevent URL injection and fail-fast on malformed parameters.
        tid = uuid.UUID(str(transaction_id))
    # Intercepts malformed or non-UUID inputs, returning structured failure response without executing remote calls.
    except (ValueError, TypeError, AttributeError):
        return {
            "success": False,
            "status_code": 404,
            "status": Transaction.Status.COMPENSATION_FAILED,
            "error": "Invalid transaction_id format",
        }

    # Imports correlation ID context utilities lazily to avoid circular dependencies with middleware.
    from coordinator.middleware import get_correlation_id, set_correlation_id
    # Binds transaction ID into contextvars correlation context unless executing inside Celery worker thread where bgworker is preserved.
    if get_correlation_id() != "bgworker":
        set_correlation_id(str(tid))

    # Selects HTTP GET dispatch callable, allowing test fixtures to supply mock implementations.
    get_http = get_func or requests.get
    post_http = post_func or requests.post

    # Resolves current operational URLs for downstream payment and inventory endpoints.
    urls = _get_urls()
    cid = str(tid)
    internal_key = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY", INTERNAL_SERVICE_KEY)
    # Standard distributed tracing and authentication headers forwarded to downstream microservices.
    headers = {
        "X-Correlation-ID": cid,
        "X-Transaction-ID": cid,
        "X-Internal-Service-Key": internal_key,
    }

    # Fetches the transaction database entity to retrieve reserved seat identifiers for inventory release.
    txn = Transaction.objects.filter(transaction_id=tid).first()
    seat_ids = txn.seat_ids if txn else []

    # 1. Payment Check in EVERY status
    # Initializes tracking variables for payment state inspection outcome and ground-truth charge status.
    payment_check_failed = False
    payment_status = None
    payment_voided = False
    try:
        # Step 1: Queries Payment Service to inspect ground-truth charge status before deciding whether to refund or void.
        p_resp = _safe_call_http(
            get_http,
            "GET",
            urls["payment_check"],
            params={"transaction_id": cid},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        # Treats HTTP 5xx server errors as transient payment check failures to prevent executing incorrect compensation steps.
        if p_resp.status_code >= 500:
            payment_check_failed = True
        # Parses successful payment check response to extract current charge status and voided tombstone flag.
        elif p_resp.status_code == 200:
            try:
                p_data = p_resp.json() if p_resp.content else {}
            except Exception:
                p_data = {}
            if isinstance(p_data, dict):
                payment_status = p_data.get("status")
                payment_voided = bool(p_data.get("voided", False))
            else:
                payment_check_failed = True
        else:
            payment_check_failed = True
    # Traps network connectivity errors, DNS resolution failures, and request timeouts during payment check.
    except (requests.exceptions.RequestException, Exception):
        payment_check_failed = True

    # If payment check fails or times out, safely release the recovery claim back to original_status so a subsequent sweep can retry.
    if payment_check_failed:
        # Preserves original non-terminal status for future sweeper retry rather than permanently marking transaction as failed.
        revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
        # Atomically reverts the transaction state in the saga log to release the claim for future sweepers.
        _set_status_with_event(
            transaction_id=tid,
            to_status=revert_status,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=actor,
            http_status_code=None,
            http_payload={"error": "Payment check failed/timed out. Claim released."},
        )
        # Returns 503 Service Unavailable indicating downstream payment probe failure to caller.
        return {
            "success": False,
            "status_code": 503,
            "status": revert_status,
            "error": "Payment check failed or timed out; claim released for retry",
        }

    # Transition the transaction to COMPENSATING status in the audit ledger before initiating external refund/release side-effects.
    # Evaluates recovery actor: transitions state to COMPENSATING for sweeper runs while preserving RECOVERY_IN_PROGRESS for manual retries.
    if actor == SagaEvent.ACTOR_RECOVERY:
        _set_status_with_event(
            transaction_id=tid,
            to_status=Transaction.Status.COMPENSATING,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=actor,
            http_status_code=None,
            http_payload=None,
        )
        expected_final_status = (Transaction.Status.COMPENSATING, Transaction.Status.RECOVERY_IN_PROGRESS)
    else:
        expected_final_status = (Transaction.Status.RECOVERY_IN_PROGRESS,)

    # Payment status evaluation / refund
    # Initializes tracking variables for refund completion, HTTP status codes, and late-charge discovery flags.
    refund_ok = False
    refund_status_code = None
    refund_payload = None
    # Sentinel: void revealed a late charge → forward-complete instead of compensating
    void_revealed_charge = False

    # Fast-path check: marks refund successful if payment is already refunded or previously voided by a tombstone.
    if payment_status == "REFUNDED" or (payment_status == "NOT_CHARGED" and payment_voided):
        refund_ok = True
    # Defensive void fence: if not charged, actively void payment authorization so a delayed inflight charge cannot land later.
    elif payment_status == "NOT_CHARGED":
        # Fence against in-flight charge landing after release
        # Initializes void fencing flag to ensure payment gateway voids inflight authorizations.
        void_ok = False
        # Dispatches void request to Payment Service to permanently close the transaction window against late charges.
        try:
            v_resp = _safe_call_http(
                post_http,
                "POST",
                urls["payment_void"],
                json={"transaction_id": cid},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            # Inspects void response: verifies successful voiding or detects if an in-flight charge completed during transit.
            if v_resp.status_code == 200:
                try:
                    v_data = v_resp.json() if v_resp.content else {}
                except Exception:
                    v_data = {}
                if isinstance(v_data, dict):
                    if v_data.get("status") == "CHARGED":
                        payment_status = "CHARGED"  # Late charge landed; route to confirm below
                        void_revealed_charge = True
                    else:
                        void_ok = True
                else:
                    void_ok = True
        except (requests.exceptions.RequestException, Exception):
            void_ok = False

        # Advances refund tracking flag when defensive voiding successfully completes without errors.
        if void_ok:
            refund_ok = True
        # Traps void failures where payment state remains uncertain to abort compensation safely.
        elif payment_status != "CHARGED":
            # Void failed and payment not confirmed charged -> state uncertain, abort compensation
            revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
            # Reverts transaction status in saga log so subsequent recovery sweepers can re-attempt voiding.
            _set_status_with_event(
                transaction_id=tid,
                to_status=revert_status,
                expected_status=expected_final_status,
                actor=actor,
                http_status_code=None,
                http_payload={"error": "Payment void failed/timed out. Claim released."},
            )
            # Returns 503 Service Unavailable when defensive voiding cannot be verified, aborting inventory release.
            return {
                "success": False,
                "status_code": 503,
                "status": revert_status,
                "error": "Payment void failed; claim released for retry",
            }

    # Before refunding, re-check inventory: if payment is charged, attempt to confirm first
    if payment_status == "CHARGED" or void_revealed_charge:
        confirm_ok = False
        confirm_payload = None
        confirm_status_code = None
        confirm_transport_error = False
        try:
            c_resp = _safe_call_http(
                post_http,
                "POST",
                urls["inventory_confirm"],
                json={"transaction_id": cid, "seat_ids": seat_ids},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            confirm_status_code = c_resp.status_code
            if c_resp.status_code == 200:
                confirm_ok = True
                try:
                    confirm_payload = c_resp.json() if c_resp.content else None
                except Exception:
                    pass
            elif c_resp.content:
                try:
                    confirm_payload = c_resp.json()
                except Exception:
                    pass
        except (requests.exceptions.RequestException, Exception):
            confirm_transport_error = True
            confirm_ok = False

        if confirm_ok:
            _set_status_with_event(
                transaction_id=tid,
                to_status=Transaction.Status.COMPLETED,
                expected_status=expected_final_status,
                actor=actor,
                http_status_code=200,
                http_payload={"payment": {"status": "CHARGED"}, "inventory": confirm_payload},
            )
            return {
                "success": True,
                "status_code": 200,
                "status": Transaction.Status.COMPLETED,
                "forward_completed": True,
                "compensation_response": confirm_payload,
                "refund_response": None,
            }

        # Transport error or 5xx server error: DO NOT refund or release! Revert claim so next sweep retries.
        if confirm_transport_error or (confirm_status_code and confirm_status_code >= 500):
            revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
            _set_status_with_event(
                transaction_id=tid,
                to_status=revert_status,
                expected_status=expected_final_status,
                actor=actor,
                http_status_code=confirm_status_code,
                http_payload={"error": "Inventory confirm transport error/5xx; leaving recoverable for retry."},
            )
            return {
                "success": False,
                "status_code": 503,
                "status": revert_status,
                "error": "Inventory confirm transport error/5xx; claim released for retry",
            }

        # Definitive 4xx error (hold expired, seats not found)
        # Check released_reason: if released by ADMIN_RESET, prohibit auto-refund and require manual review
        released_reason = None
        if confirm_payload and isinstance(confirm_payload, dict):
            released_reason = confirm_payload.get("released_reason")

        if not released_reason:
            try:
                chk_resp = _safe_call_http(
                    get_http,
                    "GET",
                    urls["inventory_check"],
                    params={"transaction_id": cid},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                if chk_resp.status_code == 200:
                    try:
                        chk_data = chk_resp.json() if chk_resp.content else {}
                    except Exception:
                        chk_data = {}
                    if isinstance(chk_data, dict):
                        released_reason = chk_data.get("released_reason")
            except Exception:
                pass

        if released_reason == "ADMIN_RESET":
            revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
            _set_status_with_event(
                transaction_id=tid,
                to_status=revert_status,
                expected_status=expected_final_status,
                actor=actor,
                http_status_code=409,
                http_payload={
                    "error": "Inventory was released by ADMIN_RESET while payment is CHARGED; auto-refund prohibited. Manual review required.",
                    "manual_review_required": True,
                    "released_reason": "ADMIN_RESET",
                    "payment_status": "CHARGED",
                },
            )
            return {
                "success": False,
                "status_code": 409,
                "status": revert_status,
                "error": "Reservation was released by ADMIN_RESET; auto-refund prohibited, manual review required",
                "manual_review_required": True,
                "released_reason": "ADMIN_RESET",
            }

        payment_status = "CHARGED"

    # Step 2: If payment was confirmed charged, issue a refund request to Payment Service to credit funds back to the customer.
    if payment_status == "CHARGED":
        try:
            r_resp = _safe_call_http(
                post_http,
                "POST",
                urls["payment_refund"],
                json={"transaction_id": cid},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            refund_status_code = r_resp.status_code
            if r_resp.content:
                try:
                    refund_payload = r_resp.json()
                except Exception:
                    pass
            refund_ok = (r_resp.status_code == 200)
            if not refund_ok and isinstance(refund_payload, dict):
                msg = str(refund_payload.get("message", "")).lower() + " " + str(refund_payload.get("error", "")).lower()
                code = str(refund_payload.get("code", "")).upper()
                if (
                    "already refunded" in msg
                    or "payment not found" in msg
                    or code in ("ALREADY_REFUNDED", "NOT_FOUND")
                ):
                    refund_ok = True
        except (requests.exceptions.RequestException, Exception):
            refund_ok = False
    elif payment_status != "NOT_CHARGED" and payment_status != "REFUNDED":
        refund_ok = False

    # 2. Inventory Release
    # Step 3: Issues seat release request to Inventory Service to restore physical seats back to AVAILABLE for other buyers.
    release_ok = False
    release_status_code = None
    release_payload = None
    try:
        rel_resp = _safe_call_http(
            post_http,
            "POST",
            urls["inventory_release"],
            json={"transaction_id": cid, "seat_ids": seat_ids},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        release_status_code = rel_resp.status_code
        if rel_resp.content:
            try:
                release_payload = rel_resp.json()
            except Exception:
                pass
        release_ok = (rel_resp.status_code == 200)
        if not release_ok and isinstance(release_payload, dict):
            msg = str(release_payload.get("message", "")).lower() + " " + str(release_payload.get("error", "")).lower()
            code = str(release_payload.get("code", "")).upper()
            if (
                "already released" in msg
                or "nothing to release" in msg
                or "holds may have already been cancelled or expired" in msg
                or code in ("ALREADY_RELEASED", "NOTHING_TO_RELEASE", "NOT_FOUND")
            ):
                release_ok = True
    except (requests.exceptions.RequestException, Exception):
        release_ok = False

    # 3. Final State Resolution
    # Both compensation legs succeeded: funds returned (or voided) and held seats returned to available pool.
    if release_ok and refund_ok:
        success_status = (
            Transaction.Status.COMPENSATED_ON_RETRY
            if actor == SagaEvent.ACTOR_RETRY
            else Transaction.Status.COMPENSATED
        )
        # Atomically records settled COMPENSATED state in saga log with release outcome payload.
        _set_status_with_event(
            transaction_id=tid,
            to_status=success_status,
            expected_status=expected_final_status,
            actor=actor,
            http_status_code=release_status_code or 200,
            http_payload=release_payload,
        )
        return {
            "success": True,
            "status_code": 200,
            "status": success_status,
            "compensation_response": release_payload,
            "refund_response": refund_payload,
        }
    # Partial failure branch: either refund or seat release failed; routes to compensation failure.
    else:
        failed_status = (
            Transaction.Status.RETRY_COMPENSATION_FAILED
            if actor == SagaEvent.ACTOR_RETRY
            else Transaction.Status.COMPENSATION_FAILED
        )
        # Records COMPENSATION_FAILED in saga log so operators can inspect or retry compensation via staff endpoint.
        _set_status_with_event(
            transaction_id=tid,
            to_status=failed_status,
            expected_status=expected_final_status,
            actor=actor,
            http_status_code=refund_status_code if not refund_ok else (release_status_code or 500),
            http_payload=refund_payload if not refund_ok else release_payload,
        )
        return {
            "success": False,
            "status_code": 500,
            "status": failed_status,
            "compensation_response": release_payload,
            "refund_response": refund_payload,
            "error": "Compensation partially failed",
        }