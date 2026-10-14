# Comprehensive Codebase Audit Coverage Ledger

This ledger accounts for every source, configuration, model, view, task, migration, container specification, and test file in the repository.

---

## 1. Coordinator Service (`coordinator-service/`)

| File Path | Entity / Symbol / Endpoint | Status | Finding IDs |
| :--- | :--- | :---: | :--- |
| `coordinator/views.py` | `transactions_endpoint` (POST /transactions/) | REVIEWED | SEC-04, SEC-07 |
| `coordinator/views.py` | `get_transaction` (GET /transactions/<id>/) | REVIEWED | SEC-07 |
| `coordinator/views.py` | `list_transactions` (GET /transactions/all/) | REVIEWED | SEC-07 |
| `coordinator/views.py` | `list_transaction_events` (GET /events/) | REVIEWED | No findings |
| `coordinator/views.py` | `retry_compensation` (POST /retry-compensation/) | REVIEWED | **SEC-01, REL-01** |
| `coordinator/views.py` | `trigger_recovery_sweep` (POST /recovery/sweep/) | REVIEWED | **SEC-02, DOS-01** |
| `coordinator/views.py` | `recovery_status` (GET /recovery/status/) | REVIEWED | No findings |
| `coordinator/views.py` | `health_check` (GET /health/) | REVIEWED | No findings |
| `coordinator/views.py` | `_get_caller()` | REVIEWED | SEC-04, SEC-07 |
| `coordinator/views.py` | `_run_saga()` | REVIEWED | No findings |
| `coordinator/views.py` | `_reserve()` | REVIEWED | No findings |
| `coordinator/views.py` | `_charge()` | REVIEWED | No findings |
| `coordinator/views.py` | `_confirm()` | REVIEWED | No findings |
| `coordinator/views.py` | `_compensate()` | REVIEWED | **REL-01 (Critical)** |
| `coordinator/auth_views.py` | `send_otp` (POST /auth/send-otp/) | REVIEWED | **SEC-05, DOS-02** |
| `coordinator/auth_views.py` | `verify_otp` (POST /auth/verify-otp/) | REVIEWED | No findings |
| `coordinator/auth_views.py` | `get_me` (GET /auth/me/) | REVIEWED | No findings |
| `coordinator/auth_views.py` | `heartbeat` (POST /auth/heartbeat/) | REVIEWED | No findings |
| `coordinator/auth_views.py` | `logout` (POST /auth/logout/) | REVIEWED | No findings |
| `coordinator/auth_views.py` | `_create_jwt()`, `verify_jwt()` | REVIEWED | No findings |
| `coordinator/auth_views.py` | `_get_client_ip()` | REVIEWED | **SEC-05** |
| `coordinator/auth_views.py` | `validate_email()`, `_generate_user_id()` | REVIEWED | No findings |
| `coordinator/models.py` | `Transaction` Model | REVIEWED | No findings |
| `coordinator/models.py` | `SagaEvent` Model | REVIEWED | No findings |
| `coordinator/saga_log.py` | `_set_status_with_event()` | REVIEWED | No findings |
| `coordinator/tasks.py` | `execute_saga_task()` | REVIEWED | No findings |
| `coordinator/tasks.py` | `trigger_recovery_sweep_task()` | REVIEWED | No findings |
| `coordinator/middleware.py`| `CorrelationIdMiddleware` | REVIEWED | No findings |
| `commands/recovery_stuck_transactions.py`| `Command.handle()`, `_claim()` | REVIEWED | No findings |
| `commands/recovery_stuck_transactions.py`| `_resolve_transaction()`, `_compensate()` | REVIEWED | **REL-01 (Critical)** |
| `coordinator_service/settings.py`| Django Settings Configuration | REVIEWED | SEC-06 |
| `coordinator_service/urls.py`| URL routing table | REVIEWED | No findings |
| `coordinator_service/celery.py`| Celery app and beat schedule | REVIEWED | No findings |
| `coordinator_service/asgi.py`, `wsgi.py`| ASGI / WSGI entrypoints | REVIEWED | No findings |
| `coordinator/tests/*.py` (11 files)| Unit & integration test suites | REVIEWED | No findings |
| `coordinator/migrations/*.py` (0001-0014)| Schema migrations | REVIEWED | Excluded (Django generated) |

---

## 2. Inventory Service (`inventory-service/`)

| File Path | Entity / Symbol / Endpoint | Status | Finding IDs |
| :--- | :--- | :---: | :--- |
| `inventory/views.py` | `event_list` (GET /events/) | REVIEWED | No findings |
| `inventory/views.py` | `seat_list` (GET /seats/) | REVIEWED | **PERF-01, PERF-02** |
| `inventory/views.py` | `seat_pricing` (POST /seats/pricing/) | REVIEWED | No findings |
| `inventory/views.py` | `reserve_seat` (POST /seats/reserve/) | REVIEWED | **CONC-01** |
| `inventory/views.py` | `confirm_seat` (POST /seats/confirm/) | REVIEWED | SEC-04, SEC-08 |
| `inventory/views.py` | `release_seat` (POST /seats/release/) | REVIEWED | SEC-04, SEC-08 |
| `inventory/views.py` | `check_reservation` (GET /seats/check/) | REVIEWED | No findings |
| `inventory/views.py` | `reset_seats` (POST /seats/reset/) | REVIEWED | SEC-08 |
| `inventory/views.py` | `health_check` (GET /health/) | REVIEWED | No findings |
| `inventory/views.py` | `_verify_internal_service_key()` | REVIEWED | **SEC-04, SEC-08** |
| `inventory/views.py` | `reclaim_expired_holds()` | REVIEWED | **PERF-01** |
| `inventory/models.py` | `Event`, `Seat`, `Reservation` | REVIEWED | No findings |
| `inventory/serializers.py` | `EventSerializer`, `SeatSerializer` | REVIEWED | No findings |
| `inventory/middleware.py` | `CorrelationIdMiddleware` | REVIEWED | No findings |
| `inventory/tasks.py` | `sweep_expired_holds_task()` | REVIEWED | No findings |
| `inventory_service/settings.py`| Django Settings Configuration | REVIEWED | SEC-06 |
| `inventory_service/urls.py`| URL routing table | REVIEWED | No findings |
| `inventory/tests/*.py` (2 files)| Test suites | REVIEWED | No findings |
| `inventory/migrations/*.py` (0001-0014)| Schema migrations | REVIEWED | Excluded (Django generated) |

---

## 3. Payment Service (`payment-service/`)

| File Path | Entity / Symbol / Endpoint | Status | Finding IDs |
| :--- | :--- | :---: | :--- |
| `payments/views.py` | `charge_payment` (POST /payments/charge/) | REVIEWED | **SEC-03** |
| `payments/views.py` | `payment_check` (GET /payments/check/) | REVIEWED | **SEC-03** |
| `payments/views.py` | `refund_payment` (POST /payments/refund/) | REVIEWED | **SEC-03** |
| `payments/views.py` | `health_check` (GET /health/) | REVIEWED | No findings |
| `payments/models.py` | `Payment` Model | REVIEWED | No findings |
| `payments/middleware.py` | `CorrelationIdMiddleware` | REVIEWED | No findings |
| `payment_service/settings.py` | Django Settings Configuration | REVIEWED | SEC-03, SEC-06 |
| `payment_service/urls.py` | URL routing table | REVIEWED | No findings |
| `payments/tests/*.py` (1 file) | Test suite | REVIEWED | No findings |
| `payments/migrations/*.py` (0001-0009)| Schema migrations | REVIEWED | Excluded (Django generated) |

---

## 4. Infrastructure & Configuration

| File Path | Entity / Component | Status | Finding IDs |
| :--- | :--- | :---: | :--- |
| `docker-compose.yml` | Container network & port bindings | REVIEWED | **SEC-03, SEC-06, INFRA-01** |
| `coordinator-service/Dockerfile` | Python 3.13 image, root user | REVIEWED | **INFRA-02** |
| `inventory-service/Dockerfile` | Python 3.13 image, root user | REVIEWED | **INFRA-02** |
| `payment-service/Dockerfile` | Python 3.13 image, root user | REVIEWED | **INFRA-02** |
| `requirements.txt` | Package dependencies | REVIEWED | **DEP-01** |
| `scripts/concurrency_tester.py` | Load testing utility | REVIEWED | No findings |
| `visualizer/` (app.js, booking.js, etc.) | Frontend UI client | REVIEWED | No findings |
