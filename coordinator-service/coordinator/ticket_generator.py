import base64
import io
import os
from datetime import timezone as dt_timezone, timedelta
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
import qrcode
from PIL import Image
from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
import icalendar
import textwrap
import zoneinfo

from coordinator.models import Transaction, Ticket


def get_public_base_url() -> str:
    """
    Resolves the base URL for public QR verification links.
    In production (DEBUG=False, IS_TESTING=False), requires explicit PUBLIC_BASE_URL.
    """
    url = getattr(settings, "PUBLIC_BASE_URL", None) or os.getenv("PUBLIC_BASE_URL", "")
    if not url:
        if getattr(settings, "DEBUG", False) or getattr(settings, "IS_TESTING", False):
            url = "http://localhost:8002"
        else:
            from django.core.exceptions import ImproperlyConfigured
            raise ImproperlyConfigured(
                "CRITICAL CONFIGURATION ERROR: PUBLIC_BASE_URL environment variable must be set in production when DEBUG=False! "
                "It must be configured with a public URL reachable from client devices (e.g. mobile phones for QR scanning)."
            )
    return str(url).strip().rstrip("/")


def generate_qr_code_image(ticket_code: str) -> Image.Image:
    """
    Generates a high-contrast, scannable QR code image encoding the public verify URL.
    Uses Error Correction Level M, box size >= 6, and quiet zone border >= 4.
    """
    base_url = get_public_base_url()
    safe_code = str(ticket_code or "").strip()
    verify_url = f"{base_url}/verify/{safe_code}/"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=4,
    )
    qr.add_data(verify_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.get_image() if hasattr(img, "get_image") else img


def generate_qr_code_base64(ticket_code: str) -> str:
    """
    Returns data:image/png;base64,... string for inline frontend display.
    """
    img = generate_qr_code_image(ticket_code)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def format_kolkata_time(dt) -> str:
    """
    Formats a datetime (or ISO string) into Asia/Kolkata timezone representation.
    Safely handles string timestamps, naive datetimes, and None.
    """
    if not dt:
        return "Date & Time TBA"
    try:
        if isinstance(dt, str):
            parsed = parse_datetime(dt)
            if parsed is not None:
                dt = parsed
            else:
                return str(dt)
        kolkata_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        if timezone.is_aware(dt):
            local_dt = dt.astimezone(kolkata_tz)
        else:
            local_dt = dt.replace(tzinfo=kolkata_tz)
        return local_dt.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        if hasattr(dt, "strftime"):
            return dt.strftime("%d %b %Y, %H:%M UTC")
        return str(dt)


def generate_tickets_pdf(transaction: Transaction, tickets: list[Ticket]) -> bytes:
    """
    Generates a clean, professional portrait A5 PDF with one page per seat ticket.
    Pure Python via ReportLab and QRCode.
    """
    if not tickets:
        raise ValueError("Cannot generate tickets PDF: tickets list is empty.")

    buf = io.BytesIO()
    # A5: 419.5 x 595.2 points
    c = canvas.Canvas(buf, pagesize=A5)
    page_w, page_h = A5

    tid = getattr(transaction, "transaction_id", "UNKNOWN")
    short_ref = str(tid)[:8].upper()

    for ticket in tickets:
        # Header banner
        c.setFillColor(colors.HexColor("#0f172a"))  # Slate 900
        c.rect(0, page_h - 70, page_w, 70, fill=1, stroke=0)

        c.setFillColor(colors.HexColor("#38bdf8"))  # Sky 400
        c.setFont("Helvetica-Bold", 10)
        c.drawString(25, page_h - 26, "DISTRIBUTED TRANSACTION COORDINATOR")

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(25, page_h - 48, "OFFICIAL E-TICKET")

        c.setFillColor(colors.HexColor("#94a3b8"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 25, page_h - 48, f"REF: #{short_ref}")

        # Event Title: wrap across multiple lines without truncating
        raw_event_name = getattr(ticket, "event_name", None) or ""
        title_lines = textwrap.wrap(raw_event_name, width=36)
        if not title_lines:
            title_lines = [raw_event_name]
        font_size = 12 if len(title_lines) > 3 else (14 if len(title_lines) > 2 else 15)
        line_height = font_size + 3
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica-Bold", font_size)

        cur_y = page_h - 92
        for line in title_lines:
            c.drawString(25, cur_y, line)
            cur_y -= line_height

        # Venue
        c.setFillColor(colors.HexColor("#475569"))
        c.setFont("Helvetica", 10.5)
        venue_name = getattr(ticket, "venue", "") or "Main Arena"
        venue_text = f"Venue: {venue_name}"
        c.drawString(25, cur_y - 2, venue_text[:65])
        cur_y -= 18

        # Date & Time in Asia/Kolkata
        time_text = f"Date & Time: {format_kolkata_time(getattr(ticket, 'starts_at', None))}"
        c.drawString(25, cur_y - 2, time_text)
        cur_y -= 20

        # Seat & Price Box
        box_top = min(cur_y, page_h - 165)
        box_h = 58
        box_bottom = box_top - box_h

        c.setFillColor(colors.HexColor("#f8fafc"))  # Slate 50
        c.setStrokeColor(colors.HexColor("#cbd5e1"))  # Slate 300
        c.rect(25, box_bottom, page_w - 50, box_h, fill=1, stroke=1)

        c.setFillColor(colors.HexColor("#64748b"))
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(40, box_top - 18, "ASSIGNED SEAT")
        c.drawString(220, box_top - 18, "PRICE / ADMISSION")

        c.setFillColor(colors.HexColor("#0f172a"))
        seat_text = str(getattr(ticket, "seat_label", "") or "")
        seat_font_size = 18
        if len(seat_text) > 16:
            seat_font_size = 11
        elif len(seat_text) > 12:
            seat_font_size = 13
        elif len(seat_text) > 8:
            seat_font_size = 15
        c.setFont("Helvetica-Bold", seat_font_size)
        c.drawString(40, box_top - 44, seat_text[:28])

        c.setFont("Helvetica-Bold", 16)
        raw_amount = getattr(ticket, "amount", None)
        amount_val = raw_amount if raw_amount is not None else Decimal("0.00")
        try:
            amount_val = Decimal(str(amount_val))
        except Exception:
            amount_val = Decimal("0.00")
        c.drawString(220, box_top - 44, f"${amount_val:.2f}")

        # Attendee & Order details
        c.setFillColor(colors.HexColor("#334155"))
        c.setFont("Helvetica", 8.5)
        holder_email = getattr(transaction, "user_email", "") or "Ticket Holder"
        holder_display = holder_email if len(holder_email) <= 40 else holder_email[:37] + "..."
        c.drawString(25, box_bottom - 16, f"Holder: {holder_display}")

        ticket_code = str(getattr(ticket, "ticket_code", "") or "")
        code_display = ticket_code[:16] if ticket_code else "N/A"
        c.drawRightString(page_w - 25, box_bottom - 16, f"Ticket Code: {code_display}")

        # Divider line
        c.setStrokeColor(colors.HexColor("#e2e8f0"))
        c.setLineWidth(1)
        c.setDash(4, 3)
        divider_y = box_bottom - 26
        c.line(25, divider_y, page_w - 25, divider_y)
        c.setDash()  # reset dash

        # QR Code Section
        qr_img = generate_qr_code_image(ticket_code)
        qr_io = io.BytesIO()
        qr_img.save(qr_io, format="PNG")
        qr_io.seek(0)
        qr_reader = ImageReader(qr_io)

        qr_size = 165
        if (divider_y - qr_size - 12) < 55:
            qr_size = max(130, int(divider_y - 70))
        qr_x = (page_w - qr_size) / 2
        qr_y = divider_y - qr_size - 12
        c.drawImage(qr_reader, qr_x, qr_y, width=qr_size, height=qr_size)

        # Scan at gate instruction
        c.setFillColor(colors.HexColor("#0f172a"))
        c.setFont("Helvetica-Bold", 11.5)
        c.drawCentredString(page_w / 2, qr_y - 16, "Scan at gate")

        c.setFillColor(colors.HexColor("#64748b"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(
            page_w / 2,
            qr_y - 28,
            "Present this barcode on mobile or printed for one-time admission."
        )

        # Footer
        c.setFillColor(colors.HexColor("#94a3b8"))
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(
            page_w / 2,
            16,
            "Powered by DTC • Single-use electronic pass • Unique cryptographic serial"
        )

        c.showPage()

    c.save()
    return buf.getvalue()


def generate_tickets_ics(transaction: Transaction, tickets: list[Ticket]) -> bytes:
    """
    Generates a standard iCalendar (.ics) event for the booking with UTC DTSTART.
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//DTC Ticket Service//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    first_ticket = tickets[0] if tickets else None
    event_name = getattr(first_ticket, "event_name", "") if first_ticket else ""
    venue = getattr(first_ticket, "venue", "") if first_ticket else ""
    starts_at = getattr(first_ticket, "starts_at", None) if first_ticket else None

    if starts_at:
        if isinstance(starts_at, str):
            parsed = parse_datetime(starts_at)
            starts_at = parsed or starts_at
        if hasattr(starts_at, "tzinfo"):
            if timezone.is_naive(starts_at):
                starts_at = timezone.make_aware(starts_at, timezone.get_current_timezone())
            dtstart_utc = starts_at.astimezone(dt_timezone.utc)
        else:
            dtstart_utc = timezone.now().astimezone(dt_timezone.utc)
    else:
        dtstart_utc = timezone.now().astimezone(dt_timezone.utc)

    dtend_utc = dtstart_utc + timedelta(hours=3)

    seat_labels = ", ".join(t.seat_label for t in tickets) if tickets else "General"
    tid = getattr(transaction, "transaction_id", "UNKNOWN")
    short_ref = str(tid)[:8].upper()
    user_email = getattr(transaction, "user_email", "") or "Customer"

    event = icalendar.Event()
    event.add("summary", event_name or "Event Ticket")
    event.add("location", venue or "Venue TBA")
    event.add(
        "description",
        f"DTC Ticket Booking Confirmation.\nSeats: {seat_labels}\nReference: #{short_ref}\nEmail: {user_email}"
    )
    event.add("dtstart", dtstart_utc)
    event.add("dtend", dtend_utc)
    event.add("dtstamp", timezone.now().astimezone(dt_timezone.utc))
    event.add("uid", f"dtc-txn-{tid}@dtc.internal")
    event.add("status", "CONFIRMED")

    cal.add_component(event)
    return cal.to_ical()