# Distributed Transaction Coordinator (DTC) - Production Deployment Checklist

This document details the production deployment requirements, environment variables, shared secret configurations, Redis dependencies, and verification procedures for the Distributed Transaction Coordinator (`coordinator-service`, `inventory-service`, and `payment-service`).

---

## 1. Required Environment Variables per Service

### 1.1 `inventory-service`

| Variable | Required? | Default in Dev | Production Requirement |
| :--- | :--- | :--- | :--- |
| `DEBUG` | **Yes** | `False` | Must be set to `False` in production. |
| `SECRET_KEY` | **Yes** | None | Must be a unique, cryptographically random secret string. Hardcoded defaults fail startup. |
| `ALLOWED_HOSTS` | **Yes** | None | Comma-separated domain names or IP addresses (e.g. `inventory.dtc.internal,10.0.0.5`). Cannot be empty. |
| `DB_ENGINE` | No | `django.db.backends.postgresql` | Defaults to PostgreSQL. |
| `DB_NAME` | **Yes** | `inventory_db` | PostgreSQL database name. |
| `DB_USER` | **Yes** | `postgres` | PostgreSQL database username. |
| `DB_PASSWORD` / `PGPASSWORD` | **Yes** | None | Database password. Must be provided in production (`DEBUG=False`). |
| `DB_HOST` | **Yes** | `localhost` | PostgreSQL host. |
| `DB_PORT` | **Yes** | `5432` | PostgreSQL port. |
| `INTERNAL_SERVICE_KEY` | **Yes** | None | Shared internal service secret. Must match `coordinator-service`. Insecure default rejected in prod. |
| `JWT_SIGNING_KEY` | **Yes** | None | Shared HMAC secret for verifying user JWT tokens. Must match `coordinator-service`. |
| `STAFF_API_KEY` | **Yes** | None | API key for administrative operations (e.g., `/seats/reset/`). Insecure default rejected in prod. |
| `REDIS_URL` | **Yes** | `redis://127.0.0.1:6379/1` | Redis connection URL for DRF throttling cache and heartbeat hold keys (`dtc:session:*`). |
| `NUM_PROXIES` | Optional | `0` | Number of reverse proxies in front of the application for client IP resolution (`X-Forwarded-For`). |
| `INVENTORY_USER_THROTTLE_RATE` | Optional | `60/min` | Rate limit per authenticated user. |
| `INVENTORY_ANON_THROTTLE_RATE` | Optional | `300/min` | Rate limit per IP for unauthenticated routes (NAT-aware). |
| `INVENTORY_RESERVE_THROTTLE_RATE` | Optional | `30/min` | Rate limit per user for seat reservation. |
| `INVENTORY_RELEASE_THROTTLE_RATE` | Optional | `30/min` | Rate limit per user for seat release. |
| `INVENTORY_SEAT_LIST_THROTTLE_RATE` | Optional | `300/min` | Rate limit per IP for seat catalog retrieval. |
| `INVENTORY_CHECK_THROTTLE_RATE` | Optional | `120/min` | Rate limit per IP for reservation status polling. |

> [!CAUTION]
> **Prohibited in Production:**
> `ALLOW_UNVERIFIED_IDENTITY_HEADERS` and `ALLOW_UNAUTHENTICATED_RESET` MUST NOT be set to `True` when `DEBUG=False`. The Django system check (`Tags.security`) and startup validation will immediately raise an `ImproperlyConfigured` exception and halt process startup if either is enabled.

---

### 1.2 `coordinator-service`

| Variable | Required? | Default in Dev | Production Requirement |
| :--- | :--- | :--- | :--- |
| `DEBUG` | **Yes** | `False` | Must be set to `False` in production. |
| `SECRET_KEY` | **Yes** | None | Must be a unique, cryptographically random secret string. Hardcoded defaults fail startup. |
| `ALLOWED_HOSTS` | **Yes** | None | Comma-separated domain names or IP addresses (e.g. `coordinator.dtc.internal,10.0.0.4`). |
| `DB_ENGINE` | No | `django.db.backends.postgresql` | Defaults to PostgreSQL. |
| `DB_NAME` | **Yes** | `coordinator_db` | PostgreSQL database name. |
| `DB_USER` | **Yes** | `postgres` | PostgreSQL database username. |
| `DB_PASSWORD` / `PGPASSWORD` | **Yes** | None | Database password. Must be provided in production (`DEBUG=False`). |
| `DB_HOST` | **Yes** | `localhost` | PostgreSQL host. |
| `DB_PORT` | **Yes** | `5432` | PostgreSQL port. |
| `INTERNAL_SERVICE_KEY` | **Yes** | None | Must match `inventory-service` and `payment-service`. Insecure default rejected in prod. |
| `JWT_SIGNING_KEY` | **Yes** | None | Secret key used to issue and sign authentication JWT tokens. Must match `inventory-service`. |
| `STAFF_API_KEY` | **Yes** | None | API key for coordinator admin endpoints (retry compensation, recovery trigger). |
| `ADMIN_DOMAIN` | **Yes** | `@dtc.internal` | Required in prod (`DEBUG=False`). Domain suffix for staff accounts (e.g. `@company.com`). |
| `INVENTORY_SERVICE_URL` | **Yes** | `http://127.0.0.1:8000` | Base URL of the inventory service. |
| `PAYMENT_SERVICE_URL` | **Yes** | `http://127.0.0.1:8001` | Base URL of the payment service. |
| `CELERY_BROKER_URL` / `REDIS_URL` | **Yes** | `redis://127.0.0.1:6379/0` | Redis instance for Celery task queuing, SSE event pub/sub, and recovery locks. |
| `REDIS_SOCKET_TIMEOUT` | Optional | `1.0` (prod) / `0.02` (test) | Socket connect & read timeout for Redis operations. |
| `REQUEST_CONNECT_TIMEOUT` | Optional | `2.0` | Timeout in seconds for establishing outbound HTTP connections. |
| `REQUEST_READ_TIMEOUT` | Optional | `5.0` | Timeout in seconds for waiting on outbound HTTP responses. |

> [!CAUTION]
> **Prohibited in Production:**
> `ALLOW_DEV_STAFF_BYPASS` and `ENABLE_TEST_PAYMENT_FAILURE` MUST NOT be set to `True` when `DEBUG=False`. Startup checks will halt process execution if enabled.

---

### 1.3 `payment-service`

| Variable | Required? | Default in Dev | Production Requirement |
| :--- | :--- | :--- | :--- |
| `DEBUG` | **Yes** | `False` | Must be set to `False` in production. |
| `SECRET_KEY` | **Yes** | None | Must be a unique, cryptographically random secret string. Hardcoded defaults fail startup. |
| `ALLOWED_HOSTS` | **Yes** | None | Comma-separated domain names or IP addresses (e.g. `payment.dtc.internal,10.0.0.6`). |
| `DB_ENGINE` | No | `django.db.backends.postgresql` | Defaults to PostgreSQL. |
| `DB_NAME` | **Yes** | `payment_db` | PostgreSQL database name. |
| `DB_USER` | **Yes** | `postgres` | PostgreSQL database username. |
| `DB_PASSWORD` / `PGPASSWORD` | **Yes** | None | Database password. Must be provided in production (`DEBUG=False`). |
| `DB_HOST` | **Yes** | `localhost` | PostgreSQL host. |
| `DB_PORT` | **Yes** | `5432` | PostgreSQL port. |
| `INTERNAL_SERVICE_KEY` | **Yes** | None | Shared internal service secret. Must match `coordinator-service`. Insecure default rejected in prod. |
| `STAFF_API_KEY` | **Yes** | None | API key for staff payment reconciliation operations. |

---

## 2. Shared-Key & Secret Architecture

### 2.1 `INTERNAL_SERVICE_KEY`
- **Purpose**: Authenticates machine-to-machine service calls between `coordinator-service`, `inventory-service`, and `payment-service`.
- **Enforcement**:
  - Sent via header `X-Internal-Service-Key`.
  - Validated using constant-time comparison (`secrets.compare_digest` or `hmac.compare_digest`) to prevent timing side-channel attacks.
  - Mere presence of the header is insufficient; random or invalid keys will return `401 Unauthorized` or `403 Forbidden`.
  - Rate limiting exemptions only apply when key passes constant-time validation against the configured server secret.
- **Requirement**: Must be identical on all three services.

### 2.2 `JWT_SIGNING_KEY`
- **Purpose**: Signs caller identification tokens (containing `user_id`, `user_email`, `exp`).
- **Enforcement**:
  - `coordinator-service` signs JWTs when users log in or send OTP.
  - `inventory-service` decodes and verifies the signature using HMAC-SHA256 (`HS256`).
- **Requirement**: Must be identical between `coordinator-service` and `inventory-service`.

---

## 3. Redis Dependency & Fail-Open Resilience

Redis is used across DTC for multiple subsystems:

1. **Heartbeat Holds & Session Tracking (`inventory-service`)**:
   - Holds are bound to user identity via Redis keys `dtc:session:{user_id}` and `dtc:session:{user_email}` with a 60-second TTL.
   - The presence sweeper reclaims holds if neither key is refreshed by a client heartbeat.
2. **Checkout Presence Key (`coordinator-service`)**:
   - When entering the `PAYMENT_PROCESSING` step of a saga, the coordinator sets `dtc:checkout:{transaction_id}` with TTL `payment timeout + 60s`.
   - This prevents the inventory service presence sweep from clearing holds while the customer completes 3D-Secure payment authentication.
   - The coordinator removes this key in a `finally` block on all terminal paths (completed, compensated, failed).
3. **DRF Throttling Cache (`inventory-service`)**:
   - High-throughput IP and user bucket counters reside in Redis (`django-redis` / `CACHES['default']`).
4. **Celery Task Queue & Distributed Recovery Lock (`coordinator-service`)**:
   - Non-blocking saga execution uses Redis as the Celery broker (`coordinator_queue`).
   - Periodic crash-recovery sweep uses Redis distributed lock `dtc:recovery:lock` with 120s expiry.
5. **Real-Time SSE Streaming (`coordinator-service`)**:
   - Publishes transaction state changes to channel `dtc:tx_stream:{transaction_id}`.

### Fail-Open Resilience Guarantee:
- **Checkout Presence Fail-Open**: If Redis is offline or experiences network partitions during `_set_checkout_presence` or `_clear_checkout_presence`, the operation logs a warning and fails open without interrupting the saga.
- **SSE Stream Fail-Open**: If Redis pub/sub is unreachable, status transitions complete normally in PostgreSQL and `_publish_transaction_update` logs a warning.
- **Recovery Lock Fail-Open**: If Redis is down during `trigger_recovery_sweep`, the lock check logs a warning and proceeds with database-level CAS claims (`expected_status` conditional updates).
- **Socket Timeouts**: All Redis client instances configure explicit socket connect and read timeouts (`REDIS_SOCKET_TIMEOUT = 1.0` in prod, `0.02` in tests) to prevent thread pool starvation if Redis fails.

---

## 4. Verification & Smoke Test Commands

Run the following checks on each deployed instance before routing traffic:

### 4.1 Verify Redis Connectivity
```bash
# Verify Redis is reachable and responding
redis-cli -u "$REDIS_URL" ping
# Expected output: PONG
```

### 4.2 Verify Django Configuration & Security Checks
Run management checks inside each service directory:
```bash
# 1. Standard configuration check
python manage.py check

# 2. Production deployment security check
python manage.py check --deploy

# 3. Verify database migrations are fully applied
python manage.py migrate --check

# 4. Verify no unapplied model schema changes exist
python manage.py makemigrations --check --dry-run
```
*All commands must exit with code `0` and report `System check identified no issues`.*

### 4.3 Verify Readiness & Health Endpoints

#### Coordinator Service (Port 8002):
```bash
# Health probe (liveness)
curl -i http://localhost:8002/health/
# Expected: HTTP 200 OK {"status": "ok"}

# Ready probe (database + broker connectivity)
curl -i http://localhost:8002/ready/
# Expected: HTTP 200 OK {"status": "ready"}
```

#### Inventory Service (Port 8000):
```bash
# Health probe (liveness)
curl -i http://localhost:8000/health/
# Expected: HTTP 200 OK {"status": "ok"}

# Ready probe (database + cache connectivity)
curl -i http://localhost:8000/ready/
# Expected: HTTP 200 OK {"status": "ready"}
```

#### Payment Service (Port 8001):
```bash
# Health probe (liveness)
curl -i http://localhost:8001/health/
# Expected: HTTP 200 OK {"status": "ok"}

# Ready probe
curl -i http://localhost:8001/ready/
# Expected: HTTP 200 OK {"status": "ready"}
```

### 4.4 Automated Test Suite Verification
Before deploying a release artifact, run test suites with production configuration:
```bash
# Inventory Service (163 tests)
cd inventory-service && python manage.py test inventory --noinput

# Payment Service (70 tests)
cd payment-service && python manage.py test payments --noinput

# Coordinator Service (232 tests)
cd coordinator-service && python manage.py test coordinator --noinput --durations 10 --timing
```
*Expected: All 465 tests pass cleanly in under 30 seconds total.*
