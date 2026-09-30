import hmac
import logging
import os
import uuid
from django.conf import settings
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from coordinator.auth_views import verify_jwt, _get_client_ip
from coordinator.models import Transaction, Ticket
from coordinator.ticket_generator import (
    generate_qr_code_base64,
    generate_tickets_pdf,
    generate_tickets_ics,
    format_kolkata_time,
)
from coordinator.tickets import issue_tickets_for_transaction, TicketIssuanceError

logger = logging.getLogger("coordinator")


def check_rate_limit(key: str, limit: int, window: int = 60) -> bool:
    """
    Returns True if request is allowed within the limit, False if rate limited.
    """
    try:
        current = cache.get(key)
        if current is None:
            cache.set(key, 1, timeout=window)
            return True
        if int(current) >= limit:
            return False
        try:
            cache.incr(key)
        except Exception:
            cache.set(key, int(current) + 1, timeout=window)
        return True
    except Exception as exc:
        logger.warning(f"Rate limiting cache error for {key}: {exc}")
        return True  # Fail open on cache communication error


def authenticate_ticket_requester(request, transaction_id):
    """
    Authenticates ticket requests.
    Enforces:
    1. Rejects query param '?token=' with 401.
    2. Requires 'Authorization: Bearer <jwt>' header.
    3. Requires transaction ownership: non-owners receive 404 (not 403).
    Returns (txn, None) on success or (None, Response) on error.
    """
    # 1. Prohibit ?token= query parameter
    if request.GET.get("token") or "token" in request.query_params:
        return None, Response(
            {"error": "Query parameter token authentication is not permitted. Use Authorization header.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # 2. Extract Bearer token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, Response(
            {"error": "Authorization header with Bearer token required.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    token = auth_header.split(" ", 1)[1].strip()
    payload, err = verify_jwt(token)
    if not payload or not payload.get("user_id"):
        return None, Response(
            {"error": f"Invalid or expired authentication token: {err or 'Missing identity'}.", "code": "UNAUTHORIZED"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    requester_user_id = str(payload.get("user_id"))

    # 3. Ownership check: Return 404 for non-existent OR non-owner transaction
    txn = Transaction.objects.filter(transaction_id=transaction_id).first()
    if not txn:
        return None, Response(
            {"error": "Transaction not found.", "code": "NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    if str(txn.user_id) != requester_user_id:
        return None, Response(
            {"error": "Transaction not found.", "code": "NOT_FOUND"},
            status=status.HTTP_404_NOT_FOUND,
        )

    return txn, None


@api_view(["GET"])
def get_transaction_tickets(request, transaction_id):
    """
    GET /tickets/<transaction_id>/
    Returns JSON containing ticket list with base64 PNG QR code and event snapshot.
    """
    txn, err_resp = authenticate_ticket_requester(request, transaction_id)
    if err_resp:
        return err_resp

    try:
        tickets = issue_tickets_for_transaction(txn)
    except TicketIssuanceError as exc:
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    first_ticket = tickets[0] if tickets else None
    event_data = {
        "name": first_ticket.event_name if first_ticket else "Live Event",
        "venue": first_ticket.venue if first_ticket else "",
        "starts_at": first_ticket.starts_at.isoformat() if first_ticket and first_ticket.starts_at else None,
        "starts_at_formatted": format_kolkata_time(first_ticket.starts_at) if first_ticket else "TBA",
    }

    tickets_data = [
        {
            "ticket_code": t.ticket_code,
            "seat_id": t.seat_id,
            "seat_label": t.seat_label,
            "amount": str(t.amount),
            "issued_at": t.issued_at.isoformat() if t.issued_at else None,
            "used_at": t.used_at.isoformat() if t.used_at else None,
            "is_used": t.used_at is not None,
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
        tickets = issue_tickets_for_transaction(txn)
    except TicketIssuanceError as exc:
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    pdf_bytes = generate_tickets_pdf(txn, tickets)

    seat_slug = "-".join(str(t.seat_label).replace(" ", "") for t in tickets[:3])
    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-{seat_slug}-{short_ref}.pdf"

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
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
        tickets = issue_tickets_for_transaction(txn)
    except TicketIssuanceError as exc:
        return Response({"error": exc.detail, "code": "CONFLICT"}, status=exc.status_code)

    ics_bytes = generate_tickets_ics(txn, tickets)
    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-{short_ref}.ics"

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
    rate_key = f"dtc:ratelimit:verify:{ip}"
    if not check_rate_limit(rate_key, limit=60, window=60):
        return HttpResponse("Too Many Requests", status=429, content_type="text/plain")

    ticket = Ticket.objects.select_related("transaction").filter(ticket_code=ticket_code).first()
    accept_header = request.headers.get("Accept", "")

    if not ticket:
        if "application/json" in accept_header:
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

    if ticket.transaction.status != Transaction.Status.COMPLETED:
        if "application/json" in accept_header:
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

    if "application/json" in accept_header:
        return Response({
            "status": status_label,
            "event_name": ticket.event_name,
            "venue": ticket.venue,
            "starts_at": ticket.starts_at.isoformat() if ticket.starts_at else None,
            "seat_label": ticket.seat_label,
            "used_at": ticket.used_at.isoformat() if ticket.used_at else None,
        }, status=status.HTTP_200_OK)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ticket Verification: {status_label}</title>
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
    <div class="badge">{status_label}</div>
    <h1>{status_label}</h1>
    <div class="info-row">
      <div class="info-label">Event</div>
      <div class="info-val">{ticket.event_name}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Venue</div>
      <div class="info-val">{ticket.venue or "Grand Arena Stadium"}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Date & Time</div>
      <div class="info-val">{formatted_time}</div>
    </div>
    <div class="info-row">
      <div class="info-label">Seat</div>
      <div class="info-val">{ticket.seat_label}</div>
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
    # Rate limit gate check: 10 attempts per minute
    ip = _get_client_ip(request)
    rate_key = f"dtc:ratelimit:gate:{ip}"
    if not check_rate_limit(rate_key, limit=10, window=60):
        return Response({"error": "Rate limit exceeded on gate redemption.", "code": "RATE_LIMITED"}, status=status.HTTP_429_TOO_MANY_REQUESTS)


    # Verify GATE_API_KEY
    expected_key = getattr(settings, "GATE_API_KEY", os.getenv("GATE_API_KEY", "gate-secret-key-change-in-production"))
    provided_key = request.headers.get("X-Gate-Api-Key", "")

    if not provided_key or not hmac.compare_digest(str(provided_key), str(expected_key)):
        return Response({"error": "Invalid or missing gate API key.", "code": "UNAUTHORIZED"}, status=status.HTTP_401_UNAUTHORIZED)



    ticket_code = request.data.get("ticket_code")
    if not ticket_code:
        return Response({"error": "ticket_code is required.", "code": "BAD_REQUEST"}, status=status.HTTP_400_BAD_REQUEST)

    with db_transaction.atomic():
        ticket = Ticket.objects.select_for_update().select_related("transaction").filter(ticket_code=ticket_code).first()
        if not ticket:
            return Response({"error": "Ticket code not found.", "code": "NOT_FOUND"}, status=status.HTTP_404_NOT_FOUND)

        if ticket.transaction.status != Transaction.Status.COMPLETED:
            return Response({
                "error": f"Cannot redeem ticket: transaction status is {ticket.transaction.status}, expected COMPLETED.",
                "code": "TRANSACTION_NOT_COMPLETED",
            }, status=status.HTTP_409_CONFLICT)

        if ticket.used_at is not None:
            return Response({
                "error": "Ticket has already been used.",
                "code": "ALREADY_USED",
                "used_at": ticket.used_at.isoformat(),
                "used_by": ticket.used_by,
                "seat_label": ticket.seat_label,
                "event_name": ticket.event_name,
            }, status=status.HTTP_409_CONFLICT)

        ticket.used_at = timezone.now()
        ticket.used_by = "gate_staff"
        ticket.save(update_fields=["used_at", "used_by"])

        return Response({
            "status": "REDEEMED",
            "message": f"Admission granted for seat {ticket.seat_label}.",
            "ticket_code": ticket.ticket_code,
            "seat_label": ticket.seat_label,
            "event_name": ticket.event_name,
            "used_at": ticket.used_at.isoformat(),
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
    rate_key = f"dtc:ratelimit:ticket_email:{txn.transaction_id}"
    if not check_rate_limit(rate_key, limit=3, window=3600):
        return Response(
            {"error": "Email rate limit exceeded. You can only send ticket emails 3 times per hour.", "code": "RATE_LIMITED"},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # Dispatch Celery task
    from coordinator.tasks import send_ticket_email_task
    try:
        send_ticket_email_task.delay(str(txn.transaction_id))
    except Exception as exc:
        logger.warning(f"Celery dispatch failed, running inline or logging: {exc}")
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


