# ROB-535 Toss Stock Warnings Guard and Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Korea stock warnings check, pre-trade order execution guard, MCP preview/mutation safety integration, database schema, and daily sync job using Toss OpenAPI.

---

## 1. File Structure Changes

- **Create**: `app/models/kr_stock_warnings.py`
  - DB model `KRStockWarning` representing the `kr_stock_warnings` table.
- **Modify**: `app/models/__init__.py`
  - Register and export `KRStockWarning`.
- **Modify**: `app/services/brokers/toss/dto.py`
  - Add `TossWarningInfo` dataclass and `parse_warnings` parser.
- **Modify**: `app/services/brokers/toss/client.py`
  - Implement `warnings(symbol: str) -> list[TossWarningInfo]` returning parsed warnings.
- **Create**: `app/services/brokers/toss/warnings_guard.py`
  - Implement `check_warnings_guard` function with timeout, fail-open, and blocking policy for `LIQUIDATION_TRADING`.
- **Modify**: `app/mcp_server/tooling/orders_toss_variants.py`
  - Wire warnings guard into `toss_preview_order` (show active warnings) and `toss_place_order` (block on LIQUIDATION_TRADING).
- **Modify**: `app/mcp_server/tooling/orders_kis_variants.py`
  - Wire the same Toss warnings guard into KIS live KR buy preview/mutation paths.
- **Create**: `app/services/toss_warnings_sync_service.py`
  - Sync warnings for KR stocks using per-symbol replace semantic.
- **Create**: `app/jobs/toss_warnings.py`
  - Job runner wrapper for the sync service.
- **Create**: `app/tasks/toss_warnings_sync_tasks.py`
  - TaskIQ background task definitions and schedule.
- **Create**: `scripts/sync_toss_warnings.py`
  - CLI script to trigger warnings sync.
- **Modify**: `Makefile`
  - Add a target `sync-toss-warnings` to run the sync script.

---

## 2. Implementation Steps

### Task 1: Database Model and Alembic Migration
- [x] **Step 1.1**: Create `app/models/kr_stock_warnings.py` with:
  - Table name: `kr_stock_warnings`
  - Columns: `id` (BigInt PK), `market` (String 10), `symbol` (String 10), `warning_type` (String 50), `exchange` (String 20, nullable), `start_date` (Date, nullable), `end_date` (Date, nullable), `source` (String 32, default 'toss_openapi'), `fetched_at` (DateTime TZ-aware).
  - Index: `ix_kr_stock_warnings_market_symbol` on `(market, symbol)`.
- [x] **Step 1.2**: Update `app/models/__init__.py` to import and expose `KRStockWarning`.
- [x] **Step 1.3**: Run Alembic migration generation:
  ```bash
  uv run alembic revision --autogenerate -m "add kr_stock_warnings table"
  ```
- [x] **Step 1.4**: Review the generated migration file for correctness and apply it:
  ```bash
  uv run alembic upgrade head
  ```

### Task 2: Toss Client DTO & Warnings Method Update
- [x] **Step 2.1**: In `app/services/brokers/toss/dto.py`, add `TossWarningInfo` dataclass and `parse_warnings` function.
- [x] **Step 2.2**: In `app/services/brokers/toss/client.py`, update `warnings` method:
  ```python
  async def warnings(self, symbol: str) -> list[TossWarningInfo]:
      return parse_warnings(
          await self._request(
              "GET",
              f"/api/v1/stocks/{symbol}/warnings",
              group=TossApiGroup.STOCK,
          )
      )
  ```
- [x] **Step 2.3**: Add unit tests in `tests/services/brokers/toss/test_dto.py` verifying parsing of warnings with different warning types.
- [x] **Step 2.4**: Run pytest for Toss client to verify changes:
  ```bash
  uv run pytest tests/services/brokers/toss -q
  ```

### Task 3: Implement Warnings Order Guard
- [x] **Step 3.1**: Create `app/services/brokers/toss/warnings_guard.py` with `check_warnings_guard` function:
  - Check if target symbol is KR (e.g. 6 numeric digits). If not, immediately return success (`ok=True`).
  - Request Toss warnings API with a 3.0 second timeout (`asyncio.wait_for`).
  - Fail-open: If the API request times out or raises an error, log the error and allow the order (`ok=True`, log warnings check failure).
  - Blocking Policy: Check if warning list contains `LIQUIDATION_TRADING`. If yes, block order (`ok=False`).
- [x] **Step 3.2**: Write unit tests for `warnings_guard.py` in `tests/services/brokers/toss/test_warnings_guard.py` covering success, failure, timeout, and blocking cases.

### Task 4: Integrate Warnings Guard in MCP Tools
- [x] **Step 4.1**: In `app/mcp_server/tooling/orders_toss_variants.py`, update `toss_preview_order` to:
  - Fetch warnings using `client.warnings(symbol)` inside client context.
  - Expose active warning types in the return dictionary under `warnings` (e.g. `[w.warning_type for w in warnings]`).
- [x] **Step 4.2**: In `app/mcp_server/tooling/orders_toss_variants.py`, update `toss_place_order`:
  - Before sending mutations to Toss, check `check_warnings_guard`.
  - If `ok=False`, return a structured response with `"success": False`, `**base_response`, and `"error": guard_result.error_message`.
- [x] **Step 4.3**: Write tests in `tests/test_mcp_toss_order_variants.py` to verify warnings list in preview and blocking behavior on place order (consolidated with existing order variants tests).

### Task 5: Warnings Sync Service and Scheduler Job
- [x] **Step 5.1**: Create `app/services/toss_warnings_sync_service.py` implementing `sync_toss_warnings(db, client, market, symbols)`:
  - If symbols are not provided, resolve only scoped symbols from Toss holdings, active manual holdings, and active watch alerts. Do not poll the full KR/US symbol universe by default; the Toss warnings API is rate-limited and the default job must stay bounded.
  - If symbols are provided explicitly, sync exactly those normalized symbols.
  - Loop through symbols: delete existing warnings matching `(market, symbol)` and insert new warnings.
  - Batch commit to database.
- [x] **Step 5.2**: Create `app/jobs/toss_warnings.py` to wrap the sync service with database session and `TossReadClient` contexts.
- [x] **Step 5.3**: Create `app/tasks/toss_warnings_sync_tasks.py` defining task `sync_toss_warnings_task` with schedule `30 7 * * *` (KST).
- [x] **Step 5.4**: Create `scripts/sync_toss_warnings.py` to expose warnings sync to the command line.
- [x] **Step 5.5**: Add `sync-toss-warnings` target to the `Makefile`.
- [x] **Step 5.6**: Write tests in `tests/test_toss_warnings_sync.py` to verify the sync service behaves correctly (e.g., replaces per-symbol warnings).


---

## 3. Verification

Run the focused checks plus full lint/test before PR:
```bash
make lint
uv run pytest tests/services/brokers/toss tests/test_mcp_toss_order_variants.py tests/test_mcp_kis_order_variants.py tests/test_toss_warnings_sync.py -q
uv run alembic heads
make test
```
