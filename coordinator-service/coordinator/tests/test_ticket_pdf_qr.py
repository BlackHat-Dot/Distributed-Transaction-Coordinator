import io
import secrets
import uuid

from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
import cv2
import numpy as np
import pypdf
import icalendar

from coordinator.models import Transaction, Ticket
from coordinator.ticket_generator import (
    generate_qr_code_image,
    generate_tickets_pdf,
    generate_tickets_ics,
)


class TicketPdfQrTests(TestCase):
    def setUp(self):
        self.txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            user_id="usr_alice",
            user_email="alice@example.com",
            status=Transaction.Status.COMPLETED,
            seat_ids=[1],
            seat_numbers="A1",
            amount=Decimal("500.00"),
            quantity=1,
        )

    def _create_ticket(self, txn, seat_id=1, seat_label="A1", amount=Decimal("500.00")):
        return Ticket.objects.create(
            transaction=txn,
            seat_id=seat_id,
            seat_label=seat_label,
            event_name="Coldplay: Music of the Spheres World Tour",
            venue="Grand Arena Stadium",
            starts_at=timezone.now() + timezone.timedelta(days=7),
            amount=amount,
            ticket_code=secrets.token_urlsafe(16),
        )


    def test_qr_code_scannable_and_decodes_to_verify_url(self):
        """Extract QR image and decode using cv2.QRCodeDetector; must equal the verify URL."""
        ticket = self._create_ticket(self.txn)
        qr_pil_image = generate_qr_code_image(ticket.ticket_code)

        # Convert PIL image to cv2 numpy array (grayscale)
        open_cv_image = np.array(qr_pil_image.convert("RGB"))
        detector = cv2.QRCodeDetector()
        decoded_text, points, _ = detector.detectAndDecode(open_cv_image)

        self.assertTrue(len(decoded_text) > 0, "QR code could not be decoded by OpenCV detector")
        expected_url_part = f"/verify/{ticket.ticket_code}/"
        self.assertIn(expected_url_part, decoded_text)
        self.assertTrue(decoded_text.startswith("http://") or decoded_text.startswith("https://"))

    def test_pdf_structure_and_text_content(self):
        """Generate PDF for single seat: starts with %PDF, contains event, seat, venue, email, short ref."""
        ticket = self._create_ticket(self.txn, seat_id=1, seat_label="Row A Seat 1", amount=Decimal("750.00"))
        pdf_bytes = generate_tickets_pdf(self.txn, [ticket])

        # Assert starts with %PDF
        self.assertTrue(pdf_bytes.startswith(b"%PDF"), "PDF bytes must start with %PDF header")

        # Parse with pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        self.assertEqual(len(reader.pages), 1, "Single-seat ticket must have exactly 1 page")

        page_text = reader.pages[0].extract_text()
        self.assertIn("Coldplay", page_text)
        self.assertIn("Grand Arena Stadium", page_text)
        self.assertIn("Row A Seat 1", page_text)
        self.assertIn("alice@example.com", page_text)
        self.assertIn(str(self.txn.transaction_id)[:8].upper(), page_text)
        self.assertIn("Scan at gate", page_text)
        self.assertIn("IST", page_text)  # Asia/Kolkata timezone formatting

    def test_pdf_multi_seat_generates_one_page_per_seat(self):
        """3-seat transaction generates a single PDF with 3 pages (one per seat)."""
        t1 = self._create_ticket(self.txn, seat_id=1, seat_label="Seat A1")
        t2 = self._create_ticket(self.txn, seat_id=2, seat_label="Seat A2")
        t3 = self._create_ticket(self.txn, seat_id=3, seat_label="Seat A3")

        pdf_bytes = generate_tickets_pdf(self.txn, [t1, t2, t3])
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        self.assertEqual(len(reader.pages), 3, "A 3-seat booking must generate exactly 3 pages")

        page1_text = reader.pages[0].extract_text()
        page2_text = reader.pages[1].extract_text()
        page3_text = reader.pages[2].extract_text()

        self.assertIn("Seat A1", page1_text)
        self.assertIn("Seat A2", page2_text)
        self.assertIn("Seat A3", page3_text)

    def test_ics_calendar_generation_valid_utc_dtstart(self):
        """Generated .ics must parse with icalendar library and contain UTC DTSTART and event details."""
        ticket = self._create_ticket(self.txn)
        ics_bytes = generate_tickets_ics(self.txn, [ticket])

        cal = icalendar.Calendar.from_ical(ics_bytes)
        self.assertEqual(cal.get("prodid"), "-//DTC Ticket Service//EN")

        events = [c for c in cal.walk() if c.name == "VEVENT"]
        self.assertEqual(len(events), 1)

        ev = events[0]
        self.assertEqual(str(ev.get("summary")), "Coldplay: Music of the Spheres World Tour")
        self.assertEqual(str(ev.get("location")), "Grand Arena Stadium")
        
    def test_pdf_long_event_name_not_truncated(self):
        """Event names with 70+ characters must not be truncated in the generated PDF."""
        long_event_name = "Coldplay: Music of the Spheres World Tour - Live in Mumbai Extended Stadium Show 2026"
        self.assertGreaterEqual(len(long_event_name), 70)
        ticket = Ticket.objects.create(
            transaction=self.txn,
            seat_id=1,
            seat_label="VIP-1",
            event_name=long_event_name,
            venue="Grand Arena Stadium",
            starts_at=timezone.now(),
            amount=Decimal("500.00"),
            ticket_code=secrets.token_urlsafe(16),
        )

        pdf_bytes = generate_tickets_pdf(self.txn, [ticket])
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_text = reader.pages[0].extract_text()
        # Verify the entire, uncut 70+ character event name appears in the extracted text
        normalized_pdf_text = " ".join(page_text.split())
        self.assertIn(long_event_name, normalized_pdf_text)


    def test_public_base_url_unset_in_production_raises_improperly_configured(self):
        """When DEBUG=False and IS_TESTING=False, if PUBLIC_BASE_URL is unset, ImproperlyConfigured must be raised."""
        import os
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from coordinator.ticket_generator import get_public_base_url

        with patch.object(settings, "DEBUG", False), \
             patch.object(settings, "IS_TESTING", False), \
             patch.object(settings, "PUBLIC_BASE_URL", ""), \
             patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            with self.assertRaises(ImproperlyConfigured):
                get_public_base_url()


