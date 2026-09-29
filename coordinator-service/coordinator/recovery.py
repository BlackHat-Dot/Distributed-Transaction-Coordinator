import os
import requests
from django.conf import settings
from coordinator.models import Transaction, SagaEvent
from coordinator.saga_log import _set_status_with_event

INVENTORY_BASE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000").rstrip("/")
PAYMENT_BASE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")

PAYMENT_CHECK_URL = f"{PAYMENT_BASE_URL}/payments/check/"
PAYMENT_REFUND_URL = f"{PAYMENT_BASE_URL}/payments/refund/"
PAYMENT_VOID_URL = f"{PAYMENT_BASE_URL}/payments/void/"
INVENTORY_RELEASE_URL = f"{INVENTORY_BASE_URL}/seats/release/"
INVENTORY_CONFIRM_URL = f"{INVENTORY_BASE_URL}/seats/confirm/"
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "2.0"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "5.0")))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
INTERNAL_SERVICE_KEY = getattr(settings, "INTERNAL_SERVICE_KEY", None) or os.getenv("INTERNAL_SERVICE_KEY")

# Single source of truth from Transaction model
TERMINAL_STATUSES = Transaction.TERMINAL_STATUSES
RECOVERABLE_STATUSES = Transaction.RECOVERABLE_STATUSES


def execute_compensation_sweep(
    transaction_id,
    actor=SagaEvent.ACTOR_RECOVERY,
    original_status=None,
    correlation_id=None,
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
    get_http = get_func or requests.get
    post_http = post_func or requests.post

    headers = {
        "X-Correlation-ID": str(correlation_id or transaction_id),
        "X-Internal-Service-Key": INTERNAL_SERVICE_KEY,
        "X-Idempotency-Key": str(transaction_id),
    }

    txn = Transaction.objects.filter(transaction_id=transaction_id).first()
    seat_ids = txn.seat_ids if txn else []

    # 1. Payment Check in EVERY status
    payment_check_failed = False
    payment_status = None
    payment_voided = False
    try:
        p_resp = get_http(
            PAYMENT_CHECK_URL,
            params={"transaction_id": str(transaction_id)},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if p_resp.status_code >= 500:
            payment_check_failed = True
        elif p_resp.status_code == 200:
            p_data = p_resp.json() if p_resp.content else {}
            payment_status = p_data.get("status")
            payment_voided = bool(p_data.get("voided", False))
        else:
            payment_check_failed = True
    except (requests.exceptions.RequestException, Exception):
        payment_check_failed = True

    if payment_check_failed:
        revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
        _set_status_with_event(
            transaction_id=transaction_id,
            to_status=revert_status,
            expected_status=Transaction.Status.RECOVERY_IN_PROGRESS,
            actor=actor,
            http_status_code=None,
            http_payload={"error": "Payment check failed/timed out. Claim released."},
        )
        return {
            "success": False,
            "status_code": 503,
            "status": revert_status,
            "error": "Payment check failed or timed out; claim released for retry",
        }

    # For recovery sweeper, record the COMPENSATING transition before external calls
    if actor == SagaEvent.ACTOR_RECOVERY:
        _set_status_with_event(
            transaction_id=transaction_id,
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
    refund_ok = False
    refund_status_code = None
    refund_payload = None
    # Sentinel: void revealed a late charge → forward-complete instead of compensating
    void_revealed_charge = False

    if payment_status == "REFUNDED" or (payment_status == "NOT_CHARGED" and payment_voided):
        refund_ok = True
    elif payment_status == "NOT_CHARGED":
        # Fence against in-flight charge landing after release
        void_ok = False
        try:
            v_resp = post_http(
                PAYMENT_VOID_URL,
                json={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if v_resp.status_code == 200:
                v_data = v_resp.json() if v_resp.content else {}
                if v_data.get("status") == "CHARGED":
                    payment_status = "CHARGED"  # Late charge landed; route to confirm below
                    void_revealed_charge = True
                else:
                    void_ok = True
        except (requests.exceptions.RequestException, Exception):
            void_ok = False

        if void_ok:
            refund_ok = True
        elif payment_status != "CHARGED":
            # Void failed and payment not confirmed charged -> state uncertain, abort compensation
            revert_status = original_status or Transaction.Status.COMPENSATION_FAILED
            _set_status_with_event(
                transaction_id=transaction_id,
                to_status=revert_status,
                expected_status=expected_final_status,
                actor=actor,
                http_status_code=None,
                http_payload={"error": "Payment void failed/timed out. Claim released."},
            )
            return {
                "success": False,
                "status_code": 503,
                "status": revert_status,
                "error": "Payment void failed; claim released for retry",
            }

    # H1(a): void revealed a late charge → seats still reserved, forward-complete
    if void_revealed_charge:
        confirm_ok = False
        confirm_payload = None
        confirm_status_code = None
        confirm_transport_error = False
        try:
            c_resp = post_http(
                INVENTORY_CONFIRM_URL,
                json={"transaction_id": str(transaction_id), "seat_ids": seat_ids},
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
        except (requests.exceptions.RequestException, Exception):
            confirm_transport_error = True
            confirm_ok = False

        if confirm_ok:
            _set_status_with_event(
                transaction_id=transaction_id,
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
                transaction_id=transaction_id,
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

        # Definitive 4xx error (hold expired, seats not found) -> route to refund and release seats
        payment_status = "CHARGED"

    if payment_status == "CHARGED":
        try:
            r_resp = post_http(
                PAYMENT_REFUND_URL,
                json={"transaction_id": str(transaction_id)},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            refund_status_code = r_resp.status_code
            refund_ok = (r_resp.status_code == 200)
            if r_resp.content:
                try:
                    refund_payload = r_resp.json()
                except Exception:
                    pass
        except (requests.exceptions.RequestException, Exception):
            refund_ok = False
    elif payment_status != "NOT_CHARGED" and payment_status != "REFUNDED":
        refund_ok = False

    # 2. Inventory Release
    release_ok = False
    release_status_code = None
    release_payload = None
    try:
        rel_resp = post_http(
            INVENTORY_RELEASE_URL,
            json={"transaction_id": str(transaction_id), "seat_ids": seat_ids},
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
    if release_ok and refund_ok:
        success_status = (
            Transaction.Status.COMPENSATED_ON_RETRY
            if actor == SagaEvent.ACTOR_RETRY
            else Transaction.Status.COMPENSATED
        )
        _set_status_with_event(
            transaction_id=transaction_id,
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
    else:
        failed_status = (
            Transaction.Status.RETRY_COMPENSATION_FAILED
            if actor == SagaEvent.ACTOR_RETRY
            else Transaction.Status.COMPENSATION_FAILED
        )
        _set_status_with_event(
            transaction_id=transaction_id,
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
