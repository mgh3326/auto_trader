# Refactor MCP Screening Common Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split `app/mcp_server/tooling/screening/common.py` by responsibility while preserving all MCP screening contracts.

**Architecture:** Keep backwards-compatible imports from `common.py` initially. Move request normalization, market-cap cache, filter application, and response building into focused modules with re-exports to avoid broad callsite churn.

**Tech Stack:** Python 3.13, MCP tooling, pytest, Ruff.

---

## Cleanup constraints
- MCP public response keys, warnings, and defaults must not change.
- Keep `common.py` re-export compatibility until all callsites/tests are green.
- Do not alter `screening/us.py`, `screening/kr.py`, or `screening/crypto.py` behavior beyond import paths.
- Tests first for any moved helper not already directly covered.

## Evidence / target seams
- `app/mcp_server/tooling/screening/common.py` is 1035 lines.
- `MarketCapCache` spans roughly lines 204-307.
- `normalize_screen_request()` spans roughly lines 456-623.
- `_validate_screen_filters()` spans roughly lines 631-685.
- `_apply_basic_filters()` spans roughly lines 688-771.
- `_build_screen_response()` spans roughly lines 846-889.

### Task 1: Baseline screening tests
**Files:**
- Read: `app/mcp_server/tooling/screening/common.py`
- Test: `tests/test_screening_common.py`, relevant `tests/test_mcp_screen_stocks_*`

1. Run: `uv run pytest tests/test_screening_common.py -q`.
2. Run a small MCP screening subset if baseline passes.
3. Add characterization tests for helper clusters only if missing.

### Task 2: Extract request normalization
**Files:**
- Create: `app/mcp_server/tooling/screening/request_normalization.py`
- Modify: `app/mcp_server/tooling/screening/common.py`
- Test: `tests/test_screening_common.py`

1. Move normalization helpers required by `normalize_screen_request()`.
2. Re-export from `common.py`.
3. Run `tests/test_screening_common.py`.

### Task 3: Extract market-cap cache
**Files:**
- Create: `app/mcp_server/tooling/screening/market_cap_cache.py`
- Modify: `common.py`
- Test: screening common tests

1. Move `MarketCapCache` and direct support helpers.
2. Preserve constructor/default behavior.
3. Run targeted tests.

### Task 4: Extract filters and response builder
**Files:**
- Create: `app/mcp_server/tooling/screening/filters.py`
- Create: `app/mcp_server/tooling/screening/response.py`
- Modify: `common.py`
- Test: screening tests

1. Move `_validate_screen_filters()` and `_apply_basic_filters()` into `filters.py`.
2. Move `_build_screen_response()` and diagnostics helpers into `response.py` if dependency graph is small.
3. Re-export from `common.py`.
4. Run MCP screening subsets.

### Task 5: Verify and commit
Run:
```bash
uv run pytest tests/test_screening_common.py -q
uv run pytest tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_screen_stocks_crypto.py -q
uv run ruff check app/mcp_server/tooling/screening tests/test_screening_common.py
```
Commit using Lore protocol. Mark `Scope-risk: moderate` because MCP contracts are public.
