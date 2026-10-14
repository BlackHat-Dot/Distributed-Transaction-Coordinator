# Function and Helper Rename Map for `coordinator-service`

This document details all internal renamings applied to simplify `coordinator/views.py`. All external endpoints mapped in `urls.py` and model fields are strictly preserved. In accordance with zero-alias rules, all callers, imports, and test patch targets across `coordinator-service` were updated simultaneously with zero shim or alias lines retained.

| Original Identifier | New Identifier | Type | Scope & Files Touched |
| :--- | :--- | :--- | :--- |
| `_get_active_correlation_id` | `_corr_id` | Internal helper | `views.py`, `tests/test_audit_hardening.py` |
| `_fault_injection_allowed` | `_faults_on` | Internal helper | `views.py`, `tests/test_b4_fault_injection_and_secrets.py` |
| `_request_payload_verify` | `_check_body` | Internal helper | `views.py`, `tests/test_audit_hardening.py` |
| `_resolve_seat_pricing` | `_get_prices` | Internal helper | `views.py`, `tests/test_a7_reserve_order.py`, `tests/test_coverage_boost.py` |
| `_handle_create_transaction` | `_create_txn` | Controller handler | `views.py`, `tests/test_audit_hardening.py` |
| `_handle_list_transactions` | `_list_txns` | Controller handler | `views.py` |
| `_run_saga_pipeline` | `_run_saga` | Saga orchestrator | `views.py`, `tasks.py`, `tests/test_a3_concurrency.py`, `tests/test_coverage_boost.py` |
| `_reserve_inventory` | `_reserve` | Microservice call | `views.py`, `tests/test_a6_request_exceptions.py`, `tests/test_a7_reserve_order.py` |
| `_confirm_inventory` | `_confirm` | Microservice call | `views.py`, `tests/test_a2_cas_checks.py`, `tests/test_a6_request_exceptions.py`, `tests/test_a8_cleanup.py`, `tests/test_audit_hardening.py`, `tests/test_exit_gate_gaps.py` |
| `_charge_payment` | `_charge` | Microservice call | `views.py`, `tests/test_a3_concurrency.py`, `tests/test_a5_payment_5xx.py`, `tests/test_a6_request_exceptions.py`, `tests/test_audit_hardening.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py`, `tests/test_f1_checkout_presence.py`, `tests/test_g8_coordinator_checkout_presence_resilience.py` |
| `_do_charge_payment` | `_do_charge` | Internal call | `views.py` |
| `_finalize_payment_outcome` | `_finish_payment` | Internal pipeline | `views.py`, `tests/test_a2_cas_checks.py`, `tests/test_a8_cleanup.py`, `tests/test_audit_hardening.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py` |
| `_get_requester_identity` | `_get_caller` | Internal helper | `views.py`, `tests/test_audit_hardening.py`, `tests/test_b1_ownership.py`, `tests/test_b2_staff_bypass.py`, `tests/test_b3_admin_domain.py`, `tests/test_characterization.py`, `tests/test_coverage_boost.py`, `tests/test_exit_gate_gaps.py` |
| `_serialize_transaction` | `_txn_json` | Serializer helper | `views.py`, `tests/test_audit_hardening.py` |
| `_serialize_event` | `_event_json` | Serializer helper | `views.py`, `tests/test_coverage_boost.py` |
| `_transport_status` | `_err_status` | Exception helper | `views.py` |

### Local Variable Concept Unification
Across `coordinator/views.py`, local variables are standardized to plain, consistent identifiers:
- `txn`: Transaction model instance (replacing `transaction_obj`, `tx_record`, `transaction`).
- `tx_id`: UUID or string transaction ID (replacing `raw_tid`, `pay_tid`, `release_transaction_id`).
- `body`: Parsed JSON response/request dictionary (replacing `resp_json`, `response_data`, `res_body`).
- `status`: HTTP or saga status code/string (replacing `payment_status_code`, `status_code_int`).
