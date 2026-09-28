import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch
from django.db import DatabaseError
from django.test import TestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.views import _create_transaction


class ReserveOrderA7Tests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_hold_creates_transaction_before_reserving(self):
        """F-11: action='hold' must create Transaction row in DB BEFORE calling _reserve_inventory."""
        row_existed_during_reserve = [False]

        def mock_reserve(tid, seat_ids, **kwargs):
            # Inspect database at the exact moment _reserve_inventory is invoked
            txn = Transaction.objects.filter(transaction_id=tid).first()
            if txn is not None and txn.status == Transaction.Status.STARTED:
                row_existed_during_reserve[0] = True
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b'{"held_until": "2026-09-02T07:21:32Z", "total_amount": "500.00"}'
            mock_resp.json.return_value = {"held_until": "2026-09-02T07:21:32Z", "total_amount": "500.00"}
            return mock_resp, None

        with patch("coordinator.views._resolve_seat_pricing", return_value=(Decimal("500"), [1], ["A1"], None)), \
             patch("coordinator.views._reserve_inventory", side_effect=mock_reserve):
            resp = self.client.post(
                "/transactions/",
                {"seat_ids": [1], "action": "hold"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(
            row_existed_during_reserve[0],
            "Transaction record did not exist in DB before _reserve_inventory was called!",
        )

    def test_hold_reserve_timeout_calls_inventory_release(self):
        """F-11: On reserve error/timeout during hold, coordinator must release inventory to prevent seat leak."""
        mock_release_resp = MagicMock()
        mock_release_resp.status_code = 200
        mock_release_resp.content = b'{}'
        mock_release_resp.json.return_value = {}

        def mock_reserve(tid, seat_ids, **kwargs):
            from rest_framework.response import Response
            return None, Response({"error": "Inventory timeout"}, status=504)

        with patch("coordinator.views._resolve_seat_pricing", return_value=(Decimal("500"), [1], ["A1"], None)), \
             patch("coordinator.views._reserve_inventory", side_effect=mock_reserve), \
             patch("coordinator.views._http_post", return_value=mock_release_resp) as mock_http_post:
            resp = self.client.post(
                "/transactions/",
                {"seat_ids": [1], "action": "hold"},
                format="json",
            )
            self.assertEqual(resp.status_code, 504)
            # Must have attempted to call release endpoint
            release_called = any("release" in call.args[0] for call in mock_http_post.call_args_list)
            self.assertTrue(release_called, "Inventory release was not called on reserve error!")

    def test_create_transaction_handles_non_integrity_error(self):
        """F-11: _create_transaction must catch non-IntegrityError DB errors and return 500 response."""
        with patch("coordinator.models.Transaction.objects.create", side_effect=DatabaseError("Connection lost")):
            created, resp = _create_transaction(uuid.uuid4())
            self.assertFalse(created)
            self.assertIsNotNone(resp)
            self.assertEqual(resp.status_code, 500)
