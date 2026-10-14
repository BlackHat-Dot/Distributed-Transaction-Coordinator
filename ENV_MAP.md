# Environment Variable Mapping for Transaction Coordinator Views (`coordinator/views.py`)

This document catalogs every environment variable read by the coordinator views module, its location, default, purpose, and simplification resolution.

| Env Var | Original Location | Default | What It Controls | Action & Rationale |
| :--- | :--- | :--- | :--- | :--- |
| `INVENTORY_SERVICE_URL` | Line 61 | `"http://127.0.0.1:8000"` | Base URL for downstream Inventory service | **Kept**: Read once at top block. |
| `PAYMENT_SERVICE_URL` | Line 62 | `"http://127.0.0.1:8001"` | Base URL for downstream Payment service | **Kept**: Read once at top block. |
| `REQUEST_CONNECT_TIMEOUT` | Line 73 | `2.0` | Connection timeout (seconds) for outbound HTTP calls | **Kept**: Read once at top block as `CONNECT_TIMEOUT`. |
| `REQUEST_READ_TIMEOUT` | Line 74 | `5.0` | Read timeout (seconds) for outbound HTTP calls | **Kept**: Removed redundant fallback to `REQUEST_TIMEOUT` env var. Used to construct `REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)`. |
| `INTERNAL_SERVICE_KEY` | Line 76 | `settings.INTERNAL_SERVICE_KEY` | Shared secret for inter-service communication (`X-Internal-Service-Key`) | **Merged**: `settings.py` already defines and validates this with security checks; read directly from `settings.INTERNAL_SERVICE_KEY`. |
| `STAFF_API_KEY` | Line 86 | `""` | Secret key for administrative staff access (`X-Staff-Key`) | **Kept**: Distinct from `INTERNAL_SERVICE_KEY`. Kept as env var read in top block. |
| `ENABLE_FAULT_INJECTION` | Line 102 | `False` | Toggles test fault injection (coordinator crash, compensation failure) | **Merged**: Evaluated via single helper `_faults_on()` respecting `settings.DEBUG` and `ENABLE_FAULT_INJECTION`. |
| `RECOVERY_MIN_THRESHOLD_SECONDS` | Line 552 | `30` | Minimum age (seconds) before stuck transactions are eligible for sweep | **Kept**: Read once at top block as `RECOVERY_MIN_THRESHOLD`. |
| `MAX_SEATS_PER_TRANSACTION` | Lines 832, 868 | `10` | Maximum seats allowed per booking transaction | **Deduplicated**: Read once at top block as `MAX_SEATS` instead of duplicate reads. |
| `ADMIN_DOMAIN` | Line 2297 | `""` | Email domain prefix/suffix treated as staff (e.g. `@dtc.internal`) | **Kept**: Read once at top block as `ADMIN_DOMAIN`. |
| `STAFF_USER_ID` | Line 2417 | `"system_staff"` | Default user ID assigned when caller authenticates via staff key | **Kept**: Read once at top block as `DEFAULT_STAFF_USER_ID`. |
| `STAFF_EMAIL_ID` | Line 2418 | `"admin@dtc.internal"` | Default email assigned when caller authenticates via staff key | **Kept**: Read once at top block as `DEFAULT_STAFF_EMAIL`. |
| `ALLOW_DEV_STAFF_BYPASS` | Line 2445 | `False` | Permits local dev unauthenticated staff bypass | **Merged**: `settings.py` already defines and validates this; read directly from `settings.ALLOW_DEV_STAFF_BYPASS`. |
| `CELERY_BROKER_URL` | Line 251 | `"redis://127.0.0.1:6379/0"` | Redis broker URL for SSE event streaming & checkout presence | **Merged**: `settings.py` already defines this; read directly from `settings.CELERY_BROKER_URL`. |

---

### Key Overlap Fixes
1. **REQUEST_TIMEOUT Overlap**: Separated Python tuple constant `REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)` from env names. Removed the env fallback `os.getenv("REQUEST_TIMEOUT")` inside `REQUEST_READ_TIMEOUT`.
2. **Settings vs Env Duplication**: `INTERNAL_SERVICE_KEY`, `ALLOW_DEV_STAFF_BYPASS`, and `CELERY_BROKER_URL` now read strictly from `django.conf.settings` which already handles env parsing and production validation.
3. **MAX_SEATS_PER_TRANSACTION Duplication**: Consolidated the two inline `os.getenv("MAX_SEATS_PER_TRANSACTION", "10")` calls into a single constant `MAX_SEATS`.
4. **Key Separation**: Explicitly separated and commented `INTERNAL_SERVICE_KEY` (service-to-service internal auth) and `STAFF_API_KEY` (human operator/admin API auth).
