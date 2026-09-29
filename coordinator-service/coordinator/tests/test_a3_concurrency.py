import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from coordinator.models import Transaction
from coordinator.views import _run_saga_pipeline


class ConcurrencyA3Tests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()

    def test_run_saga_pipeline_skip_reserve_accepts_holding_and_inventory_reserved(self):
        """F-04: _run_saga_pipeline(skip_reserve=True) must accept both HOLDING and INVENTORY_RESERVED."""
        for initial_st in (Transaction.Status.HOLDING, Transaction.Status.INVENTORY_RESERVED):
            tid = uuid.uuid4()
            Transaction.objects.create(
                transaction_id=tid,
                status=initial_st,
                seat_ids=[1],
                quantity=1,
                amount=100,
            )
            with patch("coordinator.views._charge_payment") as mock_charge:
                mock_charge.return_value = MagicMock(status_code=200)
                resp = _run_saga_pipeline(
                    transaction_id=tid,
                    seat_ids=[1],
                    amount=Decimal("100"),
                    skip_reserve=True,
                )
                self.assertEqual(resp.status_code, 200)
                mock_charge.assert_called_once()

    def test_run_saga_pipeline_lost_payment_processing_cas_returns_409(self):
        """F-04: When INVENTORY_RESERVED -> PAYMENT_PROCESSING CAS is lost, return 409 and do not charge."""
        Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.INVENTORY_RESERVED,
            seat_ids=[1],
            quantity=1,
            amount=100,
        )

        from coordinator import views

        real_set_status = views._set_status_with_event

        def mock_set_status(tid, to_status, **kwargs):
            if to_status == Transaction.Status.PAYMENT_PROCESSING:
                return False  # Simulate concurrent loser on PAYMENT_PROCESSING gate
            return real_set_status(tid, to_status, **kwargs)

        with patch("coordinator.views._set_status_with_event", side_effect=mock_set_status), \
             patch("coordinator.views._charge_payment") as mock_charge:
            resp = _run_saga_pipeline(
                transaction_id=self.tx_id,
                seat_ids=[1],
                amount=Decimal("100"),
                skip_reserve=True,
            )
            self.assertEqual(resp.status_code, 409)
            mock_charge.assert_not_called()
