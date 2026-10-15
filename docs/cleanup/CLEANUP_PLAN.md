# DTC Codebase Cleanup Plan

This document details the target files, risk tiers, and specific refactoring actions planned across the repository to maximize readability, maintainability, and code hygiene with **zero behavior change**.

## Risk Tiers and Target Files

### Tier A: Primary Coordinator Controller (Full Refactor)
- **Target**: `coordinator-service/coordinator/views.py`
- **Actions**:
  - Unified env var block at top of file, one line per variable, one comment per line.
  - Dedicated `_release_inventory` helper deduplicating 3 copy-pasted blocks.
  - Clean `_compensate` catching only `RequestException`.
  - Remove `hasattr(requests.post, "mock_calls")` from `_http_post`/`_http_get`.
  - Canonical short function names without shim/alias lines: update all callers and test patch targets across all test files.
  - Standardized plain local variable names (`txn`, `tx_id`, `body`, `status`).
  - Streamlined `RequesterIdentity` tuple subclass.
  - Remove all bare string literals and long essay/diagram blocks; keep concise 1-2 line functional comments.

### Tier B: Other Coordinator Service Modules (Dead Code, Comments, Imports)
- **Targets**:
  - `coordinator-service/coordinator/auth_views.py`: Remove dead imports, clean verbose comments, streamline OTP/JWT auth flows.
  - `coordinator-service/coordinator/recovery.py`: Clean redundant imports, normalize logger usage and comments.
  - `coordinator-service/coordinator/saga_log.py`: Clean redundant imports, remove unused variables.
  - `coordinator-service/coordinator/middleware.py`: Clean correlation ID header extraction and logging comments.
  - `coordinator-service/coordinator/tasks.py`: Clean Celery task signatures, remove duplicate imports.
  - `coordinator-service/coordinator/models.py`: Clean docstrings and model comments.
  - `coordinator-service/coordinator/ticket_views.py`, `tickets.py`, `ticket_generator.py`: Remove unused imports, simplify gate validation helpers.
  - `coordinator-service/coordinator/management/commands/recovery_stuck_transactions.py`: Clean duplicate timeout parsing and comments.

### Tier C: Downstream Services (Inventory & Payment)
- **Inventory Service Targets**:
  - `inventory-service/inventory/views.py`: Clean redundant comments, simplify duplicate hold-release blocks, remove unused imports.
  - `inventory-service/inventory/services.py`: Clean redis connection helpers, comments.
  - `inventory-service/inventory/auth.py`: Clean auth headers verification and verbose comments.
  - `inventory-service/inventory/middleware.py`, `throttling.py`, `tasks.py`, `models.py`: Import optimization and comment cleanup.
- **Payment Service Targets**:
  - `payment-service/payments/views.py`: Clean redundant comments, simplify charge/void/refund logging, remove unused imports.
  - `payment-service/payments/permissions.py`: Clean internal key header check.
  - `payment-service/payments/models.py`, `middleware.py`: Clean comments and unused imports.

### Tier D: Scripts and Frontend
- **Scripts Targets**:
  - `scripts/run_real_flow_e2e.py`: Remove unused imports (`base64`, `sys`), remove unused variable `qr_b64`, fix non-placeholder f-string.
  - `scripts/start_services.py`: Organize imports.
  - `tests/test_gate_scanner.py`: Remove unused imports (`sys`, `socket`), organize imports.
  - `tests/test_nonblocking_load.py`: Remove unused import (`uuid`).
  - `tests/test_seat_concurrency_race.py`: Clean import block.
- **Frontend Targets**:
  - Inspect `visualizer/` and `gate-scanner/` for unreferenced junk files or dead scripts.

### Repository Hygiene & Linting
- Move root `ENV_MAP.md` and `RENAME_MAP.md` into `docs/cleanup/`.
- Ensure `.gitignore` properly excludes `venv`, `__pycache__`, `*.pyc`, `.env`, `logs`, `node_modules`, `.DS_Store`.
- Remove tracked junk files (e.g. `.coverage`, `celerybeat-schedule*`, untracked scratch/temporary artifacts).
- Run full `ruff check` sweep to resolve `F` (Pyflakes) findings.
