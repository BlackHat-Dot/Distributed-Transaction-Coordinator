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
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 1.0)
    return redis.Redis.from_url(
        broker_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


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
        try:
            import time
            client.set("dtc:recovery:last_run_timestamp", str(time.time()))
        except Exception:
            pass
        logger.info("recovery_stuck_transactions task run completed normally.")
        return "completed"
    finally:
        try:
            client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, lock_token)
        except Exception as exc:
            logger.error("Failed to release lock '%s': %s", LOCK_KEY, exc)


@shared_task(name="coordinator.tasks.execute_saga_task")
def execute_saga_task(transaction_id, seat_ids=None, amount=500, correlation_id=None, user_id=None, user_email=None, skip_reserve=False, **kwargs):
    from coordinator.views import _run_saga

    logger.info("Executing async saga task for transaction_id=%s [correlation_id=%s, user_id=%s, user_email=%s, skip_reserve=%s]", transaction_id, correlation_id, user_id, user_email, skip_reserve)

    try:
        parsed_tid = uuid.UUID(str(transaction_id))
    except ValueError:
        logger.error("Invalid transaction_id UUID passed to execute_saga_task: %s", transaction_id)
        return "invalid_uuid"

    response = _run_saga(
        transaction_id=parsed_tid,
        seat_ids=seat_ids,
        amount=amount,
        correlation_id=correlation_id,
        actor="request",
        user_id=user_id,
        user_email=user_email,
        skip_reserve=skip_reserve,
        **kwargs,
    )

    logger.info("Async saga task completed for transaction_id=%s with status=%s", transaction_id, getattr(response, 'status_code', 'unknown'))

    return "completed"


@shared_task(name="coordinator.tasks.send_ticket_email_task", bind=True, max_retries=3, default_retry_delay=10)
def send_ticket_email_task(self, transaction_id):
    from coordinator.models import Transaction
    from coordinator.ticket_generator import generate_tickets_pdf
    from coordinator.tickets import issue_tickets_for_transaction
    import resend

    try:
        tid = uuid.UUID(str(transaction_id))
    except ValueError:
        logger.error("Invalid transaction_id for ticket email task: %s", transaction_id)
        return "invalid_uuid"

    txn = Transaction.objects.filter(transaction_id=tid).first()
    if not txn or not txn.user_email:
        logger.warning("Transaction %s not found or has no email.", tid)
        return "no_email"

    tickets = list(txn.tickets.all().order_by("seat_id"))
    if not tickets:
        try:
            tickets = issue_tickets_for_transaction(txn)
        except Exception as exc:
            logger.error("Failed to issue tickets for email dispatch on %s: %exc", tid)
            raise self.retry(exc=exc)

    pdf_bytes = generate_tickets_pdf(txn, tickets)
    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-Ticket-{short_ref}.pdf"

    api_key = getattr(settings, "RESEND_API_KEY", "")
    if not api_key:
        logger.info("RESEND_API_KEY not configured. Simulated ticket email to %s with PDF attachment %s.", txn.user_email, filename)
        return "simulated"

    resend.api_key = api_key
    try:
        seat_labels = ", ".join(t.seat_label for t in tickets)
        event_name = tickets[0].event_name if tickets else ""
        params = {
            "from": "DTC Tickets <tickets@resend.dev>",
            "to": [txn.user_email],
            "subject": f"Your Electronic Tickets: {event_name} (#{short_ref})",
            "html": f"""<p>Dear Customer,</p>
<p>Thank you for booking with DTC. Attached is your official electronic ticket for <strong>{event_name}</strong>.</p>
<p><strong>Seats:</strong> {seat_labels}<br>
<strong>Booking Ref:</strong> #{short_ref}</p>
<p>Please present the QR code on your ticket at the venue gate for admission.</p>
<p>Best regards,<br>The DTC Team</p>""",
            "attachments": [
                {
                    "filename": filename,
                    "content": list(pdf_bytes),
                }
            ],
        }
        res = resend.Emails.send(params)
        logger.info("Ticket email sent successfully to %s: %s", txn.user_email, res)
        return "sent"
    except Exception as exc:
        logger.error("Error dispatching ticket email to %s: %s", txn.user_email, exc)
        raise self.retry(exc=exc)
