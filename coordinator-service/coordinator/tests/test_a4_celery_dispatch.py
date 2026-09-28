import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent


class CeleryDispatchA4Tests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.tx_id = uuid.uuid4()

    def test_pay_dispatch_failure_reverts_to_holding_and_returns_503(self):
        """F-12: When Celery dispatch fails, CAS revert status back to HOLDING and return 503."""
        txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100"),
        )

        with patch("coordinator.tasks.execute_saga_task.delay", side_effect=Exception("RabbitMQ connection failure")):
            resp = self.client.post(
                "/transactions/",
                {"transaction_id": str(self.tx_id), "action": "pay"},
                format="json",
            )
            self.assertEqual(resp.status_code, 503)

        txn.refresh_from_db()
        self.assertEqual(
            txn.status,
            Transaction.Status.HOLDING,
            "Transaction status was not reverted to HOLDING upon dispatch failure!",
        )

        # Check SagaEvent audit trail includes the reversion
        events = list(SagaEvent.objects.filter(transaction=txn).order_by("seq"))
        self.assertTrue(len(events) >= 2)
        self.assertEqual(events[-1].to_status, Transaction.Status.HOLDING)
        self.assertEqual(events[-1].from_status, Transaction.Status.INVENTORY_RESERVED)
