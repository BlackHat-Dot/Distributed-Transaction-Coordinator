# verified

# Imports standard JSON library to serialize real-time event payloads and deserialize audit log data.
import json
# Imports standard logging library to log saga state transitions, warnings, and Redis connection diagnostics.
import logging
# Imports threading primitives to synchronize shared client connection pools across concurrent threads.
import threading
# Imports standard time library for monotonic timestamps powering the Redis circuit-breaker cooldown window.
import time
# Imports redis-py client library to publish transaction lifecycle events to Redis Pub/Sub channels for Server-Sent Events (SSE) streaming.
import redis
# Imports Django runtime settings to load broker URLs, socket timeouts, and environment configuration.
from django.conf import settings
# Imports Django's database transaction module to manage atomic database blocks, row-level locks (select_for_update), and on_commit hooks.
from django.db import transaction as db_transaction
# Imports Django ORM Max aggregate function to determine the highest existing event sequence number.
from django.db.models import Max
# Imports Django timezone utilities to timestamp transaction updates in UTC database format.
from django.utils import timezone

# Imports Transaction and SagaEvent ORM models representing parent transactions and immutable audit steps.
from .models import Transaction, SagaEvent

logger = logging.getLogger(__name__)

# Global singleton holding the active Redis pub/sub client connection across the worker process.
_pub_client = None
# Monotonic timestamp recording when Redis connectivity was last probed to throttle reconnection attempts.
_pub_last_check = 0.0
# Mutex lock protecting the singleton Redis publisher client from race conditions during concurrent worker thread reconnection.
_pub_lock = threading.Lock()


# Thread-safe factory returning a healthy cached Redis client with circuit-breaker protection against connection storming during Redis downtime.
def _get_pub_client():
    """
    Thread-safe, self-healing Redis publisher client.
    Guarded by double-checked locking and a 5.0s circuit-breaker cooldown.
    """
    # Declares access to module-level singleton Redis instance and cooldown timer state variables.
    global _pub_client, _pub_last_check

    # Fast-path check: returns active client immediately without acquiring synchronization lock if already initialized.
    if _pub_client is not None:
        return _pub_client

    # Double-checked locking pattern: synchronizes client instantiation across concurrent threads to prevent race conditions.
    with _pub_lock:
        # Re-verifies client presence after acquiring lock in case another thread initialized it while waiting.
        if _pub_client is not None:
            return _pub_client

        now = time.monotonic()
        # Circuit-breaker cooldown: if Redis failed within the last 5 seconds, immediately abort to prevent stalling active transactions.
        if now - _pub_last_check < 5.0:
            return None

        # Records current timestamp to mark this probe attempt and enforce the next cooldown window.
        _pub_last_check = now
        # Resolves Redis connection URL from Celery broker configuration with local default fallback.
        broker_url = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
        # Determines whether unit test suite is executing to enforce aggressively short connection timeouts.
        testing_mode = bool(getattr(settings, "IS_TESTING", False))
        timeout = getattr(settings, "REDIS_SOCKET_TIMEOUT", 0.05 if testing_mode else 1.0)

        client = None
        # Attempts socket connection to Redis instance, wrapping network calls to catch unreachable hosts safely.
        try:
            client = redis.Redis.from_url(
                broker_url,
                decode_responses=True,
                socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )
            # Sends an active PING command to verify the connection is alive before caching the singleton instance.
            client.ping()
            # Caches verified live Redis client in module singleton for subsequent publisher requests.
            _pub_client = client
            return _pub_client
        # Handles Redis connection failures by closing leaked sockets and logging diagnostics without raising.
        except Exception as e:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            _pub_client = None
            logger.warning("Failed to initialize Redis publisher client: %s", e)
            return None


# Strict directed acyclic/cyclic state transition graph enforcing legal status paths to prevent illegal state jumps or out-of-order execution.
ALLOWED_TRANSITIONS = {
    # Starting state: may transition forward into inventory reservation, holding, or directly into failure/recovery modes on immediate fault.
    Transaction.Status.STARTED: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # Temporary hold transitions: allows proceeding to confirmed reservation, timing out, or rolling back via compensation.
    Transaction.Status.HOLDING: {
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # Confirmed inventory transitions: allows proceeding to payment processing, holding, compensation, or timeout.
    Transaction.Status.INVENTORY_RESERVED: {
        Transaction.Status.PAYMENT_PROCESSING,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.TIMEOUT,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # In-flight payment transitions: leads to completion on charge success, or failure/timeout/compensation paths on error.
    Transaction.Status.PAYMENT_PROCESSING: {
        Transaction.Status.COMPLETED,
        Transaction.Status.PAYMENT_FAILED,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.COMPENSATING,
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # Payment timeout transitions: allows sweeper recovery, charge verification completion, or compensation rollback.
    Transaction.Status.PAYMENT_TIMEOUT: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    # Payment network error transitions: enables status verification sweep, completion if charged, or rollback compensation.
    Transaction.Status.PAYMENT_CONNECTION_ERROR: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    # Inventory timeout transitions: permits moving to reservation failure, timeout, or recovery in progress.
    Transaction.Status.INVENTORY_TIMEOUT: {
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    # Inventory connection fault transitions: allows transition to reservation failure, timeout, or sweeper claiming.
    Transaction.Status.INVENTORY_CONNECTION_ERROR: {
        Transaction.Status.INVENTORY_RESERVE_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.TIMEOUT,
    },
    # Active rollback transitions: leads to fully compensated state, compensation failure, retry resolution, or sweeper claim.
    Transaction.Status.COMPENSATING: {
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.RECOVERY_IN_PROGRESS,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    },
    # Failed compensation transitions: strictly limits transitions to recovery sweeper claiming for automated or manual retry.
    Transaction.Status.COMPENSATION_FAILED: {
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # Failed retry transitions: allows background sweeper to re-claim the transaction for escalation or retry.
    Transaction.Status.RETRY_COMPENSATION_FAILED: {
        Transaction.Status.RECOVERY_IN_PROGRESS,
    },
    # Sweeper active lock transitions: permits recovery progression or rollback reversion on transient error.
    Transaction.Status.RECOVERY_IN_PROGRESS: {
        Transaction.Status.COMPENSATING,
        Transaction.Status.COMPLETED,
        Transaction.Status.COMPENSATED,
        Transaction.Status.COMPENSATED_ON_RETRY,
        Transaction.Status.COMPENSATION_FAILED,
        Transaction.Status.RETRY_COMPENSATION_FAILED,
        Transaction.Status.TIMEOUT,
        # Reversion targets when sweeper or retry encounters transport error
        Transaction.Status.STARTED,
        Transaction.Status.HOLDING,
        Transaction.Status.INVENTORY_RESERVED,
        Transaction.Status.PAYMENT_PROCESSING,
        Transaction.Status.PAYMENT_TIMEOUT,
        Transaction.Status.PAYMENT_CONNECTION_ERROR,
        Transaction.Status.INVENTORY_TIMEOUT,
        Transaction.Status.INVENTORY_CONNECTION_ERROR,
    },
}


# Dispatches real-time JSON state events over Redis pub/sub channel dtc:tx_stream:<id> to push live updates to the browser frontend via SSE.
def _publish_transaction_update(transaction_id, to_status, from_status=None):
    """Publish real-time transaction state update to Redis pub/sub channel for SSE streaming."""
    # References module singleton Redis client to reuse connection for event publishing.
    global _pub_client
    try:
        # Resolves healthy Redis client from connection factory, exiting silently if Redis broker is currently unavailable.
        client = _get_pub_client()
        if not client:
            return
        # Assembles event dictionary containing transaction UUID, lifecycle states, terminal flag, and recovery necessity.
        payload = json.dumps({
            "transaction_id": str(transaction_id),
            "status": to_status,
            "from_status": from_status,
            "is_terminal": to_status in Transaction.TERMINAL_STATUSES,
            "needs_recovery": to_status in Transaction.RECOVERABLE_STATUSES,
        })
        # Publishes the serialized JSON event to the per-transaction Redis channel subscribed to by EventSource SSE clients.
        client.publish(f"dtc:tx_stream:{transaction_id}", payload)
    # Catches pub/sub network errors, logs non-fatal diagnostic warning, and invalidates cached client under mutex lock.
    except Exception as exc:
        logger.warning("Failed to publish SSE event for tx %s: %s", transaction_id, exc)
        with _pub_lock:
            if _pub_client is not None:
                try:
                    _pub_client.close()
                except Exception:
                    pass
                _pub_client = None


# Blacklist of case-insensitive sensitive keys and PII tokens that must be sanitized before persisting into audit payloads.
SENSITIVE_KEYS = {
    "user_email", "email", "card_number", "credit_card", "pan", "cvv", "cvc",
    "password", "token", "otp", "secret", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "private_key",
    "secret_key", "staff_key", "x-staff-key",
}


# Recursively traverses arbitrary nested dictionaries and lists to partially mask emails and redact authentication tokens/passwords.
def _mask_pii(data):
    """
    Recursively masks sensitive fields (email, credentials, tokens) in audit payloads.
    """
    # Short-circuit check returning non-collection or empty payloads unmodified.
    if not data:
        return data
    # Traverses dictionary key-value pairs, normalizing keys to lowercase to identify sensitive attributes.
    if isinstance(data, dict):
        masked = {}
        for k, v in data.items():
            clean_k = str(k).strip().lower()
            # Detects PII and credential keys to apply appropriate email partial masking or token redaction.
            if clean_k in SENSITIVE_KEYS:
                if isinstance(v, str) and "@" in v:
                    parts = v.split("@", 1)
                    user_part = parts[0]
                    masked_user = (user_part[0] + "***" + user_part[-1]) if len(user_part) > 2 else "***"
                    masked[k] = f"{masked_user}@{parts[1]}"
                elif isinstance(v, str) and len(v) > 4:
                    masked[k] = f"****{v[-4:]}"
                else:
                    masked[k] = "******"
            elif isinstance(v, (dict, list)):
                masked[k] = _mask_pii(v)
            else:
                masked[k] = v
        return masked
    elif isinstance(data, list):
        return [_mask_pii(item) for item in data]
    return data


# Core Compare-And-Swap (CAS) orchestrator: atomically updates transaction state under row lock and records immutable SagaEvent audit log.
def _set_status_with_event(
    transaction_id,
    to_status,
    expected_status=None,
    actor=SagaEvent.ACTOR_REQUEST,
    http_status_code=None,
    http_payload=None,
):
    """
    Execute compare-and-swap (CAS) status update within an exclusive database transaction lock.
    Records an append-only SagaEvent audit log entry and registers an on_commit SSE broadcast.
    """
    if not transaction_id:
        return False

    # Defensive guard verifying the requested destination status exists within the official Transaction.Status enum.
    if to_status not in Transaction.Status.values:
        logger.warning(
            "Transition aborted for tx %s: %r is invalid; caller must use a valid Transaction.Status enum constant",
            transaction_id,
            to_status,
        )
        return False

    # Wraps status verification, update, and event creation inside an isolated atomic database transaction block.
    with db_transaction.atomic():
        # SELECT ... FOR UPDATE acquires a pessimistic row-level database lock, preventing concurrent workers from updating the same transaction simultaneously.
        qs = Transaction.objects.select_for_update().filter(
            transaction_id=transaction_id
        )
        # CAS Guard: verifies that the current database status matches expected_status; fails safely if another worker already transitioned it.
        if expected_status is not None:
            if isinstance(expected_status, (list, tuple, set)):
                qs = qs.filter(status__in=[s for s in expected_status if s is not None])
            else:
                qs = qs.filter(status=expected_status)

        rows = list(qs.values("id", "status"))
        if not rows:
            return False

        txn_pk = rows[0]["id"]
        from_status = rows[0]["status"]

        if to_status == from_status:
            # Idempotency fast-path: if the transaction is already in the target status, exit immediately without duplicate writes.
            return True

        # Validate CAS transition against ALLOWED_TRANSITIONS
        if expected_status is not None and to_status != from_status:
            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            # Validates that the requested state jump is legally declared in ALLOWED_TRANSITIONS to protect state machine integrity.
            if to_status not in allowed:
                logger.warning(
                    "Illegal CAS state transition rejected for tx %s: %s -> %s (allowed: %s)",
                    transaction_id,
                    from_status,
                    to_status,
                    allowed,
                )
                return False

        # Atomically mutates the persisted transaction status and bumps updated_at timestamp within the acquired row lock.
        Transaction.objects.filter(pk=txn_pk).update(
            status=to_status,
            updated_at=timezone.now(),
        )

        last_seq = (
            SagaEvent.objects.filter(transaction_id=txn_pk)
            .aggregate(m=Max("seq"))["m"]
            or 0
        )
        # Computes the next monotonically increasing sequence number (1, 2, 3...) to enforce causal event ordering in the audit log.
        next_seq = last_seq + 1

        # Appends an immutable audit event to the historical ledger, recording the state transition, actor, and sanitized HTTP payload.
        SagaEvent.objects.create(
            transaction_id=txn_pk,
            seq=next_seq,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            http_status_code=http_status_code,
            # Sanitizes downstream request/response payloads before committing them to the persistent audit database.
            http_payload=_mask_pii(http_payload),
        )

        # Post-commit hook: defers publishing the Redis SSE update until the database transaction successfully commits, preventing dirty event reads.
        db_transaction.on_commit(
            lambda tid=transaction_id, ts=to_status, fs=from_status: _publish_transaction_update(tid, ts, fs)
        )

    # Successfully transitioned state, recorded audit event, and registered SSE notification.
    return True