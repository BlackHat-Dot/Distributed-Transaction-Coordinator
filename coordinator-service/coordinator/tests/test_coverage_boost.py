import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from coordinator.models import Transaction, SagaEvent
from coordinator.recovery import execute_compensation_sweep
from coordinator.tasks import execute_saga_task, recover_stuck_transactions
from coordinator.views import (
    _can_access,
    _compensate,
    _finish_payment,
    _http_get,
    _http_post,
    _event_json,
    _txn_json,
)


class TasksCoverageTests(TestCase):
    def test_execute_saga_task_invalid_uuid(self):
        result = execute_saga_task("invalid-uuid-string")
        self.assertEqual(result, "invalid_uuid")

    @patch("coordinator.tasks.redis.Redis.from_url")
    @patch("coordinator.tasks.call_command")
    def test_recovery_stuck_transactions_redis_exceptions(self, mock_cmd, mock_redis_from_url):
        mock_client = MagicMock()
        mock_client.set.side_effect = [True, Exception("redis set failed")]
        mock_client.eval.side_effect = Exception("redis eval failed")
        mock_redis_from_url.return_value = mock_client

        res = recover_stuck_transactions()
        self.assertEqual(res, "completed")


class RecoveryCoverageTests(TestCase):
    def setUp(self):
        self.tx_id = uuid.uuid4()
        self.txn = Transaction.objects.create(
            transaction_id=self.tx_id,
            user_id="user_rec_cov",
            status=Transaction.Status.RECOVERY_IN_PROGRESS,
            amount=Decimal("150.00"),
            seat_ids=[1, 2],
        )

    def test_sweep_payment_check_500_failure(self):
        mock_get = MagicMock(status_code=500, content=b"Server error")
        mock_post = MagicMock(status_code=200, content=b"{}")
        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            original_status=Transaction.Status.PAYMENT_PROCESSING,
            get_func=lambda *a, **kw: mock_get,
            post_func=lambda *a, **kw: mock_post,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["status_code"], 503)

    def test_sweep_payment_refunded_already(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "REFUNDED"}',
            json=lambda: {"status": "REFUNDED"},
        )
        mock_post = MagicMock(
            status_code=200,
            content=b'{"message": "Inventory released"}',
            json=lambda: {"message": "Inventory released"},
        )
        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=lambda *a, **kw: mock_post,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], Transaction.Status.COMPENSATED)

    def test_sweep_void_request_exception(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "NOT_CHARGED"}',
            json=lambda: {"status": "NOT_CHARGED"},
        )

        def mock_post(url, **kw):
            if "/void/" in url:
                raise Exception("Network error on void")
            return MagicMock(status_code=200, content=b"{}", json=lambda: {})

        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=mock_post,
        )
        self.assertFalse(res["success"])

    def test_sweep_refund_request_exception(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "CHARGED"}',
            json=lambda: {"status": "CHARGED"},
        )

        def mock_post(url, **kw):
            if "/confirm/" in url:
                return MagicMock(status_code=409, content=b'{"error": "Hold expired"}', json=lambda: {"error": "Hold expired"})
            if "/refund/" in url:
                raise Exception("Network error on refund")
            return MagicMock(status_code=200, content=b"{}", json=lambda: {})

        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=mock_post,
        )
        self.assertFalse(res["success"])

    def test_sweep_unknown_payment_status(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "STRANGE_UNKNOWN_STATE"}',
            json=lambda: {"status": "STRANGE_UNKNOWN_STATE"},
        )
        mock_post = MagicMock(status_code=200, content=b"{}", json=lambda: {})
        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=lambda *a, **kw: mock_post,
        )
        self.assertFalse(res["success"])

    def test_sweep_release_request_exception(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "NOT_CHARGED", "voided": true}',
            json=lambda: {"status": "NOT_CHARGED", "voided": True},
        )

        def mock_post(url, **kw):
            if "/release/" in url:
                raise Exception("Release network error")
            return MagicMock(status_code=200, content=b"{}", json=lambda: {})

        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=mock_post,
        )
        self.assertFalse(res["success"])

    def test_sweep_release_400_already_released(self):
        mock_get = MagicMock(
            status_code=200,
            content=b'{"status": "NOT_CHARGED", "voided": true}',
            json=lambda: {"status": "NOT_CHARGED", "voided": True},
        )
        mock_post = MagicMock(
            status_code=400,
            content=b'{"code": "ALREADY_RELEASED", "message": "Nothing to release"}',
            json=lambda: {"code": "ALREADY_RELEASED", "message": "Nothing to release"},
        )
        res = execute_compensation_sweep(
            transaction_id=self.tx_id,
            actor=SagaEvent.ACTOR_RECOVERY,
            get_func=lambda *a, **kw: mock_get,
            post_func=lambda *a, **kw: mock_post,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], Transaction.Status.COMPENSATED)


class ViewsCoverageTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_can_access_edge_cases(self):
        txn = Transaction(user_id="123")
        self.assertTrue(_can_access(txn, "123"))
        self.assertTrue(_can_access(txn, 123))
        self.assertTrue(_can_access(txn, "456", is_staff=True))
        self.assertFalse(_can_access(txn, None))
        self.assertFalse(_can_access(txn, ""))
        txn_no_user = Transaction(user_id=None)
        self.assertFalse(_can_access(txn_no_user, "123"))

    def test_serialize_helpers(self):
        tx_id = uuid.uuid4()
        txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_cov",
            user_email="cov@test.io",
            amount=Decimal("250.00"),
            status=Transaction.Status.COMPLETED,
            seat_ids=[10, 11],
        )
        event = SagaEvent.objects.create(
            transaction=txn,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.COMPLETED,
            actor="test",
            http_payload={"key": "val"},
        )
        d_txn = _txn_json(txn)
        self.assertEqual(d_txn["transaction_id"], str(tx_id))
        self.assertEqual(str(d_txn["amount"]), "250.00")

        d_evt = _event_json(event)
        self.assertEqual(d_evt["seq"], 1)

    def test_pay_action_seat_ids_validation(self):
        tx_id = str(uuid.uuid4())
        # Not a list of ints
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"action": "pay", "transaction_id": tx_id, "seat_ids": ["abc"]}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_SEAT_IDS")

        # Duplicate seat_ids
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"action": "pay", "transaction_id": tx_id, "seat_ids": [1, 1]}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "DUPLICATE_SEATS")

        # Exceeded max seats
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"action": "pay", "transaction_id": tx_id, "seat_ids": list(range(1, 15))}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "EXCEEDED_MAX_SEATS")

    def test_list_transactions_filters_and_pagination(self):
        for i in range(5):
            Transaction.objects.create(
                transaction_id=uuid.uuid4(),
                user_id="usr_list_test",
                status=Transaction.Status.COMPLETED if i % 2 == 0 else Transaction.Status.STARTED,
                amount=Decimal("100.00"),
            )

        # Filter by status as staff
        resp = self.client.get(
            "/transactions/?status=COMPLETED&limit=2&offset=0",
            HTTP_X_STAFF_KEY="staff-key-list",
            HTTP_X_USER_ID="admin",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.data)

        # Filter as normal user
        resp = self.client.get(
            "/transactions/?limit=10&offset=0",
            HTTP_X_USER_ID="usr_list_test",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 5)

    def test_finalize_payment_outcome_409_codes(self):
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_cov_409",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("100.00"),
        )
        with patch("coordinator.views._compensate") as mock_comp:
            # IDEMPOTENCY_PAYLOAD_MISMATCH
            _finish_payment(
                transaction_id=tx_id,
                fail_compensation=False,
                payment_data={"code": "IDEMPOTENCY_PAYLOAD_MISMATCH", "error": "Mismatch", "status": "FAILED"},
                payment_status_code=409,
            )
            mock_comp.assert_called()

            # TRANSACTION_VOIDED
            _finish_payment(
                transaction_id=tx_id,
                fail_compensation=False,
                payment_data={"code": "TRANSACTION_VOIDED", "error": "Voided", "status": "FAILED"},
                payment_status_code=409,
            )

    @patch("coordinator.views.redis.Redis.from_url")
    def test_transaction_stream_sse_flow(self, mock_redis):
        from django.db import connection
        connection.ensure_connection()
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_stream",
            status=Transaction.Status.STARTED,
            amount=Decimal("100.00"),
        )

        mock_pubsub = MagicMock()
        msg_progress = {"type": "message", "data": json.dumps({"status": "HOLDING", "is_terminal": False})}
        msg_term = {"type": "message", "data": json.dumps({"status": "COMPLETED", "is_terminal": True})}
        mock_pubsub.get_message.side_effect = [msg_progress, None, msg_term]

        mock_client = MagicMock()
        mock_client.pubsub.return_value = mock_pubsub
        mock_redis.return_value = mock_client

        resp = self.client.get(
            f"/transactions/{tx_id}/stream/",
            HTTP_X_USER_ID="usr_stream",
        )
        self.assertEqual(resp.status_code, 200)
        lines = list(resp.streaming_content)
        self.assertTrue(len(lines) >= 3)
        connection.ensure_connection()

    @patch("coordinator.views.redis.Redis.from_url")
    def test_transaction_stream_sse_exception(self, mock_redis):
        from django.db import connection
        connection.ensure_connection()
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_stream_err",
            status=Transaction.Status.STARTED,
            amount=Decimal("100.00"),
        )
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.side_effect = Exception("Redis connection died")
        mock_client = MagicMock()
        mock_client.pubsub.return_value = mock_pubsub
        mock_redis.return_value = mock_client

        resp = self.client.get(
            f"/transactions/{tx_id}/stream/",
            HTTP_X_USER_ID="usr_stream_err",
        )
        self.assertEqual(resp.status_code, 200)
        lines = list(resp.streaming_content)
        self.assertTrue(any("STREAM_ERROR" in str(line) for line in lines))
        connection.ensure_connection()

    def test_resolve_seat_pricing_error_branches(self):
        from coordinator.views import _get_prices
        # RequestException branch
        with patch("coordinator.views._http_post", side_effect=Exception("Timeout")):
            amt, seats, names, err_resp = _get_prices([1, 2])
            self.assertIsNotNone(err_resp)
            self.assertEqual(err_resp.status_code, 503)

        # Mismatch length branch (404)
        mock_resp = MagicMock(status_code=200, json=lambda: [{"id": 1, "price": 100}])
        with patch("coordinator.views._http_post", return_value=mock_resp):
            amt, seats, names, err_resp = _get_prices([1, 2])
            self.assertIsNotNone(err_resp)
            self.assertEqual(err_resp.status_code, 404)

        # Empty catalog branch (503)
        mock_resp = MagicMock(status_code=200, json=lambda: [])
        with patch("coordinator.views._http_post", return_value=mock_resp):
            amt, seats, names, err_resp = _get_prices([1])
            self.assertIsNotNone(err_resp)
            self.assertEqual(err_resp.status_code, 503)

    @patch("coordinator.views.settings.IS_TESTING", False)
    def test_http_get_and_post_session_paths(self):
        with patch("coordinator.views._get_http_session") as mock_get_sess:
            mock_sess = MagicMock()
            mock_sess.post.return_value = MagicMock(status_code=200)
            mock_sess.get.return_value = MagicMock(status_code=200)
            mock_get_sess.return_value = mock_sess

            resp_post = _http_post("http://test.internal/post")
            self.assertEqual(resp_post.status_code, 200)

            resp_get = _http_get("http://test.internal/get")
            self.assertEqual(resp_get.status_code, 200)

    def test_request_payload_verify_invalid_fields(self):
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"seat_ids": [1], "unknown_field_attack": "hacked"}),
            content_type="application/json",
            HTTP_X_USER_ID="user_hacker",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_FIELDS")

    def test_run_saga_pipeline_simulate_decline(self):
        from coordinator.views import _run_saga
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_decline",
            status=Transaction.Status.STARTED,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )
        with patch("coordinator.views._http_post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {"status": "SUCCESS"})
            resp = _run_saga(
                transaction_id=tx_id,
                seat_ids=[1],
                amount=Decimal("100.00"),
                fail_compensation=False,
                simulate_decline=True,
                is_sync=True,
            )
            self.assertIn(resp.status_code, [400, 422])
            txn = Transaction.objects.get(transaction_id=tx_id)
            self.assertEqual(txn.status, Transaction.Status.COMPENSATED)

    def test_get_transaction_and_events_error_paths(self):
        unknown_id = uuid.uuid4()
        # Unauthenticated get
        with patch("coordinator.views._get_caller", return_value=(False, None, False)):
            resp = self.client.get(f"/transactions/{unknown_id}/")
            self.assertEqual(resp.status_code, 401)

        # 404 get
        resp = self.client.get(f"/transactions/{unknown_id}/", HTTP_X_USER_ID="usr_1")
        self.assertEqual(resp.status_code, 404)

        # 403 get
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="owner_user",
            status=Transaction.Status.STARTED,
            amount=Decimal("100.00"),
        )
        resp = self.client.get(
            f"/transactions/{tx_id}/",
            HTTP_X_USER_ID="stranger_user",
            HTTP_X_USER_EMAIL="stranger@user.com",
        )
        self.assertEqual(resp.status_code, 403)

        # Unauthenticated events
        with patch("coordinator.views._get_caller", return_value=(False, None, False)):
            resp = self.client.get(f"/transactions/{unknown_id}/events/")
            self.assertEqual(resp.status_code, 401)

        # 404 events
        resp = self.client.get(f"/transactions/{unknown_id}/events/", HTTP_X_USER_ID="usr_1")
        self.assertEqual(resp.status_code, 404)

        # 403 events
        resp = self.client.get(
            f"/transactions/{tx_id}/events/",
            HTTP_X_USER_ID="stranger_user",
            HTTP_X_USER_EMAIL="stranger@user.com",
        )
        self.assertEqual(resp.status_code, 403)

    def test_transaction_stream_options_and_errors(self):
        tx_id = uuid.uuid4()
        # OPTIONS
        resp = self.client.options(f"/transactions/{tx_id}/stream/")
        self.assertEqual(resp.status_code, 204)

        # 401
        with patch("coordinator.views._get_caller", return_value=(False, None, False)):
            resp = self.client.get(f"/transactions/{tx_id}/stream/")
            self.assertEqual(resp.status_code, 401)

        # 404
        resp = self.client.get(f"/transactions/{tx_id}/stream/", HTTP_X_USER_ID="usr_1")
        self.assertEqual(resp.status_code, 404)

    def test_list_transactions_bad_params_and_unauthenticated(self):
        # 401 unauthenticated
        with patch("coordinator.views._get_caller", return_value=(False, None, False)):
            resp = self.client.get("/transactions/")
            self.assertEqual(resp.status_code, 401)

        # Bad params
        resp = self.client.get(
            "/transactions/?limit=invalid&offset=invalid&page=2",
            HTTP_X_USER_ID="usr_1",
        )
        self.assertEqual(resp.status_code, 200)

        # Bad page
        resp = self.client.get(
            "/transactions/?page=invalid",
            HTTP_X_USER_ID="usr_1",
        )
        self.assertEqual(resp.status_code, 200)

    def test_action_pay_completed_idempotent_and_failure_branches(self):
        from requests.exceptions import RequestException
        from datetime import timedelta

        # 1. Idempotent return if already completed
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_pay_idem",
            status=Transaction.Status.COMPLETED,
            amount=Decimal("100.00"),
        )
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"action": "pay", "transaction_id": str(tx_id)}),
            content_type="application/json",
            HTTP_X_USER_ID="usr_pay_idem",
        )
        self.assertEqual(resp.status_code, 200)

        # 2. Expired hold with inventory release RequestException
        tx_id_exp = uuid.uuid4()
        txn_exp = Transaction.objects.create(
            transaction_id=tx_id_exp,
            user_id="usr_pay_exp",
            status=Transaction.Status.HOLDING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )
        SagaEvent.objects.create(
            transaction=txn_exp,
            seq=1,
            from_status=Transaction.Status.STARTED,
            to_status=Transaction.Status.HOLDING,
            actor=SagaEvent.ACTOR_REQUEST,
            http_payload={"held_until": (timezone.now() - timedelta(minutes=10)).isoformat()},
        )
        with patch("coordinator.views._http_post", side_effect=RequestException("Rel timeout")):
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"action": "pay", "transaction_id": str(tx_id_exp)}),
                content_type="application/json",
                HTTP_X_USER_ID="usr_pay_exp",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "HOLD_EXPIRED")

        # 3. Transition fails concurrent
        tx_id_race = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id_race,
            user_id="usr_pay_race",
            status=Transaction.Status.HOLDING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )
        with patch("coordinator.views._set_status_with_event", return_value=False):
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"action": "pay", "transaction_id": str(tx_id_race)}),
                content_type="application/json",
                HTTP_X_USER_ID="usr_pay_race",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.data.get("code"), "CONCURRENT_PAYMENT_IN_PROGRESS")

    @patch("coordinator.views.redis.Redis.from_url")
    @patch("django.core.management.call_command")
    def test_trigger_recovery_sweep_redis_paths(self, mock_cmd, mock_redis):
        mock_client = MagicMock()
        mock_client.set.return_value = True
        mock_redis.return_value = mock_client

        resp = self.client.post(
            "/recovery/sweep/",
            data=json.dumps({"threshold_seconds": 60}),
            content_type="application/json",
            HTTP_X_STAFF_KEY="staff-key-rec",
            HTTP_X_USER_ID="admin",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("threshold_seconds_applied"), 60)

    @patch("coordinator.views.redis.Redis.from_url")
    def test_transaction_stream_periodic_db_check(self, mock_redis):
        from django.db import connection
        connection.ensure_connection()
        tx_id = uuid.uuid4()
        _txn = Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_stream_db",
            status=Transaction.Status.COMPLETED,
            amount=Decimal("100.00"),
        )
        mock_pubsub = MagicMock()
        mock_pubsub.get_message.return_value = None
        mock_client = MagicMock()
        mock_client.pubsub.return_value = mock_pubsub
        mock_redis.return_value = mock_client

        # Mock time.time to simulate passage of 4 seconds between start and first loop
        with patch("coordinator.views.time.time", side_effect=[100.0, 100.0, 104.5, 105.0]):
            resp = self.client.get(
                f"/transactions/{tx_id}/stream/",
                HTTP_X_USER_ID="usr_stream_db",
            )
            self.assertEqual(resp.status_code, 200)
            lines = list(resp.streaming_content)
            self.assertTrue(any("COMPLETED" in str(line) for line in lines))
        connection.ensure_connection()

    def test_action_hold_updates_authoritative_price(self):
        pricing_resp = MagicMock(status_code=200, json=lambda: [{"id": 1, "price": 100, "row": "A", "seat_number": "1"}])
        reserve_resp = MagicMock(status_code=200, json=lambda: {"total_amount": "999.00", "seat_ids": [1]})

        def mock_post_dispatch(url, **kw):
            if "/seats/pricing/" in url:
                return pricing_resp
            return reserve_resp

        with patch("coordinator.views._http_post", side_effect=mock_post_dispatch):
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"action": "hold", "seat_ids": [1]}),
                content_type="application/json",
                HTTP_X_USER_ID="usr_hold_price",
            )
            self.assertIn(resp.status_code, [200, 201])
            tx_id = resp.data.get("transaction_id")
            txn = Transaction.objects.get(transaction_id=tx_id)
            self.assertEqual(txn.amount, Decimal("999.00"))

    def test_compensate_already_released_and_refund_transport_error(self):
        from requests.exceptions import RequestException

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_comp_rel",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )

        def mock_post_side_effect(url, **kw):
            if "/seats/release/" in url:
                return MagicMock(status_code=400, content=b'{"code": "ALREADY_RELEASED"}', json=lambda: {"code": "ALREADY_RELEASED"})
            if "/payments/refund/" in url:
                raise RequestException("Refund network timeout")
            return MagicMock(status_code=200, content=b"{}", json=lambda: {})

        with patch("coordinator.views._http_post", side_effect=mock_post_side_effect):
            resp = _compensate(
                transaction_id=tx_id,
                fail_compensation=False,
                payment_data={"status": "CHARGED"},
                payment_status_code=200,
            )
            self.assertEqual(resp.status_code, 503)

    def test_charge_payment_timeout_check_recovers(self):
        from requests.exceptions import RequestException
        from coordinator.views import _charge

        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_charge_chk",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )

        def mock_post_side_effect(url, **kw):
            if "/payments/charge/" in url:
                raise RequestException("Charge read timeout")
            if "/seats/confirm/" in url:
                return MagicMock(status_code=200, content=b'{"message": "Confirmed"}', json=lambda: {"message": "Confirmed"})
            return MagicMock(status_code=200, content=b"{}", json=lambda: {})

        mock_get_resp = MagicMock(status_code=200, content=b'{"status": "CHARGED"}', json=lambda: {"status": "CHARGED"})

        with patch("coordinator.views._http_post", side_effect=mock_post_side_effect), \
             patch("coordinator.views._http_get", return_value=mock_get_resp):
            resp = _charge(
                transaction_id=tx_id,
                amount=Decimal("100.00"),
                fail_compensation=False,
                seat_ids=[1],
            )
            self.assertEqual(resp.status_code, 200)

    def test_retry_compensation_unclaimed_concurrent_race(self):
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_retry_race",
            status=Transaction.Status.COMPENSATION_FAILED,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )
        with patch("coordinator.views._set_status_with_event", return_value=False):
            resp = self.client.post(
                f"/transactions/{tx_id}/retry-compensation/",
                HTTP_X_STAFF_KEY="staff-key-retry",
                HTTP_X_USER_ID="admin",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertIn("concurrently resolved", resp.data.get("message"))

    @patch("coordinator.tasks.get_redis_client")
    def test_recovery_status_with_cached_timestamp(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get.return_value = b"1700000000.0"
        mock_get_client.return_value = mock_client

        resp = self.client.get(
            "/recovery/status/",
            HTTP_X_STAFF_KEY="staff-key-stat",
            HTTP_X_USER_ID="admin",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get("last_run_timestamp"), 1700000000.0)

    def test_payload_verify_additional_error_cases(self):
        # 1. Non-dict JSON body
        resp = self.client.post(
            "/transactions/",
            data=json.dumps([1, 2, 3]),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)

        # 2. Invalid action
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"action": "invalid_action_name"}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_ACTION")

        # 3. Empty seat_ids on default/hold
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"seat_ids": []}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "SEATS_REQUIRED")

        # 4. Non-int seat_ids on default/hold
        resp = self.client.post(
            "/transactions/",
            data=json.dumps({"seat_ids": ["one", "two"]}),
            content_type="application/json",
            HTTP_X_USER_ID="u1",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data.get("code"), "INVALID_SEAT_IDS")

    def test_action_hold_pricing_error(self):
        from rest_framework.response import Response
        err_mock = Response({"error": "Pricing failed"}, status=503)
        with patch("coordinator.views._get_prices", return_value=(None, None, None, err_mock)):
            resp = self.client.post(
                "/transactions/",
                data=json.dumps({"action": "hold", "seat_ids": [1]}),
                content_type="application/json",
                HTTP_X_USER_ID="usr_hold_err",
            )
            self.assertEqual(resp.status_code, 503)

    def test_compensate_release_server_error(self):
        tx_id = uuid.uuid4()
        Transaction.objects.create(
            transaction_id=tx_id,
            user_id="usr_comp_500",
            status=Transaction.Status.PAYMENT_PROCESSING,
            amount=Decimal("100.00"),
            seat_ids=[1],
        )
        mock_resp = MagicMock(status_code=500, content=b'{"error": "Server error"}', json=lambda: {"error": "Server error"})
        def fake_post(url, *a, **kw):
            if "void" in url:
                return MagicMock(status_code=200, content=b'{"status": "VOIDED"}', json=lambda: {"status": "VOIDED"})
            return mock_resp

        with patch("coordinator.views._http_post", side_effect=fake_post):
            resp = _compensate(
                transaction_id=tx_id,
                fail_compensation=False,
                payment_data={"status": "NOT_CHARGED"},
                payment_status_code=400,
            )
            self.assertEqual(resp.status_code, 500)

