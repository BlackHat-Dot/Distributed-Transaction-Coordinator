# Imports Python standard logging library to log ticket issuance lifecycles and ground-truth verification errors.
import logging
# Imports standard OS library to inspect operating system environment variables for inventory endpoint resolution.
import os
# Imports Python's cryptographically secure random number generator to create unguessable admission ticket codes.
import secrets
# Imports Decimal class to ensure fixed-point financial precision when calculating seat admission pricing.
from decimal import Decimal
# Imports requests library to execute synchronous HTTP ground-truth probes against Inventory Service.
import requests
# Imports Django runtime settings module to access configured microservice base URLs and timeouts.
from django.conf import settings
# Imports database transaction control and IntegrityError exception to handle atomic serializable blocks.
from django.db import transaction as db_transaction, IntegrityError
# Imports Django timezone utilities to normalize event start timestamps into timezone-aware datetime instances.
from django.utils import timezone
# Imports ISO-8601 date-time string parser to deserialize event scheduling timestamps from inventory payloads.
from django.utils.dateparse import parse_datetime

# Imports Transaction and Ticket database models to query booking status and persist minted tickets.
from coordinator.models import Transaction, Ticket

# Configures module logger scoped to coordinator logging namespace for structured audit tracing.
logger = logging.getLogger("coordinator")


# Dynamically resolves the base URL for Inventory Service, prioritizing Django settings over environment variables.
def _get_inventory_base_url() -> str:
    """Dynamically resolve inventory service URL with settings/env fallback."""
    return (getattr(
        settings,
        "INVENTORY_SERVICE_URL",
        None
    ) or os.getenv("INVENTORY_SERVICE_URL", "http://127.0.0.1:8000")).rstrip("/")


# Custom domain exception raised when ticket issuance preconditions or inventory ground-truth validations fail.
class TicketIssuanceError(Exception):
    # Initializes custom ticket error with human-readable detail message and default 409 Conflict status code.
    def __init__(self, detail, status_code=409):
        """
        """
        By passing only detail to super().__init__(detail),
        self.args=("Seat taken",) Because self.args has exactly 1 item, Python's __str__ returns just that string without parentheses or quotes!

        >2 args tuple is created instead of string
        """
        # Passes message string directly to base Exception class to guarantee single-string __str__ formatting.
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# Idempotently generates digital ticket records for a settled COMPLETED transaction after verifying ground truth against Inventory.
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
    # Guard clause: verifies that a non-null Transaction instance is provided before proceeding with ticket issuance.
    if not transaction:
        raise TicketIssuanceError(
            "Cannot issue ticket: Transaction is required.",
            status_code=409,
        )

    # Guard 1: Verify transaction status is COMPLETED before any DB or network work
    # Guard 1: Enforces that tickets are only ever minted for settled COMPLETED transactions to prevent tickets on failed orders.
    if transaction.status != Transaction.Status.COMPLETED:
        raise TicketIssuanceError(
            f"Cannot issue ticket: transaction status is {transaction.status}, expected COMPLETED.",
            status_code=409,
        )

    # Validate seat IDs presence
    # Extracts the reserved seat identifiers from the transaction, defaulting to empty list if unset.
    seat_ids = transaction.seat_ids or []
    if not seat_ids:
        raise TicketIssuanceError(
            "Cannot issue ticket: Transaction has no associated seat IDs.",
            status_code=409,
        )

    # Fast-path check: Return immediately if tickets are already fully issued
    # Fast-path check: Returns immediately if all tickets for this transaction have already been minted, bypassing network calls.
    # Queries database for existing tickets belonging to this transaction ordered deterministically by seat ID.
    existing = list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))
    # Evaluates fast-path return: if all tickets already exist in the database, returns them immediately.
    if existing and len(existing) == len(seat_ids):
        return existing

    # Guard 2: Inventory ground truth check BEFORE entering db_transaction.atomic()
    # Lazy-loads correlation utility and seeds current execution context with transaction ID for distributed tracing.
    from coordinator.middleware import set_correlation_id
    set_correlation_id(str(transaction.transaction_id))

    # Resolves Inventory Service base URL with runtime settings and environment fallback overrides.
    inventory_base = _get_inventory_base_url()
    # Constructs the inventory verification endpoint URL to confirm physical seat booking state before opening a database write transaction.
    check_url = f"{inventory_base}/seats/check/?transaction_id={transaction.transaction_id}"
    # Converts transaction UUID to string representation for inclusion in tracing headers and query parameters.
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

    # Configures HTTP request timeout in seconds with fallback to 5s to avoid holding connections.
    timeout = getattr(settings, "REQUEST_TIMEOUT", getattr(settings, "INVENTORY_TIMEOUT", 5))
    # Dispatches synchronous HTTP GET request to check Inventory ground-truth status for this booking.
    try:
        # Outbound HTTP check: queries Inventory Service outside any database lock to prevent holding PostgreSQL transaction locks during network I/O.
        resp = requests.get(check_url, headers=headers, timeout=timeout)
    # Catches transport and network errors during inventory verification, escalating to 409 TicketIssuanceError.
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

    # Validates HTTP 200 OK response from Inventory Service before attempting to parse response body.
    if resp.status_code != 200:
        raise TicketIssuanceError(
            f"Inventory ground-truth check returned status {resp.status_code}.",
            status_code=409,
        )

    # Parses JSON response payload from inventory service containing ground-truth reservation status.
    try:
        data = resp.json()
    # Handles JSON decoding errors by raising a formatted 409 Conflict domain error.
    except Exception as exc:
        raise TicketIssuanceError("Invalid JSON received from inventory service.", status_code=409) from exc

    # Schema validation: confirms the parsed inventory payload is a valid JSON dictionary.
    if not isinstance(data, dict):
        raise TicketIssuanceError("Invalid JSON received from inventory service.", status_code=409)

    # Ground-truth validation: ensures the seats are confirmed BOOKED in Inventory Service rather than released or held.
    if data.get("status") != "BOOKED":
        raise TicketIssuanceError(
            f"Ground truth check failed: seat is not BOOKED (inventory status: {data.get('status')}).",
            status_code=409,
        )

    # Anti-spoofing guard: verifies that Inventory Service attributes the booking to this exact transaction ID.
    if str(data.get("transaction_id")) != str(transaction.transaction_id):
        raise TicketIssuanceError(
            f"Ground truth check failed: seat is booked by different transaction ({data.get('transaction_id')}).",
            status_code=409,
        )

    # 1. Prefer the immutable event snapshot captured on the transaction
    # Prefers the immutable event snapshot captured on the transaction row at checkout time to prevent display discrepancies.
    # Retrieves event name snapshot captured at booking creation, stripping whitespace if present.
    raw_event_name = getattr(transaction, "event_name", None)
    event_name = str(raw_event_name).strip() if raw_event_name and str(raw_event_name).strip() else None
    # Resolves immutable venue name and start timestamp attributes from the transaction entity.
    venue = getattr(transaction, "venue", None) or ""
    starts_at = getattr(transaction, "starts_at", None)

    # 2. If not stored on transaction, extract directly from ground-truth check
    # Fallback: extracts event metadata (name, venue, start time) from the inventory check payload if omitted from transaction.
    # Evaluates whether event metadata needs to be recovered from inventory payload if missing on transaction.
    if not event_name:
        # Extracts event metadata block from inventory ground-truth response dictionary.
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
    # Strict fail-fast rule: aborts issuance if event metadata is completely absent; never fabricates dummy placeholder event names.
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
    # Extracts the confirmed seats array returned by Inventory Service containing seat labels, row identifiers, and pricing.
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
    # Parses confirmed seat records from inventory into normalized seat ID, seat label, and amount lookup maps.
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
                    # Enforces exact decimal financial precision for individual seat prices to avoid floating-point inaccuracies.
                    dec_amt = Decimal(str(raw_amt))
                    if not dec_amt.is_nan() and not dec_amt.is_infinite() and dec_amt >= Decimal("0.00"):
                        seat_amount_map[sid] = dec_amt
                except Exception:
                    pass

    # STRICT RULE: Validate all requested seats upfront before touching database
    # Validates that every requested seat ID was explicitly confirmed and priced by Inventory Service before mutating the database.
    for sid in seat_ids:
        try:
            norm_sid = int(sid)
        except (ValueError, TypeError):
            norm_sid = sid

        # Aborts issuance if any requested seat is missing from the inventory confirmation response.
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
        # Opens an atomic database transaction to serialize concurrent ticket creations for this transaction.
        with db_transaction.atomic():
            # Synchronize concurrent issuances by locking the parent transaction row
            # Acquires an exclusive row-level lock on the parent transaction to serialize parallel issuance requests.
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
                # Generates a cryptographically random, URL-safe 128-bit entropy admission code for entry turnstile scanners.
                code = secrets.token_urlsafe(16)

                # Inserts the permanent Ticket record linking the seat, transaction, venue details, and unique admission code.
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
    # Concurrency handler: if a simultaneous worker created the tickets concurrently, catches unique constraint violation gracefully.
    except IntegrityError:
        # Concurrent request beat us to it, re-fetch and return the existing tickets
        logger.info(
            "IntegrityError on ticket creation for txn %s, re-fetching existing.",
            transaction.transaction_id,
        )
        # Re-fetches and returns the already-committed tickets, guaranteeing idempotent return semantics for concurrent callers.
        return list(Ticket.objects.filter(transaction=transaction).order_by("seat_id"))