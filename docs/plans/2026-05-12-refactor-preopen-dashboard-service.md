# Refactor Preopen Dashboard Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split `app/services/preopen_dashboard_service.py` into smaller behavior-preserving modules without changing router/service contracts.

**Architecture:** Keep `get_latest_preopen_dashboard()` and public schema usage unchanged. Extract pure artifact-building helpers first, then QA/execution-review builders, leaving async data orchestration in the original service. No new dependencies, no DB/schema/API changes.

**Tech Stack:** Python 3.13, Pydantic schemas, pytest/pytest-asyncio, Ruff/ty.

---

## Cleanup constraints
- Behavior-preserving refactor only.
- Add/adjust regression tests before moving each helper cluster.
- Do not change `app/routers/preopen.py` contracts unless a failing test proves an import-only adjustment is required.
- Prefer module-level pure functions over new classes.

## Evidence / target seams
- `app/services/preopen_dashboard_service.py` is 1374 lines.
- `_build_briefing_artifact()` spans roughly lines 443-651 and builds a transport artifact from loaded data.
- `_build_qa_evaluator_summary()` spans roughly lines 710-993.
- `_build_execution_review()` spans roughly lines 996-1216.
- `get_latest_preopen_dashboard()` spans roughly lines 1219-1374 and should remain the orchestration entrypoint.

### Task 1: Baseline and characterization tests
**Files:**
- Read: `app/services/preopen_dashboard_service.py`
- Test: `tests/test_preopen_dashboard_service.py`

1. Run: `uv run pytest tests/test_preopen_dashboard_service.py -q`
2. Add focused characterization tests for the first helper cluster if current coverage does not assert output shape.
3. Run the new/changed test and confirm RED if introducing a new import seam.
4. Implement only enough test fixture wiring to cover existing behavior.

### Task 2: Extract briefing artifact helpers
**Files:**
- Create: `app/services/preopen_dashboard_artifacts.py`
- Modify: `app/services/preopen_dashboard_service.py`
- Test: `tests/test_preopen_dashboard_service.py` or new `tests/services/test_preopen_dashboard_artifacts.py`

1. Move `_build_briefing_artifact()` and directly coupled pure helper(s) only.
2. Keep function signatures stable.
3. Import from the original service.
4. Run targeted tests.

### Task 3: Extract QA evaluator summary
**Files:**
- Create/Modify: `app/services/preopen_dashboard_artifacts.py` or `app/services/preopen_dashboard_quality.py`
- Modify: `app/services/preopen_dashboard_service.py`
- Test: targeted preopen tests

1. Add characterization for summary edge cases.
2. Move `_score_qa_checks()` and `_build_qa_evaluator_summary()` together if they are tightly coupled.
3. Keep output models/keys identical.
4. Run targeted tests.

### Task 4: Extract execution review builder
**Files:**
- Create: `app/services/preopen_dashboard_execution_review.py` if separate from artifacts is clearer
- Modify: `app/services/preopen_dashboard_service.py`
- Test: targeted preopen tests

1. Characterize output for empty/missing fields.
2. Move `_build_execution_review()` plus minimal local helper set.
3. Keep async orchestration in `preopen_dashboard_service.py`.
4. Run targeted tests.

### Task 5: Verify and commit
Run:
```bash
uv run pytest tests/test_preopen_dashboard_service.py -q
uv run ruff check app/services/preopen_dashboard_service.py app/services/preopen_dashboard_*.py tests/test_preopen_dashboard_service.py
```
Commit using Lore protocol with `Confidence`, `Scope-risk`, `Tested`, and `Not-tested` trailers.
