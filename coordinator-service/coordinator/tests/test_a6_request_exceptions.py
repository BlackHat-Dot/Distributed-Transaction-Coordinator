import uuid
from decimal import Decimal
from unittest.mock import patch
import requests

from django.test import TestCase

from coordinator.models import Transaction
from coordinator.views import _reserve, _charge, _confirm


class RequestExceptionA6Tests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()

    def test_reserve_inventory_catches_chunked_encoding_error(self):
        """F-07: _reserve must catch general RequestException (e.g. ChunkedEncodingError)."""
        Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.STARTED,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100"),
        )
        with patch("coordinator.views._http_post", side_effect=requests.exceptions.ChunkedEncodingError("Stream reset")):
            resp, err_resp = _reserve(
                transaction_id=self.tx_id,
                seat_ids=[1],
            )
            self.assertIsNone(resp)
            self.assertIsNotNone(err_resp)
            self.assertEqual(err_resp.status_code, 504)

    def test_charge_payment_catches_content_decoding_error(self):
        """F-07: _charge must catch general RequestException."""
        Transaction.objects.create(
            transaction_id=self.tx_id,
            status=Transaction.Status.PAYMENT_PROCESSING,
            seat_ids=[1],
            quantity=1,
            amount=Decimal("100"),
        )
        with patch("coordinator.views._http_post", side_effect=requests.exceptions.ContentDecodingError("Corrupt payload")):
            # payment_check also fails
            with patch("coordinator.views._http_get", side_effect=requests.exceptions.RequestException("Check failed")):
                resp = _charge(
                    transaction_id=self.tx_id,
                    amount=Decimal("100"),
                    fail_compensation=False,
                    seat_ids=[1],
                )
                self.assertEqual(resp.status_code, 503)

    def test_confirm_inventory_catches_ssl_error(self):
        """F-07: _confirm must catch general RequestException."""
        with patch("coordinator.views._http_post", side_effect=requests.exceptions.SSLError("Handshake failed")):
            resp, err = _confirm(
                transaction_id=self.tx_id,
                seat_ids=[1],
            )
            self.assertIsNone(resp)
            self.assertIsInstance(err, requests.exceptions.RequestException)
