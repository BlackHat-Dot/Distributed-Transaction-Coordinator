import logging
import uuid
import redis
from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)

LOCK_KEY = "locks:recovery_stuck_transactions"
LOCK_TIMEOUT = 7200

RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def get_redis_client():
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(broker_url)


@shared_task(name="coordinator.tasks.recover_stuck_transactions")
def recover_stuck_transactions():

    client = get_redis_client()
    lock_token = str(uuid.uuid4())

    acquired = client.set(LOCK_KEY, lock_token, nx=True, ex=LOCK_TIMEOUT)
    if not acquired:
        logger.info(
            "Skipping recovery_stuck_transactions: previous run is still in progress "
            "(lock '%s' is held).",
            LOCK_KEY,
        )
        return "skipped"

    logger.info("Starting recovery_stuck_transactions task run.")
    try:
        call_command("recovery_stuck_transactions")
        logger.info("recovery_stuck_transactions task run completed normally.")
        return "completed"
    finally:
        try:
            client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, lock_token)
        except Exception as exc:
            logger.error("Failed to release lock '%s': %s", LOCK_KEY, exc)
