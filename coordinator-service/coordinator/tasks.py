# verified

# Imports Python standard logging library to log background execution, warnings, and error diagnostics.
import logging
# Imports standard OS module to inspect runtime environment variables and process settings.
import os
# Imports standard time library to record task execution timestamps and track background run intervals.
import time
# Imports standard UUID library to generate unique distributed lock tokens and validate transaction IDs.
import uuid
# Imports Redis client library to coordinate distributed mutex locks and atomic Lua scripts across worker nodes.
import redis
# Imports Celery's shared_task decorator to define reusable asynchronous background tasks without hardcoding the Celery app instance.
from celery import shared_task
# Imports Django runtime settings module to access configured broker connection URLs and socket timeouts.
from django.conf import settings
# Imports Django management command runner to trigger programmatic recovery sweeper command execution.
from django.core.management import call_command

# Configures module-level logger instance tagged with current module name for task output tracing.
logger = logging.getLogger(__name__)

# Redis mutex key and 2-hour timeout used to prevent concurrent recovery sweeper worker runs across distributed server nodes.
LOCK_KEY = "locks:recovery_stuck_transactions"
LOCK_TIMEOUT = 7200

# Redlock standard Lua script executed atomically in Redis: only deletes the lock key if its stored value matches the caller's unique lock token.
RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


# Constructs a Redis client configured with strict connection timeouts and Celery broker URL from settings.
def get_redis_client():
    """Return a Redis client instance configured from Django Celery settings."""
    # Resolves Celery message broker Redis connection URL from Django settings with local fallback.
    broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    # Detects whether test suite execution is active to apply aggressive connection timeout policies.
    testing_mode = bool(getattr(settings, "IS_TESTING", False))
    # Configures strict Redis socket connect and read timeouts (50ms in testing, 1.0s in production).
    timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 0.05 if testing_mode else 1.0)
    # Instantiates and returns a Redis client pool configured with the specified broker URL and timeouts.
    return redis.Redis.from_url(
        broker_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


# Privacy utility that masks email usernames (e.g., u***r@example.com) to comply with data privacy regulations in application logs.
def _mask_email(email):
    """Mask email for privacy compliance (e.g. user@example.com -> u***r@example.com)."""
    # Validates email string presence and @ delimiter, returning empty string for missing or invalid inputs.
    if not email or "@" not in str(email):
        return ""
    # Splits email into username and domain portions to mask sensitive local identifiers.
    try:
        name, domain = str(email).strip().split("@", 1)
        # Masks short usernames (<=2 chars) with single trailing asterisk, or longer usernames with asterisks between first and last chars.
        if len(name) <= 2:
            masked_name = name[0] + "*"
        else:
            masked_name = name[0] + "***" + name[-1]
        # Reassembles masked local username with pristine domain name for privacy-compliant logging.
        return f"{masked_name}@{domain}"
    # Safety fallback masking entire string if unexpected string formatting or parsing errors arise.
    except Exception:
        return "***"


# Periodic background worker scheduled by Celery Beat to find and heal orphaned or stuck saga transactions.
@shared_task(name="coordinator.tasks.recover_stuck_transactions")
def recover_stuck_transactions():
    """
    Periodic Celery Beat task: Acquires a distributed Redis mutex lock and triggers
    the recovery_stuck_transactions management command to sweep and heal stuck sagas.
    """
    # Lazily imports and tags current execution context with bgworker label for distributed tracing.
    from coordinator.middleware import set_correlation_id
    set_correlation_id("bgworker")

    # Obtains Redis connection pool client to execute atomic lock acquisition and release scripts.
    client = get_redis_client()
    # Generates a unique UUID lock token so this worker process can safely verify ownership when releasing the Redis mutex.
    lock_token = str(uuid.uuid4())

    # Initializes distributed lock acquisition state flag prior to executing atomic SET NX command.
    acquired = False
    try:
        # SET ... NX EX attempts atomic lock acquisition; returns True only if the key does not already exist.
        acquired = client.set(LOCK_KEY, lock_token, nx=True, ex=LOCK_TIMEOUT)
    # Intercepts Redis socket drops and connection timeouts, gracefully skipping run to avoid hung worker threads.
    except Exception as exc:
        logger.warning("Could not acquire recovery lock due to Redis connection error: %s", exc)
        return "skipped"

    # Guard clause: skips task execution if another sweeper instance is already running or holding mutex lock.
    # If the lock is held by another worker instance or a previous run is still active, safely skip this scheduled cycle.
    if not acquired:
        logger.info(
            "Skipping recovery_stuck_transactions: previous run is still in progress "
            "(lock '%s' is held).",
            LOCK_KEY,
        )
        return "skipped"

    # Logs sweeper task kickoff message indicating distributed lock was successfully secured.
    logger.info("Starting recovery_stuck_transactions task run.")
    try:
        # Invokes the Django management command to scan database records and trigger compensation on expired or stuck transactions.
        call_command("recovery_stuck_transactions")
        # Updates last successful execution timestamp in Redis for observability and cluster heartbeat monitoring.
        try:
            client.set("dtc:recovery:last_run_timestamp", str(time.time()))
        except Exception:
            pass
        # Logs normal completion message and returns task completion status to Celery broker.
        logger.info("recovery_stuck_transactions task run completed normally.")
        return "completed"
    finally:
        # Encloses atomic lock release in try block to guarantee worker continuity even if Redis times out during cleanup.
        try:
            # Atomically releases the distributed mutex lock only if the key still holds this worker's specific lock_token.
            client.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, lock_token)
        # Captures lock release exceptions to prevent worker unhandled crashes while warning operator via logs.
        except Exception as exc:
            logger.error("Failed to release lock '%s': %s", LOCK_KEY, exc)


# Asynchronous Celery task that executes the complete booking saga pipeline off the web request path.
@shared_task(name="coordinator.tasks.execute_saga_task")
def execute_saga_task(transaction_id, seat_ids=None, amount=500, user_id=None, user_email=None, skip_reserve=False, **kwargs):
    """
    Asynchronous Celery worker task that executes the booking saga state machine.
    """
    # Imports saga orchestration coordinator lazily to prevent circular imports between tasks and views.
    from coordinator.views import _run_saga
    # Imports correlation ID context setter to propagate request-level tracing into the background Celery task.
    from coordinator.middleware import set_correlation_id

    # Extracts or defaults the correlation identifier to propagate distributed tracing across worker threads.
    cid = kwargs.get("correlation_id") or str(transaction_id)
    set_correlation_id(str(cid))
    # Logs asynchronous saga dispatch parameters with masked user email for privacy compliance.
    logger.info(
        "Executing async saga task for transaction_id=%s [user_id=%s, email=%s, skip_reserve=%s]",
        transaction_id,
        user_id,
        _mask_email(user_email),
        skip_reserve,
    )

    # Parses transaction identifier strictly as a UUID to ensure downstream query safety.
    try:
        parsed_tid = uuid.UUID(str(transaction_id))
    # Traps malformed UUIDs, logging error and returning invalid_uuid status code.
    except (ValueError, TypeError, AttributeError):
        logger.error("Invalid transaction_id UUID passed to execute_saga_task: %s", transaction_id)
        return "invalid_uuid"

    # Extracts initiating actor label (defaulting to request) for audit event logging across saga state transitions.
    actor = kwargs.pop("actor", "request")

    # Executes the multi-step saga state machine (seat reservation, payment charge, and seat confirmation).
    response = _run_saga(
        transaction_id=parsed_tid,
        seat_ids=seat_ids,
        amount=amount,
        actor=actor,
        user_id=user_id,
        user_email=user_email,
        skip_reserve=skip_reserve,
        **kwargs,
    )

    # Logs saga execution outcome status code and returns completed task string.
    logger.info(
        "Async saga task completed for transaction_id=%s with status=%s",
        transaction_id,
        getattr(response, "status_code", "unknown"),
    )

    # Signals successful asynchronous execution to the Celery worker runtime.
    return "completed"


# Asynchronous Celery task with exponential backoff retries that renders an electronic ticket PDF and emails it to the buyer.
@shared_task(name="coordinator.tasks.send_ticket_email_task", bind=True, max_retries=3, default_retry_delay=10)
def send_ticket_email_task(self, transaction_id):
    """
    Generates an official electronic ticket PDF with QR codes and emails it
    to the booking owner via Resend API with automated retry handling.
    """
    # Lazy-loads context tracker and attaches transaction ID to thread-local correlation context.
    from coordinator.middleware import set_correlation_id
    set_correlation_id(str(transaction_id))
    # Lazy-imports Transaction model, PDF generator, and ticket issuer to break circular dependencies.
    from coordinator.models import Transaction
    from coordinator.ticket_generator import generate_tickets_pdf
    from coordinator.tickets import issue_tickets_for_transaction
    # Imports third-party Resend SDK client to dispatch transactional emails with attachments.
    import resend

    try:
        tid = uuid.UUID(str(transaction_id))
    except (ValueError, TypeError, AttributeError):
        logger.error("Invalid transaction_id for ticket email task: %s", transaction_id)
        return "invalid_uuid"

    # Loads the transaction record from PostgreSQL to verify completion and retrieve buyer email address.
    txn = Transaction.objects.filter(transaction_id=tid).first()
    if not txn or not txn.user_email:
        logger.warning("Transaction %s not found or has no email.", tid)
        return "no_email"

    tickets = list(txn.tickets.all().order_by("seat_id"))
    # Idempotent issuance: if tickets have not yet been generated in the database, lazily issues them now before emailing.
    if not tickets:
        try:
            tickets = issue_tickets_for_transaction(txn)
        except Exception as exc:
            logger.error("Failed to issue tickets for email dispatch on %s: %s", tid, exc)
            # Automatically retries the task with exponential delay if network dropouts or Resend API rate limits occur.
        raise self.retry(exc=exc)

    short_ref = str(txn.transaction_id)[:8]
    filename = f"DTC-Ticket-{short_ref}.pdf"

    # Resolves the Resend email service API key, gracefully falling back to simulation mode in development or staging.
    api_key = getattr(settings, "RESEND_API_KEY", None) or os.getenv("RESEND_API_KEY", "")
    # Simulation mode: logs email parameters without making external network calls if RESEND_API_KEY is not configured.
    if not api_key:
        logger.info(
            "RESEND_API_KEY not configured. Simulated ticket email to %s with PDF attachment %s.",
            _mask_email(txn.user_email),
            filename,
        )
        return "simulated"

    from_email = (
        getattr(settings, "RESEND_FROM_EMAIL", None)
        or getattr(settings, "DEFAULT_FROM_EMAIL", None)
        or os.getenv("RESEND_FROM_EMAIL")
        or "DTC Tickets <tickets@resend.dev>"
    )

    resend.api_key = api_key
    try:
        # Renders the high-resolution printable PDF ticket containing QR admission codes, seat metadata, and security references.
        pdf_bytes = generate_tickets_pdf(txn, tickets)
        seat_labels = ", ".join(t.seat_label for t in tickets)
        event_name = tickets[0].event_name if tickets else ""
        # Constructs the email payload with HTML body and attaches the binary PDF ticket stream as a byte list.
        params = {
            "from": from_email,
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
        # Dispatches the outbound email with PDF attachment via Resend's REST API.
        res = resend.Emails.send(params)
        # Extracts Resend's message delivery ID for audit tracing and confirmation logging.
        res_id = res.get("id") if isinstance(res, dict) else str(res)
        logger.info(
            "Ticket email sent successfully: email=%s resend_id=%s",
            _mask_email(txn.user_email),
            res_id,
        )
        return "sent"
    except Exception as exc:
        logger.error(
            "Error dispatching ticket email to %s: %s",
            _mask_email(txn.user_email),
            exc,
        )
        # Automatically retries the task with exponential delay if network dropouts or Resend API rate limits occur.
        raise self.retry(exc=exc)