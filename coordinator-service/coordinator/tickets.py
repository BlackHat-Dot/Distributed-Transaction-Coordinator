import logging
import os
import secrets
from decimal import Decimal
import requests
from django.conf import settings
from django.db import transaction as db_transaction, IntegrityError
from django.utils.dateparse import parse_datetime

from coordinator.models import Transaction, Ticket

logger = logging.getLogger("coordinator")

INVENTORY_BASE_URL = getattr(
    settings,
    "INVENTORY_SERVICE_URL",
    os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000")
).rstrip("/")


class TicketIssuanceError(Exception):
    def __init__(self, detail, status_code=409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def issue_tickets_for_transaction(transaction: Transaction, correlation_id: str = None) -> list[Ticket]:
    """
    Lazily and idempotently issues tickets for a COMPLETED transaction after
    verifying ground truth against inventory.

    Guards:
    1. Transaction status must be COMPLETED (never issue for FAILED, COMPENSATED, in-flight).
    2. Ground-truth check against inventory MUST return status BOOKED for this exact transaction_id.
       The network call to inventory is made BEFORE any database transaction is opened.
    3. Handles IntegrityError from concurrent requests idempotently by re-fetching.
    """
    # 0. Check if tickets are already fully issued
    seat_ids = transaction.seat_ids or [1]
    existing = list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))
    if existing and len(existing) == len(seat_ids):
        return existing

    # Guard 1: Verify transaction status is COMPLETED
    if transaction.status != Transaction.Status.COMPLETED:
        raise TicketIssuanceError(
            f"Cannot issue ticket: transaction status is {transaction.status}, expected COMPLETED.",
            status_code=409,
        )

    # Guard 2: Inventory ground truth check BEFORE entering db_transaction.atomic()
    check_url = f"{INVENTORY_BASE_URL}/seats/check/?transaction_id={transaction.transaction_id}"
    headers = {
        "X-Correlation-ID": correlation_id or "ticket-issuance",
        "X-Internal-Service-Key": getattr(settings, "INTERNAL_SERVICE_KEY", os.getenv("INTERNAL_SERVICE_KEY", "test-internal-key")),
    }

    try:
        resp = requests.get(check_url, headers=headers, timeout=5)
    except Exception as exc:
        logger.error(f"Failed to reach inventory service for ground-truth check on {transaction.transaction_id}: {exc}")
        raise TicketIssuanceError(
            "Inventory ground-truth check service unavailable.",
            status_code=409,
        ) from exc

    if resp.status_code != 200:
        raise TicketIssuanceError(
            f"Inventory ground-truth check returned status {resp.status_code}.",
            status_code=409,
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise TicketIssuanceError("Invalid JSON received from inventory service.", status_code=409) from exc

    if data.get("status") != "BOOKED":
        raise TicketIssuanceError(
            f"Ground truth check failed: seat is not BOOKED (inventory status: {data.get('status')}).",
            status_code=409,
        )

    if str(data.get("transaction_id")) != str(transaction.transaction_id):
        raise TicketIssuanceError(
            f"Ground truth check failed: seat is booked by different transaction ({data.get('transaction_id')}).",
            status_code=409,
        )



    # Extract event snapshot
    event_info = data.get("event")
    if not event_info:
        # Fallback: attempt to query /events/
        try:
            ev_resp = requests.get(f"{INVENTORY_BASE_URL}/events/", headers=headers, timeout=5)
            if ev_resp.status_code == 200:
                events_list = ev_resp.json()
                if isinstance(events_list, list) and events_list:
                    event_info = events_list[0]
        except Exception:
            pass

    event_name = "Live Event"
    venue = ""
    starts_at = None
    if event_info:
        event_name = event_info.get("name") or event_name
        venue = event_info.get("venue") or venue
        raw_starts_at = event_info.get("starts_at")
        if raw_starts_at:
            if isinstance(raw_starts_at, str):
                starts_at = parse_datetime(raw_starts_at)
            else:
                starts_at = raw_starts_at

    # Map seat labels and pricing
    seats_info = data.get("seats") or []
    seat_label_map = {}
    seat_amount_map = {}
    for s in seats_info:
        sid = s.get("seat_id") or s.get("id")
        if sid:
            seat_label_map[sid] = s.get("seat_label") or s.get("label") or f"Seat {sid}"
            if "amount" in s or "price" in s:
                seat_amount_map[sid] = Decimal(str(s.get("amount") or s.get("price")))

    # Split human-readable seat numbers if available
    raw_labels = [lbl.strip() for lbl in (transaction.seat_numbers or "").split(",") if lbl.strip()]
    
    # Calculate fallback per-seat amount
    total_amount = transaction.amount or Decimal("0.00")
    num_seats = len(seat_ids)
    fallback_amount = (total_amount / Decimal(num_seats)).quantize(Decimal("0.01")) if num_seats > 0 else Decimal("0.00")

    # Issue tickets inside atomic transaction with IntegrityError handling for concurrency
    try:
        with db_transaction.atomic():
            # Check again within lock/atomic to avoid duplicate work
            existing = list(Ticket.objects.select_for_update().filter(transaction=transaction).order_by("seat_id"))
            if existing and len(existing) == len(seat_ids):
                return existing

            created_tickets = []
            for idx, sid in enumerate(seat_ids):
                # Check if already exists for this seat
                existing_for_seat = Ticket.objects.filter(transaction=transaction, seat_id=sid).first()
                if existing_for_seat:
                    created_tickets.append(existing_for_seat)
                    continue

                label = seat_label_map.get(sid)
                if not label:
                    if idx < len(raw_labels):
                        label = raw_labels[idx]
                    else:
                        label = f"Seat {sid}"

                amount = seat_amount_map.get(sid, fallback_amount)
                code = secrets.token_urlsafe(16)

                ticket = Ticket.objects.create(
                    transaction=transaction,
                    seat_id=sid,
                    seat_label=label,
                    event_name=event_name,
                    venue=venue,
                    starts_at=starts_at,
                    amount=amount,
                    ticket_code=code,
                )
                created_tickets.append(ticket)
            return created_tickets
    except IntegrityError:
        # Concurrent request beat us to it, re-fetch and return the existing tickets
        logger.info(f"IntegrityError on ticket creation for txn {transaction.transaction_id}, re-fetching existing.")
        return list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))
