import logging
import uuid
import redis
from celery import shared_task
from django.conf import settings
from django.core.management import call_command

logger = logging.getLogger(__name__)

LOCK_KEY = "locks:recovery_stuck_transactions"
LOCK_TIMEOUT = 7200

# lua script
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


@shared_task(name="coordinator.tasks.execute_saga_task")
def execute_saga_task(transaction_id, seat_id=None, amount=500, fail_compensation=False, correlation_id=None):
    """
    Task-Driven Saga Execution:
    Executes the 3-phase saga pipeline asynchronously in a background Celery worker thread.
    """
    from coordinator.views import _run_saga_pipeline
    logger.info("Executing async saga task for transaction_id=%s [correlation_id=%s]", transaction_id, correlation_id)
    try:
        parsed_tid = uuid.UUID(str(transaction_id))
    except ValueError:
        logger.error("Invalid transaction_id UUID passed to execute_saga_task: %s", transaction_id)
        return "invalid_uuid"

    response = _run_saga_pipeline(
        transaction_id=parsed_tid,
        seat_id=seat_id,
        amount=amount,
        fail_compensation=fail_compensation,
        correlation_id=correlation_id,
        actor="request",
    )
    logger.info("Async saga task completed for transaction_id=%s with status=%s", transaction_id, getattr(response, 'status_code', 'unknown'))
    return "completed"
