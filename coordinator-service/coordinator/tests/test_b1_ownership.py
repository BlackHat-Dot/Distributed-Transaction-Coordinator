import uuid
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from coordinator.models import Transaction
from coordinator.views import _can_access


class OwnershipB1Tests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_can_access_helper(self):
        """F-16/F-44: Test _can_access helper edge cases."""
        txn_none = Transaction(transaction_id=uuid.uuid4(), user_id=None)
        txn_empty = Transaction(transaction_id=uuid.uuid4(), user_id="")
        txn_user = Transaction(transaction_id=uuid.uuid4(), user_id="123")

        # Staff can access all
        self.assertTrue(_can_access(txn_none, "any_user", is_staff=True))
        self.assertTrue(_can_access(txn_user, "other_user", is_staff=True))

        # Non-staff denied if txn.user_id is None or empty
        self.assertFalse(_can_access(txn_none, "123", is_staff=False))
        self.assertFalse(_can_access(txn_empty, "123", is_staff=False))

        # Non-staff denied if user_id is None or empty
        self.assertFalse(_can_access(txn_user, None, is_staff=False))
        self.assertFalse(_can_access(txn_user, "", is_staff=False))

        # Non-staff denied if mismatch
        self.assertFalse(_can_access(txn_user, "456", is_staff=False))

        # String coercion: int matches str
        self.assertTrue(_can_access(txn_user, 123, is_staff=False))
        self.assertTrue(_can_access(txn_user, "123", is_staff=False))

    def test_get_transaction_denies_null_user_id_for_non_staff_f16(self):
        """F-16: GET /transactions/<id>/ must deny non-staff when txn.user_id is None."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            amount=Decimal("100"),
            user_id=None,
        )

        with patch("coordinator.views._get_caller", return_value=(True, "usr_alice", False)):
            resp = self.client.get(f"/transactions/{txn.transaction_id}/")
            self.assertEqual(resp.status_code, 403)

    @override_settings(DEBUG=True)
    def test_pay_action_ownership_not_bypassed_by_debug_f44(self):
        """F-44: In action='pay', ownership check must NOT be bypassed by DEBUG=True."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            amount=Decimal("100"),
            user_id="usr_victim",
        )

        # Attacker tries to pay for victim's hold
        with patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "usr_attacker"}):
            resp = self.client.post(
                "/transactions/",
                {"transaction_id": str(txn.transaction_id), "action": "pay"},
                format="json",
            )
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.data.get("code"), "FORBIDDEN")

    def test_pay_action_denies_null_user_id_f44(self):
        """F-44: In action='pay', non-staff cannot pay for transaction with null user_id."""
        txn = Transaction.objects.create(
            transaction_id=uuid.uuid4(),
            status=Transaction.Status.HOLDING,
            seat_ids=[1],
            amount=Decimal("100"),
            user_id=None,
        )

        with patch("coordinator.views.get_user_from_request", return_value={"is_authenticated": True, "user_id": "usr_alice"}):
            resp = self.client.post(
                "/transactions/",
                {"transaction_id": str(txn.transaction_id), "action": "pay"},
                format="json",
            )
            self.assertEqual(resp.status_code, 403)
