# Function and Helper Rename Map for `coordinator/views.py`

This document details all internal renamings applied to simplify `coordinator/views.py`. All external endpoints mapped in `urls.py` and model fields are strictly preserved. To ensure 100% backward compatibility with external modules and test suites, backward-compatible aliases are preserved.

| Original Identifier | New Identifier | Type | Collision Check & Aliasing |
| :--- | :--- | :--- | :--- |
| `_get_active_correlation_id` | `_corr_id` | Internal helper | No collision. Alias preserved for callers. |
| `_fault_injection_allowed` | `_faults_on` | Internal helper | No collision. Alias preserved for tests. |
| `_request_payload_verify` | `_check_body` | Internal helper | No collision. Alias preserved. |
| `_resolve_seat_pricing` | `_get_prices` | Internal helper | No collision. Alias preserved for tests. |
| `_handle_create_transaction` | `_create_txn` | Controller handler | No collision. Called by `transactions_endpoint`. |
| `_create_transaction` | `_insert_txn` | Internal DB helper | No collision. Aliases `_create_transaction` and `_create_transaction_idempotent` preserved for tests. |
| `_handle_list_transactions` | `_list_txns` | Controller handler | No collision. Called by `list_transactions`. |
| `_run_saga_pipeline` | `_run_saga` | Internal pipeline | No collision. Alias preserved for `tasks.py` and tests. |
| `_reserve_inventory` | `_reserve` | Microservice call | No collision. Alias preserved for tests. |
| `_confirm_inventory` | `_confirm` | Microservice call | No collision. Alias preserved for tests. |
| `_charge_payment` | `_charge` | Microservice call | No collision. Alias preserved for tests. |
| `_do_charge_payment` | `_do_charge` | Internal call | No collision. Alias preserved. |
| `_finalize_payment_outcome` | `_finish_payment` | Internal pipeline | No collision. Alias preserved for tests. |
| `_get_requester_identity` | `_get_caller` | Internal helper | No collision. Alias preserved for tests. |
| `_serialize_transaction` | `_txn_json` | Serializer helper | No collision. Alias preserved for tests. |
| `_serialize_event` | `_event_json` | Serializer helper | No collision. Alias preserved for tests. |
| `_transport_status` | `_err_status` | Exception helper | No collision. Alias preserved. |

### Local Variable Concept Unification
Across the file, local variables are standardized to plain, consistent identifiers:
- `txn`: Transaction model instance (replacing `transaction_obj`, `tx_record`, `transaction`).
- `tx_id`: UUID or string transaction ID (replacing `raw_tid`, `pay_tid`, `release_transaction_id`, `transaction_id` where appropriate).
- `body`: Parsed JSON response/request dictionary (replacing `resp_json`, `response_data`, `res_body`).
- `status`: HTTP or saga status code/string (replacing `payment_status_code`, `status_code_int`).
