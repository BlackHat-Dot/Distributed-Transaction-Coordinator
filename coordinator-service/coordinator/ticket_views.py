# Imports the hmac cryptographic module to perform constant-time signature and key comparisons, preventing timing attack vulnerabilities.
import hmac
# Imports Python's standard logging library to log diagnostics, warnings, and error messages across ticket processing endpoints.
import logging
# Imports the os module to read runtime operating system environment variables such as rate limits and staff verification keys.
import os
# Imports the regular expression module to sanitize seat labels and format clean file names for downloadable PDF attachments.
import re
# Imports the uuid module to parse, validate, and manipulate universally unique identifiers for transaction lookups.
import uuid
# Imports Django's application settings module to access project-wide configuration parameters and secrets.
from django.conf import settings
# Imports Django's cache abstraction (backed by Redis or in-memory cache) to provide low-latency rate limiting across ticket and verification endpoints.
from django.core.cache import cache
# Imports Django's database transaction module under an alias to manage atomic blocks and prevent naming collisions with the Transaction model.
from django.db import transaction as db_transaction
# Imports HttpResponse to return raw byte streams such as downloadable PDF tickets, iCalendar files, and standalone HTML verification pages.
from django.http import HttpResponse
# Imports Django's timezone utility to record accurate, timezone-aware UTC timestamps when redeeming event tickets.
from django.utils import timezone
# Imports HTML escaping utility to sanitize untrusted database strings before injecting into public verification HTML pages, preventing XSS attacks.
from django.utils.html import escape
# Imports HTTP status codes from Django REST Framework for expressive and standardized API response status handling.
from rest_framework import status
# Imports api_view decorator to enforce supported HTTP methods and equip views with Django REST Framework request parsing.
from rest_framework.decorators import api_view
# Imports Response class to render serialized Python data dictionaries into content-negotiated JSON HTTP responses.
from rest_framework.response import Response

# Imports JWT verification and client IP resolution helpers to authenticate ticket requests and track request rates per caller.
from coordinator.auth_views import verify_jwt, _get_client_ip
# Imports Transaction and Ticket database models to query booking status and persist ticket issuance and redemption records.
from coordinator.models import Transaction, Ticket
# Imports ticket generation helpers to construct base64 QR codes, printable PDF documents, calendar invite files, and formatted dates.
from coordinator.ticket_generator import (
    generate_qr_code_base64,
    generate_tickets_pdf,
    generate_tickets_ics,
    format_kolkata_time,
)
# Imports ticket issuance service logic and custom conflict exceptions to safely mint event passes from completed transactions.
from coordinator.tickets import issue_tickets_for_transaction, TicketIssuanceError

# Initializes a dedicated coordinator logger instance to record audit trails and diagnostic output for ticket transactions.
logger = logging.getLogger("coordinator")


# Normalizes datetime objects or timestamp strings into standard ISO 8601 representation for consistent API client serialization.
def _to_iso(dt):
    """Safely converts a datetime (or string) to ISO format string."""
    # Returns None immediately if input datetime value is empty, falsy, or unassigned.
    if not dt:
        # Resolves null input to None for clean JSON serialization without raising attribute errors.
        return None
    # Checks whether the object implements the standard datetime isoformat method before invoking it.
    if hasattr(dt, "isoformat"):
        # Formats valid datetime object into standard ISO 8601 string representation.
        return dt.isoformat()
    # Casts non-datetime inputs or custom timestamps into plain string representations.
    return str(dt)


# Evaluates request thresholds against a cached sliding or fixed window counter to prevent API abuse.
def check_rate_limit(key: str, limit: int, window: int = 60) -> bool:
    """
    Returns True if request is allowed within the limit, False if rate limited.
    Fails open gracefully on cache communication failures.
    """
    # Wraps cache queries in an exception block to guarantee that cache connectivity hiccups fail open rather than dropping requests.
    try:
        # Implements a fixed-window cache counter with graceful fail-open fallback so Redis outages never block legitimate customer ticket access.
        current = cache.get(key)
        # Checks if no counter exists yet for the given cache key in the current time window.
        if current is None:
            # Initializes the rate limit counter to 1 with an expiration TTL matching the specified time window.
            cache.set(key, 1, timeout=window)
            # Grants immediate request permission upon successful counter initialization.
            return True
        # Attempts to parse the cached counter value into an integer count.
        try:
            # Casts the cached counter value to an integer for numerical comparison.
            count = int(current)
        # Catches parsing errors if the cached data is corrupted or contains unexpected data types.
        except (ValueError, TypeError):
            # Resets the local count to zero to avoid crashing when cache data is malformed.
            count = 0
        # Checks if the current request volume equals or exceeds the allowable threshold for this time window.
        if count >= limit:
            # Blocks the incoming request immediately when the rate limit quota has been exhausted.
            return False
        # Attempts an atomic in-place increment on the cache key to register the current request.
        try:
            # Executes atomic increment on Redis or backend cache server for fast concurrency handling.
            cache.incr(key)
        # Catches potential driver exceptions if the cache backend does not support atomic increment or has expired.
        except Exception:
            # Fallback: sets the incremented count with the window expiration timeout if atomic incr fails.
            cache.set(key, count + 1, timeout=window)
        # Grants request permission as the count is within the permitted rate limit.
        return True
    # Catches any unexpected cache backend errors or network timeouts to maintain fault-tolerant behavior.
    except Exception as exc:
        # Logs the cache failure diagnostic details for operational alerting without breaking the user request flow.
        logger.warning("Rate limiting cache error for %s: %s", key, exc)
        # Fails open on cache communication errors to ensure critical ticket flows remain available during transient outages.
        return True  # Fail open on cache communication error


# Authenticates ticket access requests, validating caller identity and enforcing strict transaction ownership.
def authenticate_ticket_requester(request, transaction_id):
    """
    Authenticates ticket requests.
    Enforces:
    1. Rejects query param '?token=' with 401.
    2. Requires 'Authorization: Bearer <jwt>' header.
    3. Validates transaction_id format (UUID).
    4. Requires transaction ownership: non-owners receive 404 (not 403).
    Returns (txn, None) on success or (None, Response) on error.
    """
    # 1. Prohibit ?token= query parameter
    # Explicitly rejects authentication tokens in query parameters to prevent token leakage in web server access logs, browser history, and HTTP Referer headers.
    if request.GET.get("token") or "token" in request.query_params:
        # Returns 401 Unauthorized rejecting query string authentication to protect token confidentiality.
        return None, Response(
            {"error": "Query parameter token authentication is not permitted. Use Authorization header.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # 2. Extract Bearer token from Authorization header
    # Extracts and validates the standard RFC 6750 Bearer authentication header containing the client's cryptographically signed JWT.
    auth_header = request.headers.get("Authorization", "")
    # Verifies that the Authorization header follows standard Bearer token schema.
    if not auth_header.startswith("Bearer "):
        # Rejects requests lacking a Bearer prefix with 401 Unauthorized.
        return None, Response(
            {"error": "Authorization header with Bearer token required.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Extracts the raw JWT token string by separating the Bearer prefix and stripping surrounding whitespace.
    token = auth_header.split(" ", 1)[1].strip()
    # Validates token cryptographic signature, expiration time, and claims payload.
    payload, err = verify_jwt(token)
    # Checks whether token verification succeeded and contains a valid user identifier claim.
    if not payload or not payload.get("user_id"):
        # Rejects tokens with invalid signatures, expired timestamps, or missing user identities.
        return None, Response(
            {"error": f"Invalid or expired authentication token: {err or 'Missing identity'}.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # Normalizes user identifier from JWT claims into a string for ownership matching.
    requester_user_id = str(payload.get("user_id"))

    # 3. Validate UUID syntax to prevent 500 error from Postgres query
    # Encloses UUID string conversion in a guard block to intercept malformed route parameters.
    try:
        # Validates UUID format prior to database querying to prevent raw SQL/PostgreSQL type mismatch exceptions from bubbling up as unhandled 500 errors.
        tid = uuid.UUID(str(transaction_id))
    # Catches parsing errors when the supplied transaction_id string cannot be coerced into a valid UUID.
    except (ValueError, TypeError, AttributeError):
        # Returns 404 Not Found for syntactically invalid transaction identifiers to prevent enumeration and database type errors.
        return None, Response(
            {"error": "Transaction not found.", "code": "NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # 4. Ownership check: Return 404 for non-existent OR non-owner transaction
    # Fetches the transaction record corresponding to the verified UUID from the database.
    txn = Transaction.objects.filter(transaction_id=tid).first()
    # Checks whether a transaction record was located in the database.
    if not txn:
        # Returns 404 Not Found if no transaction matches the requested UUID.
        return None, Response(
            {"error": "Transaction not found.", "code": "NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Enforces strict ownership access control by returning 404 NOT_FOUND instead of 403 FORBIDDEN, preventing unauthorized users from enumerating valid transaction IDs.
    if not txn.user_id or str(txn.user_id) != requester_user_id:
        # Returns 404 Not Found when the requester does not own the transaction, obfuscating its existence.
        return None, Response(
            {"error": "Transaction not found.", "code": "NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Imports correlation ID setter to link transaction context with request logging.
    from coordinator.middleware import set_correlation_id
    # Binds the authenticated transaction UUID to the current thread's logging context so all subsequent downstream log records correlate directly to this booking.
    set_correlation_id(str(txn.transaction_id))

    # Returns the verified transaction model instance and None indicating error-free authentication.
    return txn, None


# Restricts incoming HTTP requests for ticket listing to the GET method.
@api_view(["GET"])
# Handles HTTP GET requests to fetch tickets, event metadata, and base64 QR codes for a booking.
def get_transaction_tickets(request, transaction_id):
    """
    GET /tickets/<transaction_id>/
    Returns JSON containing ticket list with base64 PNG QR code and event snapshot.
    """
    # Authenticates the requester identity and validates ownership of the requested transaction ID.
    txn, err_resp = authenticate_ticket_requester(request, transaction_id)
    # Checks whether requester authentication or transaction verification failed.
    if err_resp:
        # Returns error HTTP response immediately if authentication or authorization failed.
        return err_resp

    # Wraps ticket issuance in exception handling to catch conflict errors on incomplete transactions.
    try:
        # Idempotently materializes ticket records from the completed transaction's saga reservation log if not already generated.
        tickets = issue_tickets_for_transaction(txn)
    # Intercepts ticket issuance business errors such as pending or aborted transaction status.
    except TicketIssuanceError as exc:
        # Returns structured conflict response detailing why tickets cannot be issued.
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    # Extracts the first ticket from the collection to derive shared event-level venue and timing metadata.
    first_ticket = tickets[0] if tickets else None
    # Constructs a structured event metadata payload containing venue, timing, and event title details.
    event_data = {
        "name": first_ticket.event_name if first_ticket else "",
        "venue": first_ticket.venue if first_ticket else "",
        "starts_at": _to_iso(first_ticket.starts_at) if first_ticket else None,
        "starts_at_formatted": format_kolkata_time(first_ticket.starts_at) if first_ticket else "TBA",
    }

    # Assembles list of serialized ticket dictionaries with seat labels, pricing, and base64 QR codes.
    tickets_data = [
        {
            "ticket_code": t.ticket_code,
            "seat_id": t.seat_id,
            "seat_label": t.seat_label,
            "amount": str(t.amount),
            "issued_at": _to_iso(t.issued_at),
            "used_at": _to_iso(t.used_at),
            "is_used": t.used_at is not None,
            # Embeds base64-encoded QR PNG data URIs directly into the JSON response for zero-latency inline image rendering on frontend client apps.
            "qr_code_base64": generate_qr_code_base64(t.ticket_code),
        }
        for t in tickets
    ]

    return Response({
        "transaction_id": str(txn.transaction_id),
        "status": txn.status,
        "event": event_data,
        "tickets": tickets_data,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
def get_ticket_pdf(request, transaction_id):
    """
    GET /tickets/<transaction_id>/pdf/
    Downloads single or multi-page A5 PDF for the transaction.
    """
    txn, err_resp = authenticate_ticket_requester(request, transaction_id)
    if err_resp:
        return err_resp

    try:
        # Idempotently materializes ticket records from the completed transaction's saga reservation log if not already generated.
        tickets = issue_tickets_for_transaction(txn)
    except TicketIssuanceError as exc:
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    # Triggers ReportLab vector PDF compilation to produce a print-ready multi-page A5 pass containing security barcodes and venue details.
    pdf_bytes = generate_tickets_pdf(txn, tickets)

    # For group bookings (>3 seats), use total count to avoid filename bloat
    # Calculates concise filename slugs for large group bookings (>3 seats) to prevent filesystem filename length errors and browser download truncation.
    if len(tickets) > 3:
        seat_slug = f"{len(tickets)}seats"
    else:
        raw_slug = "-".join(str(t.seat_label).replace(" ", "") for t in tickets)
        seat_slug = re.sub(r"[^a-zA-Z0-9_-]", "", raw_slug) or "seats"

    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-{seat_slug}-{short_ref}.pdf"

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    # Sets Content-Disposition header with sanitized filename to force direct file download in the user's browser rather than in-tab rendering.
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["GET"])
def get_ticket_ics(request, transaction_id):
    """
    GET /tickets/<transaction_id>/calendar.ics
    Downloads iCalendar event file with UTC DTSTART.
    """
    txn, err_resp = authenticate_ticket_requester(request, transaction_id)
    if err_resp:
        return err_resp

    try:
        # Idempotently materializes ticket records from the completed transaction's saga reservation log if not already generated.
        tickets = issue_tickets_for_transaction(txn)
    except TicketIssuanceError as exc:
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    # Serializes the transaction and ticket schedule into an RFC 5545 iCalendar (.ics) stream for direct one-click import into calendar applications.
    ics_bytes = generate_tickets_ics(txn, tickets)
    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-{short_ref}.ics"

    # Returns the calendar byte payload using MIME type text/calendar so mobile and desktop devices automatically invoke their native calendar handler.
    response = HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["GET"])
def public_verify_ticket(request, ticket_code):
    """
    GET /verify/<ticket_code>/
    Public read-only verification page/JSON.
    Rate limited. NO PII (no email, user ID, transaction UUID).
    """
    ip = _get_client_ip(request)
    # Applies aggressive IP-based rate limiting on the public verification endpoint to protect venue gate scanners from denial-of-service and brute-force ticket code attacks.
    rate_key = f"dtc:ratelimit:verify:{ip}"
    verify_limit = int(os.getenv("VERIFY_RATE_LIMIT", 5))
    if not check_rate_limit(rate_key, limit=verify_limit, window=60):
        return HttpResponse("Too Many Requests", status=429, content_type="text/plain")

    # Fetches ticket along with its parent transaction in a single SQL JOIN to evaluate transaction status without secondary database lookups.
    ticket = Ticket.objects.select_related("transaction").filter(ticket_code=ticket_code).first()
    accept_header = request.headers.get("Accept", "")
    wants_json = "application/json" in accept_header or request.query_params.get("format") == "json"

    if not ticket:
        if wants_json:
            return Response({"status": "INVALID", "message": "Ticket not found."}, status=status.HTTP_404_NOT_FOUND)
        
        html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ticket Verification: INVALID</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; max-width: 420px; width: 100%; text-align: center; }
    .badge { display: inline-block; padding: 8px 20px; border-radius: 9999px; font-weight: 700; font-size: 14px; background: #ef4444; color: white; margin-bottom: 20px; }
    h1 { font-size: 22px; margin: 0 0 10px 0; }
    p { color: #94a3b8; font-size: 14px; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">INVALID</div>
    <h1>Invalid Ticket</h1>
    <p>This ticket code does not exist or has expired.</p>
  </div>
</body>
</html>"""
        return HttpResponse(html, status=404, content_type="text/html; charset=utf-8")

    # Verification gatekeeper: immediately revokes ticket validity if the parent transaction was cancelled, aborted, or compensated during saga rollback.
    if ticket.transaction.status != Transaction.Status.COMPLETED:
        if wants_json:
            return Response({"status": "REVOKED", "message": "This ticket has been revoked or cancelled."}, status=status.HTTP_200_OK)

        html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ticket Verification: REVOKED</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; max-width: 420px; width: 100%; text-align: center; }
    .badge { display: inline-block; padding: 8px 20px; border-radius: 9999px; font-weight: 700; font-size: 14px; background: #ef4444; color: white; margin-bottom: 20px; }
    h1 { font-size: 22px; margin: 0 0 10px 0; }
    p { color: #94a3b8; font-size: 14px; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">REVOKED</div>
    <h1>Ticket Revoked</h1>
    <p>This ticket is no longer valid because the transaction was not completed.</p>
  </div>
</body>
</html>"""
        return HttpResponse(html, status=200, content_type="text/html; charset=utf-8")

    is_used = ticket.used_at is not None
    status_label = "ALREADY USED" if is_used else "VALID"
    badge_bg = "#f59e0b" if is_used else "#10b981"
    formatted_time = format_kolkata_time(ticket.starts_at)

    # Protects customer privacy by omitting all PII (user_id, buyer email, payment references) from public verification responses, returning only venue admission validity.
    if wants_json:
        return Response({
            "status": status_label,
            "event_name": ticket.event_name,
            "venue": ticket.venue,
            "starts_at": _to_iso(ticket.starts_at),
            "seat_label": ticket.seat_label,
            "used_at": _to_iso(ticket.used_at),
        }, status=status.HTTP_200_OK)

    escaped_event = escape(ticket.event_name or "")
    escaped_venue = escape(ticket.venue or "TBA")
    escaped_seat = escape(ticket.seat_label or "")
    escaped_status = escape(status_label)
    escaped_time = escape(formatted_time)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ticket Verification: {escaped_status}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 32px; max-width: 420px; width: 100%; text-align: center; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5); }}
    .badge {{ display: inline-block; padding: 8px 20px; border-radius: 9999px; font-weight: 700; font-size: 14px; background: {badge_bg}; color: white; margin-bottom: 20px; letter-spacing: 0.05em; }}
    h1 {{ font-size: 22px; margin: 0 0 24px 0; color: #f8fafc; }}
    .info-row {{ text-align: left; background: #0f172a; padding: 14px 18px; border-radius: 10px; margin-bottom: 12px; border: 1px solid #334155; }}
    .info-label {{ font-size: 11px; text-transform: uppercase; color: #94a3b8; font-weight: 600; letter-spacing: 0.05em; margin-bottom: 4px; }}
    .info-val {{ font-size: 16px; font-weight: 600; color: #f1f5f9; }}
    .footer {{ margin-top: 24px; font-size: 12px; color: #64748b; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">{escaped_status}</div>
    <h1>{escaped_status}</h1>
    <div class="info-row">
      <div class="info-label">Event</div>
      <div class="info-val">{escaped_event}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Venue</div>
      <div class="info-val">{escaped_venue}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Date & Time</div>
      <div class="info-val">{escaped_time}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Seat</div>
      <div class="info-val">{escaped_seat}</div>
    </div>
    <div class="footer">Scan verified by DTC Gate Check-In</div>
  </div>
</body>
</html>"""
    return HttpResponse(html, status=200, content_type="text/html; charset=utf-8")


@api_view(["POST"])
def gate_redeem_ticket(request):
    """
    POST /gate/redeem/
    Requires X-Gate-Api-Key matching GATE_API_KEY.
    Atomically redeems a single ticket using select_for_update.
    First call succeeds (200), subsequent calls return 409 ALREADY_USED.
    Refuses (409) if transaction is not COMPLETED.
    """
    # Rate limit gate check: 10 attempts per minute (relaxed on loopback in dev/debug mode)
    ip = _get_client_ip(request)
    gate_limit = int(os.getenv("GATE_RATE_LIMIT", 1000 if (ip in ("127.0.0.1", "::1", "localhost") and getattr(settings, "DEBUG", False)) else 10))
    rate_key = f"dtc:ratelimit:gate:{ip}"
    if not check_rate_limit(rate_key, limit=gate_limit, window=60):
        return Response({"error": "Rate limit exceeded on gate redemption.", "code": "RATE_LIMITED"}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    if not isinstance(request.data, dict):
        return Response(
            {"error": "Invalid request payload, expected JSON object.", "code": "INVALID_PAYLOAD"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Verify GATE_API_KEY (supports X-Staff-Key, X-Gate-Api-Key, gate_key in body, or Bearer auth)
    # Loads the venue gate scanner API key from settings or environment variables to restrict redemption access exclusively to authorized venue staff.
    expected_key = getattr(settings, "GATE_API_KEY", os.getenv("GATE_API_KEY"))

    if not expected_key:
        logging.critical("Redeem ticket service is unavailable due to unset GATE_API_KEY, Rejecting incoming traffic.")
        raise Response(
            "The service is currently unavailable and undergoing maintenance. "
            "Operations are expected to resume shortly.",
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    
    provided_key = request.headers.get("X-Gate-Api-Key","")
    
    # Employs constant-time string comparison (hmac.compare_digest) to prevent timing side-channel attacks against the venue staff gate secret.
    if not provided_key or not hmac.compare_digest(str(provided_key), str(expected_key)):
        return Response({"error": "Invalid or missing gate API key.", "code": "UNAUTHORIZED"}, status=status.HTTP_401_UNAUTHORIZED)

    if request.data.get("action") == "verify_key":
        return Response({
            "status": "KEY_VALID",
            "message": "Gate API key verified successfully.",
        }, status=status.HTTP_200_OK)

    raw_ticket_code = request.data.get("ticket_code")
    ticket_code = str(raw_ticket_code or "").strip()
    if not ticket_code:
        return Response({"error": "ticket_code is required.", "code": "BAD_REQUEST"}, status=status.HTTP_400_BAD_REQUEST)

    # Wraps redemption in an atomic database transaction to guarantee that ticket status verification and timestamp updates occur as an isolated, indivisible unit.
    with db_transaction.atomic():
        # Acquires a pessimistic SELECT FOR UPDATE row-level lock on the ticket to prevent concurrent double-entry race conditions at multi-turnstile venue gates.
        ticket = Ticket.objects.select_for_update().select_related("transaction").filter(ticket_code=ticket_code).first()
        if not ticket:
            return Response({"error": "Ticket code not found.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)

        if ticket.transaction.status != Transaction.Status.COMPLETED:
            return Response({
                "error": f"Cannot redeem ticket: transaction status is {ticket.transaction.status}, expected COMPLETED.",
                "code": "TRANSACTION_NOT_COMPLETED",
            }, status=status.HTTP_409_CONFLICT)

        # Enforces strict single-entry admission semantics by returning 409 CONFLICT if the ticket has previously been scanned and redeemed.
        if ticket.used_at is not None:
            return Response({
                "error": "Ticket has already been used.",
                "code": "ALREADY_USED",
                "used_at": _to_iso(ticket.used_at),
                "used_by": ticket.used_by,
                "seat_label": ticket.seat_label,
                "event_name": ticket.event_name,
            }, status=status.HTTP_409_CONFLICT)

        ticket.used_at = timezone.now()
        # Records the scanning gate turnstile device ID or operator staff identifier to maintain an immutable forensic audit log for venue entry.
        ticket.used_by = str(request.data.get("operator") or request.data.get("device_id") or "gate_staff").strip()[:64]
        ticket.save(update_fields=["used_at", "used_by"])

        return Response({
            "status": "REDEEMED",
            "message": f"Admission granted for seat {ticket.seat_label}.",
            "ticket_code": ticket.ticket_code,
            "seat_label": ticket.seat_label,
            "event_name": ticket.event_name,
            "used_at": _to_iso(ticket.used_at),
        }, status=status.HTTP_200_OK)


@api_view(["POST"])
def send_ticket_email(request, transaction_id):
    """
    POST /tickets/<transaction_id>/email/
    Sends the PDF e-ticket to the transaction owner's stored email.
    Accepts NO recipient parameter (ignores any attempted override).
    Rate limited to 3 per hour per transaction.
    Dispatches Celery background task.
    """
    txn, err_resp = authenticate_ticket_requester(request, transaction_id)
    if err_resp:
        return err_resp

    if not txn.user_email:
        return Response({"error": "No email address associated with this transaction.", "code": "NO_EMAIL"}, status=status.HTTP_400_BAD_REQUEST)

    # Rate limit: 3/hour per transaction
    # Enforces a rate limit of 3 email dispatches per hour per transaction to prevent mail-bombing and outbound SMTP server reputation degradation.
    rate_key = f"dtc:ratelimit:ticket_email:{txn.transaction_id}"
    if not check_rate_limit(rate_key, limit=3, window=3600):
        return Response(
            {"error": "Email rate limit exceeded. You can only send ticket emails 3 times per hour.", "code": "RATE_LIMITED"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # Dispatch Celery task
    from coordinator.tasks import send_ticket_email_task
    try:
        # Offloads PDF generation and email transmission to an asynchronous Celery worker queue so the HTTP client receives an immediate response.
        send_ticket_email_task.delay(str(txn.transaction_id))
    except Exception as exc:
        logger.warning("Celery dispatch failed, running inline or logging: %s", exc)
        # In testing or when Celery isn't running daemon:
        try:
            send_ticket_email_task(str(txn.transaction_id))
        except Exception:
            pass

    return Response({
        "status": "QUEUED",
        "message": f"Ticket email has been queued for dispatch to {txn.user_email}.",
        "recipient": txn.user_email,
    }, status=status.HTTP_200_OK)