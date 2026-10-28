# verified

import logging
import uuid
from celery import shared_task
from django.core.management import call_command

from inventory.services import get_redis_client

logger = logging.getLogger(__name__)

LOCK_KEY = "locks:sweep_expired_holds"
LOCK_TIMEOUT = 60

# Lua script for atomic compare-and-delete lock release (Redlock standard)
RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@shared_task(name="inventory.tasks.sweep_expired_holds_task")
def sweep_expired_holds_task():
    """
    Periodic Celery Beat task (every 30s): Reclaims expired and presence-abandoned
    seat holds. Employs a distributed Redis mutex lock to prevent concurrent worker
    stampedes and database lock contention on Seat.objects.select_for_update().
    """
    from inventory.middleware import set_correlation_id
    set_correlation_id("bgworker")

    client = get_redis_client()
    lock_token = str(uuid.uuid4())
    acquired = False

    if client:
        try:
            acquired = client.set(LOCK_KEY, lock_token, nx=True, ex=LOCK_TIMEOUT)
            if not acquired:
                logger.info(
                    "Skipping inventory sweep_expired_holds_task: previous sweep is still in progress "
                    "(lock '%s' is held).",
                    LOCK_KEY,
                )
                return "skipped"
        except Exception as exc:
            logger.warning("Could not acquire sweep lock due to Redis error: %s", exc)

    logger.info("Starting inventory sweep_expired_holds_task run.")
    try:
        call_command("sweep_expired_holds")
        logger.info("inventory sweep_expired_holds_task run completed.")
        return "completed"
    except Exception as exc:
        logger.error("inventory sweep_expired_holds_task failed: %s", exc, exc_info=True)
        return "failed"
    finally:
        if client and acquired:
            try:
                client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, lock_token)
            except Exception as exc:
                logger.error("Failed to release lock '%s': %s", LOCK_KEY, exc)