import logging
import os
import secrets
from decimal import Decimal
import requests
from django.conf import settings
from django.db import transaction as db_transaction, IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from coordinator.models import Transaction, Ticket

logger = logging.getLogger("coordinator")

INVENTORY_BASE_URL = getattr(
    settings,
    "INVENTORY_SERVICE_URL",
    os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000")
).rstrip("/")


def _get_inventory_base_url() -> str:
    """Dynamically resolve inventory service URL with settings/env fallback."""
    return getattr(
        settings,
        "INVENTORY_SERVICE_URL",
        os.getenv("INVENTORY_SERVICE_URL", INVENTORY_BASE_URL)
    ).rstrip("/")


class TicketIssuanceError(Exception):
    def __init__(self, detail, status_code=409):
        """
        By passing only detail to super().__init__(detail),
        self.args=("Seat taken",) Because self.args has exactly 1 item, Python's __str__ returns just that string without parentheses or quotes!

        >2 args tuple is created instead of string
        """
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def issue_tickets_for_transaction(transaction: Transaction) -> list[Ticket]:
    """
    Lazily and idempotently issues tickets for a COMPLETED transaction after
    verifying ground truth against inventory.

    Guards:
    1. Transaction status must be COMPLETED (never issue for FAILED, COMPENSATED, in-flight).
    2. Ground-truth check against inventory MUST return status BOOKED for this exact transaction_id.
       The network call to inventory is made BEFORE any database transaction is opened.
    3. Handles IntegrityError from concurrent requests idempotently by re-fetching.
    """
    if not transaction:
        raise TicketIssuanceError(
            "Cannot issue ticket: Transaction is required.",
            status_code=409,
        )

    # Guard 1: Verify transaction status is COMPLETED before any DB or network work
    if transaction.status != Transaction.Status.COMPLETED:
        raise TicketIssuanceError(
            f"Cannot issue ticket: transaction status is {transaction.status}, expected COMPLETED.",
            status_code=409,
        )

    # Validate seat IDs presence
    seat_ids = transaction.seat_ids or []
    if not seat_ids:
        raise TicketIssuanceError(
            "Cannot issue ticket: Transaction has no associated seat IDs.",
            status_code=409,
        )

    # Fast-path check: Return immediately if tickets are already fully issued
    existing = list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))
    if existing and len(existing) == len(seat_ids):
        return existing

    # Guard 2: Inventory ground truth check BEFORE entering db_transaction.atomic()
    from coordinator.middleware import set_correlation_id
    set_correlation_id(str(transaction.transaction_id))

    inventory_base = _get_inventory_base_url()
    check_url = f"{inventory_base}/seats/check/?transaction_id={transaction.transaction_id}"
    cid = str(transaction.transaction_id)
    headers = {
        "X-Correlation-ID": cid,
        "X-Transaction-ID": cid,
        "X-Internal-Service-Key": getattr(
            settings,
            "INTERNAL_SERVICE_KEY",
            os.getenv("INTERNAL_SERVICE_KEY", "test-internal-key"),
        ),
    }

    timeout = getattr(settings, "REQUEST_TIMEOUT", getattr(settings, "INVENTORY_TIMEOUT", 5))
    try:
        resp = requests.get(check_url, headers=headers, timeout=timeout)
    except Exception as exc:
        logger.error(
            "Failed to reach inventory service for ground-truth check on %s: %s",
            transaction.transaction_id,
            exc,
        )
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

    if not isinstance(data, dict):
        raise TicketIssuanceError("Invalid JSON received from inventory service.", status_code=409)

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

    # 1. Prefer the immutable event snapshot captured on the transaction
    raw_event_name = getattr(transaction, "event_name", None)
    event_name = str(raw_event_name).strip() if raw_event_name and str(raw_event_name).strip() else None
    venue = getattr(transaction, "venue", None) or ""
    starts_at = getattr(transaction, "starts_at", None)

    # 2. If not stored on transaction, extract directly from ground-truth check
    if not event_name:
        event_info = data.get("event")
        if isinstance(event_info, dict):
            event_name = event_info.get("name")
            venue = event_info.get("venue") or venue
            raw_starts_at = event_info.get("starts_at")
            if raw_starts_at:
                if isinstance(raw_starts_at, str):
                    starts_at = parse_datetime(raw_starts_at)
                else:
                    starts_at = raw_starts_at
        elif isinstance(event_info, str) and event_info.strip():
            event_name = event_info.strip()

    # 3. STRICT RULE: Fail fast if event metadata is missing. Never fabricate dummy event names.
    if not event_name or not str(event_name).strip():
        raise TicketIssuanceError(
            "Cannot issue ticket: Event metadata is missing from transaction and inventory records.",
            status_code=409,
        )

    event_name = str(event_name).strip()[:200]
    venue = str(venue or "").strip()[:200]
    if starts_at and timezone.is_naive(starts_at):
        starts_at = timezone.make_aware(starts_at)

    # 4. STRICT RULE: Inventory ground truth must contain confirmed seat records
    seats_info = data.get("seats")
    if not seats_info or not isinstance(seats_info, list):
        raise TicketIssuanceError(
            "Cannot issue ticket: Inventory returned no confirmed seat records.",
            status_code=409,
        )

    # Build lookup maps and track inventory seats
    inventory_seat_ids = set()
    seat_label_map = {}
    seat_amount_map = {}
    for s in seats_info:
        if not isinstance(s, dict):
            continue
        raw_sid = s.get("seat_id") if "seat_id" in s else s.get("id")
        if raw_sid is not None:
            try:
                sid = int(raw_sid)
            except (ValueError, TypeError):
                sid = raw_sid
            inventory_seat_ids.add(sid)

            label = s.get("seat_label") or s.get("label")
            if label and str(label).strip():
                seat_label_map[sid] = str(label).strip()[:64]

            raw_amt = s.get("amount") if "amount" in s else s.get("price")
            if raw_amt is not None:
                try:
                    dec_amt = Decimal(str(raw_amt))
                    if not dec_amt.is_nan() and not dec_amt.is_infinite() and dec_amt >= Decimal("0.00"):
                        seat_amount_map[sid] = dec_amt
                except Exception:
                    pass

    # STRICT RULE: Validate all requested seats upfront before touching database
    for sid in seat_ids:
        try:
            norm_sid = int(sid)
        except (ValueError, TypeError):
            norm_sid = sid

        if norm_sid not in inventory_seat_ids:
            raise TicketIssuanceError(
                f"Cannot issue ticket: Seat ID {sid} was not found in inventory confirmation.",
                status_code=409,
            )
        if norm_sid not in seat_label_map:
            raise TicketIssuanceError(
                f"Cannot issue ticket: Seat label is missing or blank for seat ID {sid}.",
                status_code=409,
            )
        if norm_sid not in seat_amount_map:
            raise TicketIssuanceError(
                f"Cannot issue ticket: Pricing details could not be resolved for seat ID {sid}.",
                status_code=409,
            )

    # Issue tickets inside atomic transaction with row-level serialization and IntegrityError handling
    try:
        with db_transaction.atomic():
            # Synchronize concurrent issuances by locking the parent transaction row
            Transaction.objects.select_for_update().filter(pk=transaction.pk).first()

            # Check again within lock/atomic to avoid duplicate work
            existing = list(Ticket.objects.select_for_update().filter(transaction=transaction).order_by("seat_id"))
            if existing and len(existing) == len(seat_ids):
                return existing

            created_tickets = []
            for sid in seat_ids:
                try:
                    norm_sid = int(sid)
                except (ValueError, TypeError):
                    norm_sid = sid

                # Check if already exists for this seat
                existing_for_seat = Ticket.objects.filter(transaction=transaction, seat_id=norm_sid).first()
                if existing_for_seat:
                    created_tickets.append(existing_for_seat)
                    continue

                label = seat_label_map[norm_sid]
                amount = seat_amount_map[norm_sid]
                code = secrets.token_urlsafe(16)

                ticket = Ticket.objects.create(
                    transaction=transaction,
                    seat_id=norm_sid,
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
        logger.info(
            "IntegrityError on ticket creation for txn %s, re-fetching existing.",
            transaction.transaction_id,
        )
        return list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))