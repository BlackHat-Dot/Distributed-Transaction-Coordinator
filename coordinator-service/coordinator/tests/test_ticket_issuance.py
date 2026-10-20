import concurrent.futures
import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.test import TransactionTestCase

from coordinator.models import Transaction, Ticket
from coordinator.tickets import issue_tickets_for_transaction, TicketIssuanceError


class TicketIssuanceTests(TransactionTestCase):
    def setUp(self):
        self.user_id = "usr_alice"
        self.user_email = "alice@example.com"

    def _create_transaction(self, status=Transaction.Status.COMPLETED, seat_ids=None, seat_numbers=None, amount=Decimal("500.00")):
        if seat_ids is None:
            seat_ids = [1]
        if seat_numbers is None:
            seat_numbers = "A1"
        return Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            user_id=self.user_id,
            user_email=self.user_email,
            status=status,
            seat_ids=seat_ids,
            seat_numbers=seat_numbers,
            amount=amount,
            quantity=len(seat_ids),
        )

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_completed_and_ground_truth_booked(self, mock_get):
        """Issuing ticket for COMPLETED transaction + inventory ground truth BOOKED returns tickets."""
        txn = self._create_transaction(status=Transaction.Status.COMPLETED, seat_ids=[101], seat_numbers="B5", amount=Decimal("750.00"))
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {
                "name": "Coldplay: Music of the Spheres World Tour",
                "venue": "Grand Arena Stadium",
                "starts_at": "2026-10-15T19:00:00Z",
            },
            "seats": [
                {"seat_id": 101, "seat_label": "B5", "amount": "750.00"}
            ]
        }
        mock_get.return_value = mock_resp

        tickets = issue_tickets_for_transaction(txn)
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]

        self.assertEqual(ticket.transaction, txn)
        self.assertEqual(ticket.seat_id, 101)
        self.assertEqual(ticket.seat_label, "B5")
        self.assertEqual(ticket.event_name, "Coldplay: Music of the Spheres World Tour")
        self.assertEqual(ticket.venue, "Grand Arena Stadium")
        self.assertEqual(ticket.amount, Decimal("750.00"))
        self.assertIsNotNone(ticket.ticket_code)
        self.assertEqual(len(ticket.ticket_code), 22)  # secrets.token_urlsafe(16) -> 22 chars
        self.assertIsNone(ticket.used_at)

        self.assertIsNone(ticket.used_by)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 1)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_failed_transaction_returns_409(self, mock_get):
        """Transaction status FAILED/PAYMENT_FAILED returns 409 and creates no Ticket row."""
        txn = self._create_transaction(status=Transaction.Status.PAYMENT_FAILED)

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(Ticket.objects.count(), 0)
        mock_get.assert_not_called()

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_compensated_transaction_returns_409(self, mock_get):
        """Transaction status COMPENSATED returns 409 and creates no Ticket row."""
        txn = self._create_transaction(status=Transaction.Status.COMPENSATED)

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(Ticket.objects.count(), 0)
        mock_get.assert_not_called()

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_in_flight_transaction_returns_409(self, mock_get):
        """In-flight transactions (STARTED, HOLDING, PAYMENT_PROCESSING) return 409."""
        for in_flight_status in [Transaction.Status.STARTED, Transaction.Status.HOLDING, Transaction.Status.PAYMENT_PROCESSING]:
            txn = self._create_transaction(status=in_flight_status)
            with self.assertRaises(TicketIssuanceError) as ctx:
                issue_tickets_for_transaction(txn)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)
        mock_get.assert_not_called()

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_completed_but_inventory_released_returns_409(self, mock_get):
        """Transaction COMPLETED but inventory ground truth returns RELEASED -> 409 and no Ticket."""
        txn = self._create_transaction(status=Transaction.Status.COMPLETED)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "RELEASED",
            "transaction_id": str(txn.transaction_id),
            "released_reason": "TIMEOUT"
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(Ticket.objects.count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_seat_booked_by_different_transaction_returns_409(self, mock_get):
        """Inventory ground truth reports seat is booked by different transaction -> 409 and no Ticket."""
        txn = self._create_transaction(status=Transaction.Status.COMPLETED)
        different_tid = str(uuid.uuid4())

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": different_tid,  # mismatch!
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(Ticket.objects.count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_multi_seat_booking(self, mock_get):
        """A 3-seat booking issues 3 distinct tickets, each with a unique ticket_code and seat."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[1, 2, 3],
            seat_numbers="A1, A2, A3",
            amount=Decimal("1500.00")
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {
                "name": "Coldplay: Music of the Spheres World Tour",
                "venue": "Grand Arena Stadium",
                "starts_at": "2026-10-15T19:00:00Z",
            },
            "seats": [
                {"seat_id": 1, "seat_label": "A1", "amount": "500.00"},
                {"seat_id": 2, "seat_label": "A2", "amount": "500.00"},
                {"seat_id": 3, "seat_label": "A3", "amount": "500.00"},
            ]
        }
        mock_get.return_value = mock_resp

        tickets = issue_tickets_for_transaction(txn)
        self.assertEqual(len(tickets), 3)

        codes = {t.ticket_code for t in tickets}
        self.assertEqual(len(codes), 3, "All ticket codes must be unique")

        seats = {t.seat_id for t in tickets}
        self.assertEqual(seats, {1, 2, 3})
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 3)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_idempotency_concurrent_requests(self, mock_get):
        """10 concurrent requests to issue tickets for the same transaction yield exactly 1 Ticket per seat."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[10, 11],
            seat_numbers="B1, B2",
            amount=Decimal("1000.00")
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {
                "name": "Coldplay: Music of the Spheres World Tour",
                "venue": "Grand Arena Stadium",
                "starts_at": "2026-10-15T19:00:00Z",
            },
            "seats": [
                {"seat_id": 10, "seat_label": "B1", "amount": "500.00"},
                {"seat_id": 11, "seat_label": "B2", "amount": "500.00"},
            ]
        }
        mock_get.return_value = mock_resp

        def _worker():
            try:
                # Refresh transaction from DB in each thread to avoid stale in-memory state
                t = Transaction.objects.get(pk=txn.pk)
                return issue_tickets_for_transaction(t)
            finally:
                from django.db import connections
                connections.close_all()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_worker) for _ in range(10)]
            results = [f.result() for f in futures]

        # Verify DB has exactly 2 tickets (1 for seat 10, 1 for seat 11)
        db_tickets = list(Ticket.objects.filter(transaction=txn).order_by("seat_id"))
        self.assertEqual(len(db_tickets), 2)
        self.assertEqual({t.seat_id for t in db_tickets}, {10, 11})

        # All results should have the identical ticket codes
        expected_codes = {t.ticket_code for t in db_tickets}
        for res in results:
            self.assertEqual({t.ticket_code for t in res}, expected_codes)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_missing_event_fails_fast_without_fake_data(self, mock_get):
        """When event metadata is missing from transaction and ground truth, fail fast with 409 error instead of using fake defaults."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[201],
            seat_numbers="C1",
            amount=Decimal("350.00"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": None,  # Missing event metadata!
            "seats": [{"seat_id": 201, "seat_label": "C1", "amount": "350.00"}],
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Event metadata is missing", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_missing_seat_label_fails_fast(self, mock_get):
        """When seat label is missing from inventory response, fail fast with 409 error instead of using fake 'Seat <id>'."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[301],
            seat_numbers=None,
            amount=Decimal("400.00"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {"name": "Test Concert", "venue": "Arena"},
            "seats": [{"seat_id": 301, "seat_label": None, "amount": "400.00"}],
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Seat label", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_empty_inventory_seats_fails_fast(self, mock_get):
        """When inventory ground truth returns empty seats list, fail fast with 409 before database work."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[303],
            amount=Decimal("200.00"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {"name": "Test Concert", "venue": "Arena"},
            "seats": [],  # Empty seats list!
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Inventory returned no confirmed seat records", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_seat_id_not_found_in_inventory_fails_fast(self, mock_get):
        """When requested seat_id is missing from inventory seats list, fail fast with 409."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[304, 305],
            amount=Decimal("500.00"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {"name": "Test Concert", "venue": "Arena"},
            "seats": [
                {"seat_id": 304, "seat_label": "E1", "amount": "250.00"}
                # Seat 305 is missing from inventory!
            ],
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Seat ID 305 was not found in inventory confirmation", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)

    @patch("coordinator.tickets.requests.get")
    def test_issue_ticket_missing_pricing_fails_fast(self, mock_get):
        """When seat price is missing from inventory response, fail fast with 409 error instead of using total/n split."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[302],
            seat_numbers="D1",
            amount=Decimal("400.00"),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "BOOKED",
            "transaction_id": str(txn.transaction_id),
            "event": {"name": "Test Concert", "venue": "Arena"},
            "seats": [{"seat_id": 302, "seat_label": "D1"}],  # No amount or price!
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Pricing details could not be resolved", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)

    def test_issue_ticket_empty_seat_ids_fails_fast(self):
        """When transaction has no seat_ids, fail fast with 409 instead of defaulting to [1]."""
        txn = self._create_transaction(
            status=Transaction.Status.COMPLETED,
            seat_ids=[],
            seat_numbers=None,
            amount=Decimal("0.00"),
        )

        with self.assertRaises(TicketIssuanceError) as ctx:
            issue_tickets_for_transaction(txn)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("no associated seat IDs", ctx.exception.detail)
        self.assertEqual(Ticket.objects.filter(transaction=txn).count(), 0)
