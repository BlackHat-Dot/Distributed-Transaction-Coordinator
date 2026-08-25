

from unittest.mock import MagicMock, patch
from django.conf import settings
from django.test import TestCase
from coordinator_service.celery import app as celery_app
from coordinator.tasks import (
    LOCK_KEY,
    LOCK_TIMEOUT,
    recover_stuck_transactions,
)


class MockRedisClient:
    """Hermetic in-memory Redis client simulating distributed lock operations and Lua scripts."""

    def __init__(self):
        self._store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True

    def get(self, key):
        return self._store.get(key)

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    def eval(self, script, numkeys, key, arg):
        if self._store.get(key) == arg:
            self._store.pop(key, None)
            return 1
        return 0


class CeleryTaskLockingTests(TestCase):
    def setUp(self):
        self.mock_redis = MockRedisClient()
        self._redis_patcher = patch(
            "coordinator.tasks.get_redis_client", return_value=self.mock_redis
        )
        self._redis_patcher.start()

    def tearDown(self):
        self._redis_patcher.stop()

    def test_task_runs_command_and_releases_lock(self):
        with patch("coordinator.tasks.call_command") as mock_call_command:
            with self.assertLogs("coordinator.tasks", level="INFO") as cm:
                result = recover_stuck_transactions()

            self.assertEqual(result, "completed")
            mock_call_command.assert_called_once_with("recovery_stuck_transactions")

            log_output = "\n".join(cm.output)
            self.assertIn("Starting recovery_stuck_transactions task run", log_output)
            self.assertIn("recovery_stuck_transactions task run completed normally", log_output)

            self.assertIsNone(self.mock_redis.get(LOCK_KEY))

    def test_task_skips_when_lock_already_held(self):
        existing_token = "existing-worker-lock-token"
        self.mock_redis.set(LOCK_KEY, existing_token, ex=LOCK_TIMEOUT)

        with patch("coordinator.tasks.call_command") as mock_call_command:
            with self.assertLogs("coordinator.tasks", level="INFO") as cm:
                result = recover_stuck_transactions()

            self.assertEqual(result, "skipped")
            mock_call_command.assert_not_called()

            log_output = "\n".join(cm.output)
            self.assertIn("Skipping recovery_stuck_transactions", log_output)
            self.assertIn("previous run is still in progress", log_output)
            self.assertIn(LOCK_KEY, log_output)

            stored_token = self.mock_redis.get(LOCK_KEY)
            self.assertEqual(
                stored_token.decode("utf-8") if isinstance(stored_token, bytes) else stored_token,
                existing_token,
            )

    def test_lock_released_in_finally_on_exception(self):
        with patch("coordinator.tasks.call_command", side_effect=RuntimeError("Command failed")):
            with self.assertRaises(RuntimeError):
                recover_stuck_transactions()

            self.assertIsNone(self.mock_redis.get(LOCK_KEY))

    def test_lock_ttl_and_parameters(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("coordinator.tasks.get_redis_client", return_value=mock_redis), \
             patch("coordinator.tasks.call_command"):
            recover_stuck_transactions()

            mock_redis.set.assert_called_once()
            args, kwargs = mock_redis.set.call_args
            self.assertEqual(args[0], LOCK_KEY)
            self.assertTrue(kwargs.get("nx"))
            self.assertEqual(kwargs.get("ex"), 7200)

    def test_celery_beat_schedule_configuration(self):
        beat_schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
        self.assertIn("recover_stuck_transactions_every_90_seconds", beat_schedule)
        config = beat_schedule["recover_stuck_transactions_every_90_seconds"]
        self.assertEqual(config["task"], "coordinator.tasks.recover_stuck_transactions")
        self.assertEqual(config["schedule"], 90.0)

    def test_celery_app_configuration(self):
        self.assertEqual(celery_app.main, "coordinator")
        self.assertTrue(settings.CELERY_BROKER_URL.startswith("redis://"))
