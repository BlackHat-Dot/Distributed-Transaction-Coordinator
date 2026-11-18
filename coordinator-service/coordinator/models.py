# Imports Django's Object-Relational Mapping (ORM) models package to define database tables, column types, and constraints as Python classes.
from django.db import models


# Primary database entity tracking the distributed lifecycle, saga state, booking details, and compensation history of each checkout transaction.
class Transaction(models.Model):

    # Defines the complete enumerated state machine controlling every phase, transient step, and terminal failure mode of a saga transaction.
    class Status(models.TextChoices):
        # Initial starting state indicating that the transaction has been recorded but downstream service calls have not yet completed.
        STARTED = "STARTED"
        # Transient reservation state indicating seats are temporarily locked in Inventory while awaiting downstream payment processing.
        HOLDING = "HOLDING"
        # Confirms that requested seats were successfully locked in Inventory Service with an active hold expiration deadline.
        INVENTORY_RESERVED = "INVENTORY_RESERVED"
        # Indicates compensation completed successfully in Inventory Service, releasing previously held seats back into the available pool.
        INVENTORY_RELEASED = "INVENTORY_RELEASED"
        # Terminal failure state recorded when Inventory Service explicitly rejects the reservation request (e.g. seats already sold).
        INVENTORY_RESERVE_FAILED = "INVENTORY_RESERVE_FAILED"
        # Ambiguous state recorded when an HTTP request to reserve seats in Inventory times out, qualifying for background recovery sweep.
        INVENTORY_TIMEOUT = "INVENTORY_TIMEOUT"
        # Confirms through status probe that Inventory Service has no active seat reservation record for this transaction ID.
        NOT_RESERVED = "NOT_RESERVED"
        # Ambiguous state indicating network connectivity failure or DNS resolution issue when reaching Inventory Service.
        INVENTORY_CONNECTION_ERROR = "INVENTORY_CONNECTION_ERROR"
        # Active in-flight state indicating payment charge request has been dispatched to Payment Service and awaits gateway confirmation.
        PAYMENT_PROCESSING = "PAYMENT_PROCESSING"
        # Terminal failure state recorded when Payment Service explicitly declines the charge (e.g. insufficient funds or card failure).
        PAYMENT_FAILED = "PAYMENT_FAILED"
        # Ambiguous state recorded when charge call to Payment Service times out, requiring recovery sweeper to verify charge status.
        PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"
        # Ambiguous state indicating network-level transport error or DNS failure when attempting to reach Payment Service.
        PAYMENT_CONNECTION_ERROR = "PAYMENT_CONNECTION_ERROR"
        # Active transient state indicating compensation workflow is underway to release held inventory or issue refunds.
        COMPENSATING = "COMPENSATING"
        # Terminal state confirming all compensation actions succeeded and any held seats or charges were fully reversed.
        COMPENSATED = "COMPENSATED"
        # Failure state recorded when automated compensation could not complete, requiring manual retry or escalation.
        COMPENSATION_FAILED = "COMPENSATION_FAILED"
        # Terminal resolution state indicating successful compensation achieved via subsequent automated or staff retry.
        COMPENSATED_ON_RETRY = "COMPENSATED_ON_RETRY"
        # Escalation state indicating repeated compensation retry attempts failed, demanding operator investigation.
        RETRY_COMPENSATION_FAILED = "RETRY_COMPENSATION_FAILED"
        # Mutex flag state claimed by recovery sweepers via Compare-And-Swap to prevent concurrent worker collisions.
        RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
        # Successful terminal state confirming seats confirmed in Inventory, payment charged, and tickets generated.
        COMPLETED = "COMPLETED"
        # Ambiguous failure state when generic network disconnection prevents saga progression across multiple services.
        CONNECTION_FAILED = "CONNECTION_FAILED"
        # Intermediate confirmation state verifying payment deduction succeeded before final ticket issuance.
        CHARGED = "CHARGED"
        TIMEOUT = "TIMEOUT"

    # Set of irreversible final states; transactions in these states are settled and can never be claimed or modified by background recovery workers.
    TERMINAL_STATUSES = {
        Status.COMPLETED,
        Status.COMPENSATED,
        Status.COMPENSATION_FAILED,
        Status.INVENTORY_RESERVE_FAILED,
        Status.COMPENSATED_ON_RETRY,
        Status.RETRY_COMPENSATION_FAILED,
        Status.TIMEOUT,
        Status.PAYMENT_TIMEOUT,
        Status.PAYMENT_FAILED,
        Status.PAYMENT_CONNECTION_ERROR,
        Status.INVENTORY_TIMEOUT,
        Status.INVENTORY_CONNECTION_ERROR,
    }

    # Non-terminal or ambiguous states that qualify for automated background compensation sweeps to resolve orphaned holds or unverified charges.
    RECOVERABLE_STATUSES = {
        Status.PAYMENT_PROCESSING,
        Status.INVENTORY_RESERVED,
        Status.STARTED,
        Status.INVENTORY_TIMEOUT,
        Status.PAYMENT_TIMEOUT,
        Status.INVENTORY_CONNECTION_ERROR,
        Status.PAYMENT_CONNECTION_ERROR,
        Status.COMPENSATING,
        Status.COMPENSATION_FAILED,
        Status.RETRY_COMPENSATION_FAILED,
    }

    # Unique 128-bit UUID identifying this saga instance; acts as the universal idempotency key across Coordinator, Inventory, and Payment services.
    transaction_id = models.UUIDField(unique=True)
    # Deterministic SHA-256 hashed user identifier (e.g. usr_<hash>) used for fast indexing, authorization checks, and multi-tenant scoping.
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_email = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    # Persisted current state of the transaction; validated against Status.choices and updated atomically via Compare-And-Swap (CAS) in saga_log.py.
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
    )

    # JSON array containing the numeric primary keys of physical seats reserved in Inventory Service for this transaction.
    seat_ids = models.JSONField(null=True, blank=True)
    seat_numbers = models.CharField(max_length=255, null=True, blank=True)
    quantity = models.IntegerField(null=True, default=1)

    event_name = models.CharField(max_length=200, null=True, blank=True)
    venue = models.CharField(max_length=200, null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)

    # Total monetary charge in exact two-decimal currency precision to eliminate IEEE 754 floating-point rounding errors during payment gateway processing.
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    # Immutable audit timestamp set automatically when the transaction row is first inserted, used to calculate holding timeouts and sweep eligibility.
    created_at = models.DateTimeField(auto_now_add=True)
    # Mutable audit timestamp updated automatically on every row modification, used by optimistic locking and recovery sweepers.
    updated_at = models.DateTimeField(auto_now=True)

    # Returns the stringified UUID for clean representation in Django admin panels, Celery logs, and exception tracebacks.
    def __str__(self):
        return str(self.transaction_id)


# Append-only audit log recording every discrete state transition, initiating actor, and HTTP outcome throughout the saga execution.
class SagaEvent(models.Model):

    # Identifies which subsystem executed the transition: normal HTTP user flow ('request'), automated sweeper ('recovery'), or staff manual intervention ('retry').
    ACTOR_REQUEST = "request"
    ACTOR_RECOVERY = "recovery"
    ACTOR_RETRY = "retry"

    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        related_name="events",
    )
    # Monotonically increasing sequence number (1, 2, 3...) per transaction, strictly enforcing causal event ordering in the distributed log.
    seq = models.PositiveIntegerField(
        help_text="1-based sequence number, monotonically increasing per transaction.",
    )
    # Records the previous state and target state to form an immutable directed graph edge of the saga state machine transition.
    from_status = models.CharField(max_length=40)
    to_status = models.CharField(max_length=40)
    actor = models.CharField(
        max_length=20,
        help_text="Which code path wrote this transition: 'request', 'recovery', or 'retry'.",
    )
    http_status_code = models.IntegerField(null=True, blank=True)
    # Captures the raw HTTP response payload received from downstream services (Payment or Inventory) for post-mortem incident analysis.
    http_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Inner configuration class that provides database-level options and metadata for this Django model.

        # Enforces a composite database UNIQUE constraint: guarantees no duplicate sequence numbers exist for the same transaction, preventing race conditions.
        unique_together = [("transaction", "seq")]

        # Ensures all ORM queries return events in strict chronological sequence (ORDER BY transaction_id ASC, seq ASC) for faithful saga replay.
        ordering = ["transaction", "seq"]

    def __str__(self):
        return (
            f"SagaEvent(txn={self.transaction_id}, seq={self.seq}, "
            f"{self.from_status}->{self.to_status})"
        )


# Digital admittance ticket issued upon COMPLETED saga transactions, verified at venue gates via unique cryptographic codes.
class Ticket(models.Model):
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        related_name="tickets",
    )
    seat_id = models.IntegerField(db_index=True)
    seat_label = models.CharField(max_length=64)
    event_name = models.CharField(max_length=200)
    venue = models.CharField(max_length=200, blank=True, default="")
    starts_at = models.DateTimeField(null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Cryptographically secure random alphanumeric barcode/QR string (e.g. TKT-...) presented by attendees at entry scanning gates.
    ticket_code = models.CharField(max_length=64, unique=True, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    # Gate redemption timestamp; indexed for rapid entry validation and set to NULL until the ticket barcode is successfully scanned at the gate.
    used_at = models.DateTimeField(null=True, blank=True, db_index=True)
    used_by = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        # Prevents double-issuance: guarantees a single physical seat within a transaction can only have one corresponding ticket issued.
        unique_together = [("transaction", "seat_id")]
        ordering = ["seat_id"]

    def __str__(self):
        return f"Ticket({self.ticket_code}, txn={self.transaction_id}, seat={self.seat_label})"
