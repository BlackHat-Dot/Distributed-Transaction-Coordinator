# Environment Variable Mapping Across DTC Services

This document catalogs all environment variables read across all services (`coordinator-service`, `inventory-service`, `payment-service`, and utilities), their source locations, defaults, purposes, and whether `settings.py` also reads or defines them.

| Name | File:Line | Default | Purpose | Also in `settings.py`? |
| :--- | :--- | :--- | :--- | :--- |
| `DEBUG` | `coordinator-service/coordinator_service/settings.py:9` | `"False"` | Toggles Django debug mode | Yes (Primary definition) |
| `DEBUG` | `inventory-service/inventory_service/settings.py:8` | `"False"` | Toggles Django debug mode | Yes (Primary definition) |
| `DEBUG` | `payment-service/payment_service/settings.py:8` | `"False"` | Toggles Django debug mode | Yes (Primary definition) |
| `SECRET_KEY` | `coordinator-service/coordinator_service/settings.py:18` | None (dev fallback) | Django cryptographic secret | Yes |
| `SECRET_KEY` | `inventory-service/inventory_service/settings.py:17` | None (dev fallback) | Django cryptographic secret | Yes |
| `SECRET_KEY` | `payment-service/payment_service/settings.py:17` | None (dev fallback) | Django cryptographic secret | Yes |
| `ALLOWED_HOSTS` | `coordinator-service/coordinator_service/settings.py:33` | `["*"]` in dev | Allowed host header domains | Yes |
| `ALLOWED_HOSTS` | `inventory-service/inventory_service/settings.py:31` | `["*"]` in dev | Allowed host header domains | Yes |
| `ALLOWED_HOSTS` | `payment-service/payment_service/settings.py:31` | `["*"]` in dev | Allowed host header domains | Yes |
| `INTERNAL_SERVICE_KEY` | `coordinator-service/coordinator_service/settings.py:48` | None | Shared secret for inter-service communication | Yes |
| `INTERNAL_SERVICE_KEY` | `inventory-service/inventory_service/settings.py:44` | None | Shared secret for inter-service communication | Yes |
| `INTERNAL_SERVICE_KEY` | `payment-service/payment_service/settings.py:44` | None | Shared secret for inter-service communication | Yes |
| `INTERNAL_SERVICE_KEY_FALLBACK` | `payment-service/payments/permissions.py:10` | None | Deprecated key fallback during rotation | No |
| `STAFF_API_KEY` | `coordinator-service/coordinator/views.py:56` | `""` | Key for admin staff access (`X-Staff-Key`) | Yes (`checks.py`, `views.py`) |
| `STAFF_API_KEY` | `inventory-service/inventory_service/settings.py:48` | None | Key for admin staff access | Yes |
| `GATE_API_KEY` | `coordinator-service/coordinator/ticket_views.py:342` | `""` | Scanner API key for gate ticket redemption | Yes (in `settings.GATE_API_KEY`) |
| `GATE_API_KEY` | `coordinator-service/coordinator_service/settings.py:328` | None | Scanner API key for gate ticket redemption | Yes |
| `INVENTORY_SERVICE_URL` | `coordinator-service/coordinator/views.py:27` | `"http://127.0.0.1:8000"` | Base URL for downstream Inventory service | No |
| `PAYMENT_SERVICE_URL` | `coordinator-service/coordinator/views.py:28` | `"http://127.0.0.1:8001"` | Base URL for downstream Payment service | No |
| `REQUEST_CONNECT_TIMEOUT` | `coordinator-service/coordinator/views.py:30` | `2.0` | HTTP connect timeout (seconds) | No |
| `REQUEST_READ_TIMEOUT` | `coordinator-service/coordinator/views.py:32` | `5.0` | HTTP read timeout (seconds) | No |
| `RECOVERY_MIN_THRESHOLD_SECONDS` | `coordinator-service/coordinator/views.py:59` | `30` | Min age before stuck txns are swept | No |
| `RECOVERY_THRESHOLD_SECONDS` | `coordinator-service/coordinator/management/commands/recovery_stuck_transactions.py:48` | `60` | CLI default threshold for recovery sweep | No |
| `MAX_SEATS_PER_TRANSACTION` | `coordinator-service/coordinator/views.py:61` | `10` | Max seats allowed per booking transaction | No |
| `ADMIN_DOMAIN` | `coordinator-service/coordinator/views.py:63` | `""` | Email domain treated as admin/staff | No |
| `STAFF_USER_ID` | `coordinator-service/coordinator/views.py:65` | `"system_staff"` | Fallback user ID for staff API key requests | No |
| `STAFF_EMAIL_ID` | `coordinator-service/coordinator/views.py:67` | `"admin@dtc.internal"` | Fallback email for staff API key requests | No |
| `ALLOW_DEV_STAFF_BYPASS` | `coordinator-service/coordinator_service/settings.py:350` | `False` | Toggles dev bypass without staff key | Yes |
| `ENABLE_FAULT_INJECTION` | `coordinator-service/coordinator/views.py:92` | `False` | Enables simulated failures in tests | No |
| `ENABLE_TEST_PAYMENT_FAILURE` | `payment-service/payments/views.py:172` | `False` | Simulates payment decline in test mode | Yes (tested via settings) |
| `ALLOW_UNAUTHENTICATED_RESET` | `inventory-service/inventory_service/settings.py:273` | `False` | Permits unauthenticated demo seat resets | Yes |
| `ALLOW_UNVERIFIED_IDENTITY_HEADERS` | `inventory-service/inventory_service/settings.py:255` | `False` | Permits unverified identity headers in dev | Yes |
| `HOLD_DURATION_MINUTES` | `inventory-service/inventory/services.py:84` | `10` | Default hold window for seat reservations | No |
| `HOLD_PRESENCE_GRACE_SECONDS` | `inventory-service/inventory/services.py:115` | `45` | Heartbeat presence grace period | No |
| `MAX_USER_HOLDS` | `inventory-service/inventory/views.py:264` | `4` | Concurrency limit on active holds per user | No |
| `CELERY_BROKER_URL` | `coordinator-service/coordinator_service/settings.py:265` | `"redis://127.0.0.1:6379/0"` | Redis broker URL for Celery | Yes |
| `REDIS_URL` | `inventory-service/inventory/services.py:57` | Fallback to `CELERY_BROKER_URL` | Redis URL for inventory lock/presence | No |
| `REDIS_SOCKET_TIMEOUT` | `coordinator-service/coordinator_service/settings.py:280` | `0.02` test / `1.0` prod | Redis connection/socket timeout | Yes |
| `DATABASE_URL` / `POSTGRES_URL` | `coordinator-service/coordinator_service/settings.py:209` | None | PostgreSQL database connection URL | Yes |
| `DB_NAME` / `PGDATABASE` | `coordinator-service/coordinator_service/settings.py:242` | `"dtc_coordinator"` | Database name | Yes |
| `DB_USER` / `PGUSER` | `coordinator-service/coordinator_service/settings.py:245` | `"postgres"` | Database user | Yes |
| `DB_PASSWORD` / `PGPASSWORD` | `coordinator-service/coordinator_service/settings.py:246` | `""` | Database password | Yes |
| `DB_HOST` / `PGHOST` | `coordinator-service/coordinator_service/settings.py:247` | `"localhost"` | Database host | Yes |
| `DB_PORT` / `PGPORT` | `coordinator-service/coordinator_service/settings.py:248` | `"5432"` | Database port | Yes |
| `RESEND_API_KEY` | `coordinator-service/coordinator_service/settings.py:313` | `""` | Resend email service API key | Yes |
| `PUBLIC_BASE_URL` | `coordinator-service/coordinator_service/settings.py:301` | `""` | Public hostname for ticket QR codes | Yes |
| `TRUSTED_PROXIES` | `coordinator-service/coordinator/auth_views.py:185` | `"127.0.0.1,::1"` | Comma-separated trusted reverse proxy IPs | No |
