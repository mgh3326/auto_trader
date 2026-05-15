# Refactor n8n Daily Brief Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split `app/services/n8n_daily_brief_service.py` into focused helpers while preserving `/api/n8n/daily-brief` response behavior.

**Architecture:** Keep `fetch_daily_brief()` as the public orchestration entrypoint. Extract portfolio summary and brief rendering helpers into focused modules already aligned with `app/services/n8n_formatting.py` patterns. No router contract changes.

**Tech Stack:** Python 3.13, FastAPI service layer, Pydantic n8n schemas, pytest.

---

## Cleanup constraints
- Behavior-preserving refactor only.
- Keep `app/routers/n8n.py` unchanged in this lane.
- Do not add new dependencies.
- Tests before each extraction seam.

## Evidence / target seams
- `app/services/n8n_daily_brief_service.py` is 1342 lines.
- `_build_portfolio_summary()` spans roughly lines 266-368.
- `_build_brief_text()` spans roughly lines 371-509.
- `fetch_daily_brief()` spans roughly lines 1197-1331 and should remain the public orchestrator.
- Formatting helpers have already moved into `app/services/n8n_formatting.py`, so avoid duplicating that work.

### Task 1: Baseline and coverage map
**Files:**
- Read: `app/services/n8n_daily_brief_service.py`
- Test: `tests/test_n8n_api.py` and any `tests/*n8n*daily*`

1. Run: `uv run pytest tests/test_n8n_api.py -q`.
2. Locate existing direct tests for `_build_portfolio_summary()` / `_build_brief_text()`.
3. Add characterization tests before moving any helper not directly covered.

### Task 2: Extract portfolio summary builder
**Files:**
- Create: `app/services/n8n_daily_brief_portfolio.py`
- Modify: `app/services/n8n_daily_brief_service.py`
- Test: new/updated service tests

1. Write focused tests for portfolio summary cases: KR/US/crypto grouping, zero cost, dust exclusion, gainers/losers.
2. Verify RED for new module import if applicable.
3. Move `_build_portfolio_summary()` and its minimal direct helper dependencies.
4. Run targeted tests.

### Task 3: Extract brief text builder
**Files:**
- Create: `app/services/n8n_daily_brief_rendering.py`
- Modify: `app/services/n8n_daily_brief_service.py`
- Test: service/router tests

1. Characterize Korean brief text structure with fixed input.
2. Move `_build_brief_text()` and only directly coupled pure helpers.
3. Keep external fetching and persistence in original service.
4. Run targeted tests.

### Task 4: Slim orchestration imports
**Files:**
- Modify: `app/services/n8n_daily_brief_service.py`

1. Confirm `fetch_daily_brief()` remains readable and owns IO orchestration.
2. Remove dead imports/helpers.
3. Run Ruff on touched files.

### Task 5: Verify and commit
Run:
```bash
uv run pytest tests/test_n8n_api.py -q
uv run ruff check app/services/n8n_daily_brief_service.py app/services/n8n_daily_brief_*.py tests/test_n8n_api.py
```
Commit using Lore protocol.
