# Sonar Pass 2 Priority Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce the highest-value SonarCloud backlog first by hardening the MCP batch summary path, clearing the safest FastAPI blocker sweep, and only touching adjacent low-cost blockers when direct evidence justifies it.

**Architecture:** Keep the early pass narrow and contract-preserving. Start with the smallest runtime patch in `analysis_tool_handlers.py` under TDD, then do the mechanical `Annotated[...]` router migration only in the low-risk files whose HTTP contract is already stable. Treat anything security-sensitive or weakly tested as a separate later batch instead of folding it into the first sweep.

**Tech Stack:** Python 3.13+, FastAPI, SQLAlchemy, MCP tooling, pytest, Ruff, ty

---

## Task 1: Harden MCP quick summaries against non-sequence support/resistance payloads

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py:444`
- Test: `tests/test_mcp_fundamentals_tools.py`

**Step 1: Write the failing regression test**

Extend the existing `analyze_stock_batch` quick-summary tests with a case where `support_resistance.supports` and `support_resistance.resistances` are truthy but not sliceable sequences.

```python
async def test_analyze_stock_batch_quick_summary_handles_non_sequence_support_levels(
    self, monkeypatch
):
    tools = build_tools()

    mock_analysis = {
        "symbol": "005930",
        "market_type": "equity_kr",
        "source": "kis",
        "quote": {"price": 75000},
        "support_resistance": {
            "supports": {"price": 73000},
            "resistances": "77000",
        },
    }

    async def fake_impl(symbol: str, market: str | None, include_peers: bool):
        return mock_analysis

    _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", fake_impl)

    result = await tools["analyze_stock_batch"](["005930"], market="kr")

    assert result["results"]["005930"]["supports"] == []
    assert result["results"]["005930"]["resistances"] == []
```

**Step 2: Run the targeted RED slice**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py -k "quick_summary" -q`
Expected: FAIL with a `TypeError` or incorrect summary output because `_summarize_analysis_result()` assumes truthy values are sliceable sequences.

**Step 3: Implement the minimal production fix**

- Add a tiny helper in `app/mcp_server/tooling/analysis_tool_handlers.py` that returns the first three items only when the raw value is a list or tuple.
- Preserve the public summary keys and existing behavior for valid list inputs.
- Normalize all other shapes (`None`, dict, string, scalar) to `[]` instead of raising.

```python
def _take_level_preview(raw_levels: Any) -> list[Any]:
    if isinstance(raw_levels, (list, tuple)):
        return list(raw_levels[:3])
    return []
```

**Step 4: Re-run the targeted slice**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py -k "quick_summary" -q`
Expected: PASS.

**Step 5: Run the adjacent MCP fundamentals slice**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py -q`
Expected: PASS.

**Step 6: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py tests/test_mcp_fundamentals_tools.py
git commit -m "fix: harden batch analysis support summaries"
```

---

## Task 2: Migrate the low-risk router batch to `Annotated[...]`

**Files:**
- Modify: `app/routers/manual_holdings.py`
- Modify: `app/routers/stock_latest.py`
- Modify: `app/routers/analysis_json.py`
- Test: `tests/test_routers.py`
- Test: create or extend a thin OpenAPI/router smoke test if the existing slices do not cover startup

**Step 1: Write the failing router startup regression**

Add a startup/OpenAPI smoke test that imports the app and asserts the migrated routes still register after the signature changes.

```python
def test_analysis_and_manual_holding_routes_still_register_in_openapi() -> None:
    from app.main import create_app

    app = create_app()
    schema = app.openapi()
    paths = schema["paths"]

    assert "/analysis-json/api/results" in paths
    assert "/stock-latest/api/latest-results" in paths
    assert "/api/manual-holdings/api/holdings" in paths
```

**Step 2: Run the targeted RED slice**

Run: `uv run pytest --no-cov tests/test_main_sentry.py tests/test_routers.py -k "openapi or manual_holdings" -q`
Expected: FAIL only after you make one route signature change incorrectly during the migration; use this slice as the guardrail while converting parameters file-by-file.

**Step 3: Implement the minimal signature migration**

- Import `Annotated` from `typing` in each target file.
- Convert `Depends(...)` and `Query(...)` parameters to `Annotated[...]` style.
- Keep defaults on the `=` side, not inside `Query(...)`.
- Preserve route names, decorators, response payloads, and validation bounds exactly.

```python
db: Annotated[AsyncSession, Depends(get_db)]
page: Annotated[int, Query(ge=1, description="페이지 번호")] = 1
page_size: Annotated[int, Query(ge=1, le=100, description="페이지 크기")] = 20
```

**Step 4: Re-run the router slice**

Run: `uv run pytest --no-cov tests/test_main_sentry.py tests/test_routers.py tests/test_screener_router.py -k "openapi or manual_holdings or limit_over_100" -q`
Expected: PASS.

**Step 5: Run focused diagnostics and type/lint checks**

Run: `uv run ruff check app/routers/manual_holdings.py app/routers/stock_latest.py app/routers/analysis_json.py tests/test_routers.py tests/test_main_sentry.py`
Expected: PASS.

Run: `uv run ty check app/routers/manual_holdings.py app/routers/stock_latest.py app/routers/analysis_json.py`
Expected: PASS.

**Step 6: Commit**

```bash
git add app/routers/manual_holdings.py app/routers/stock_latest.py app/routers/analysis_json.py tests/test_routers.py tests/test_main_sentry.py
git commit -m "refactor: adopt annotated router dependencies"
```

---

## Task 3: Triage the isolated `tests/test_services_krx.py` blocker before changing it

**Files:**
- Inspect: `tests/test_services_krx.py:918`
- Modify only if direct inspection proves a real issue

**Step 1: Identify the exact Sonar rule / behavior problem**

Use direct inspection and the local test context to determine whether the line-918 issue is:
- a real behavior bug,
- a mechanical cleanup, or
- a false-positive / non-priority test-only smell.

**Step 2: Only if the issue is real, write the failing test first**

Keep the test local to `tests/test_services_krx.py` and avoid broad test refactors.

**Step 3: Implement the minimal cleanup**

Change only the smallest line block needed to clear the blocker without widening the test cleanup scope.

**Step 4: Re-run the focused KRX slice**

Run: `uv run pytest --no-cov tests/test_services_krx.py -k "valuation_cache_storage" -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_services_krx.py
git commit -m "test: resolve krx cache blocker"
```

---

## Task 4: Verify the pass and document the deferred batches

**Files:**
- Modify: `docs/plans/2026-03-14-sonar-pass2-priority-remediation-implementation-plan.md` only if notes need updating after execution

**Step 1: Run the complete verification set for touched files**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py tests/test_routers.py tests/test_main_sentry.py tests/test_screener_router.py tests/test_services_krx.py -q`
Expected: PASS.

Run: `uv run ruff check app/mcp_server/tooling/analysis_tool_handlers.py app/routers/manual_holdings.py app/routers/stock_latest.py app/routers/analysis_json.py tests/test_mcp_fundamentals_tools.py tests/test_routers.py tests/test_main_sentry.py tests/test_services_krx.py`
Expected: PASS.

Run: `uv run ty check app/mcp_server/tooling/analysis_tool_handlers.py app/routers/manual_holdings.py app/routers/stock_latest.py app/routers/analysis_json.py`
Expected: PASS.

**Step 2: Note the explicitly deferred batches**

Leave the following for follow-up batches, not this pass:
- `app/services/kis_websocket.py`
- `app/analysis/model_executor.py`
- `app/mcp_server/tooling/analysis_screen_core.py`

Execution note after search-mode triage:
- `app/routers/screener.py` proved to be a safe mechanical migration and was completed independently once its regression guard was in place.
- `app/routers/symbol_settings.py` required a separate root-cause pass because the router mixed old-style dependency signatures with pre-existing LSP type noise from legacy analysis model access. The fix stayed narrow by migrating the signatures and aligning `calculate_estimated_order_cost()` typing with the actual payload shape.

**Step 3: Commit**

```bash
git add docs/plans/2026-03-14-sonar-pass2-priority-remediation-implementation-plan.md
git commit -m "docs: capture sonar pass 2 implementation plan"
```
