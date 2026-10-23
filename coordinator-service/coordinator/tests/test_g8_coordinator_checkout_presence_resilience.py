import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock

import redis
from django.test import TestCase

from coordinator.models import Transaction
from coordinator.views import _set_checkout_presence, _clear_checkout_presence, _charge


class TestG8CoordinatorCheckoutPresenceResilience(TestCase):
    def test_set_checkout_presence_fails_open_with_warning_when_redis_down(self):
        """
        G8: _set_checkout_presence fails open with a logged warning (not an exception) if Redis is down.
        """
        tx_id = uuid.uuid4()
        broken_client = MagicMock()
        broken_client.set.side_effect = redis.ConnectionError("Redis connection refused")

        with patch("coordinator.views._get_coordinator_redis_client", return_value=broken_client):
            with self.assertLogs("coordinator.views", level="WARNING") as log_cm:
                # Must NOT raise exception
                _set_checkout_presence(tx_id)
                self.assertTrue(any("Failed to set checkout presence key" in msg for msg in log_cm.output))

    def test_clear_checkout_presence_fails_open_with_warning_when_redis_down(self):
        """
        G8: _clear_checkout_presence fails open with a logged warning if Redis is down.
        """
        tx_id = uuid.uuid4()
        broken_client = MagicMock()
        broken_client.delete.side_effect = redis.ConnectionError("Redis connection refused")

        with patch("coordinator.views._get_coordinator_redis_client", return_value=broken_client):
            with self.assertLogs("coordinator.views", level="WARNING") as log_cm:
                # Must NOT raise exception
                _clear_checkout_presence(tx_id)
                self.assertTrue(any("Failed to clear checkout presence key" in msg for msg in log_cm.output))

    def test_charge_clears_checkout_presence_on_unhandled_exception(self):
        """
        G8: When _charge encounters an unhandled exception, _clear_checkout_presence is called.
        """
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            status=Transaction.Status.STARTED,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )

        mock_redis = MagicMock()

        with patch("coordinator.views._get_coordinator_redis_client", return_value=mock_redis):
            with patch("coordinator.views._http_post", side_effect=RuntimeError("Unexpected crash")):
                with self.assertRaises(RuntimeError):
                    _charge(
                        transaction_id=tx_id,
                        amount=Decimal("100.00"),
                    )

            # Assert clear was called during exception unwinding
            self.assertTrue(mock_redis.delete.called)
