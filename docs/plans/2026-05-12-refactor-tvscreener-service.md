# Refactor TvScreener Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce `app/services/tvscreener_service.py` size by extracting retry and capability concerns without changing screener query behavior.

**Architecture:** Keep `TvScreenerService` public API stable. Extract retry execution and capability registry/probing helpers only when tests lock current exception and cache behavior. Do not change tvscreener package usage or query parameters.

**Tech Stack:** Python 3.13, pandas, tvscreener, pytest/pytest-asyncio.

---

## Cleanup constraints
- Behavior-preserving refactor only.
- Do not change sort/filter semantics used by MCP screening.
- Keep public class and method names stable.
- Tests before moving retry/capability code.

## Evidence / target seams
- `app/services/tvscreener_service.py` is 1028 lines.
- `TvScreenerService` spans roughly lines 265-998.
- `fetch_with_retry()` spans roughly lines 314-466.
- `discover_fields()` spans roughly lines 468-552.
- `_probe_stock_capability()` spans roughly lines 602-683.
- `get_stock_capabilities()` spans roughly lines 685-765.
- `query_crypto_screener()` / `query_stock_screener()` occupy the query execution surface.

### Task 1: Baseline tvscreener tests
**Files:**
- Read: `app/services/tvscreener_service.py`
- Test: `tests/test_mcp_screen_stocks_tvscreener_contract.py`, any `tests/*tvscreener*`

1. Run: `uv run pytest tests/test_mcp_screen_stocks_tvscreener_contract.py -q`.
2. Locate/extend tests for retry exception mapping if missing.
3. Locate/extend tests for capability cache/probe behavior if missing.

### Task 2: Extract retry executor
**Files:**
- Create: `app/services/tvscreener_retry.py`
- Modify: `app/services/tvscreener_service.py`
- Test: targeted tvscreener tests

1. Write/confirm tests for success after retry, timeout, malformed request, and final failure.
2. Move retry loop behind a small helper used by `TvScreenerService.fetch_with_retry()`.
3. Keep method signature unchanged.
4. Run targeted tests.

### Task 3: Extract capability support
**Files:**
- Create: `app/services/tvscreener_capabilities.py`
- Modify: `app/services/tvscreener_service.py`
- Test: targeted tests

1. Move capability dataclasses/registry/probe helper if dependency graph is clean.
2. Preserve `_field_cache` and shared registry behavior.
3. Run targeted tests.

### Task 4: Keep query methods thin
**Files:**
- Modify: `app/services/tvscreener_service.py`

1. Remove dead local helpers/imports.
2. Do not split query methods unless tests show a clear pure helper seam.
3. Run Ruff.

### Task 5: Verify and commit
Run:
```bash
uv run pytest tests/test_mcp_screen_stocks_tvscreener_contract.py -q
uv run ruff check app/services/tvscreener_service.py app/services/tvscreener_*.py tests/test_mcp_screen_stocks_tvscreener_contract.py
```
Commit using Lore protocol.
