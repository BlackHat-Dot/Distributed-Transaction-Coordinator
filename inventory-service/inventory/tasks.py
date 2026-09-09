import logging
from celery import shared_task
from django.core.management import call_command

logger = logging.getLogger(__name__)


@shared_task(name="inventory.tasks.sweep_expired_holds_task")
def sweep_expired_holds_task():
    logger.info("Starting inventory sweep_expired_holds_task run.")
    try:
        call_command("sweep_expired_holds")
        logger.info("inventory sweep_expired_holds_task run completed.")
        return "completed"
    except Exception as exc:
        logger.error("inventory sweep_expired_holds_task failed: %s", exc)
        return "failed"
