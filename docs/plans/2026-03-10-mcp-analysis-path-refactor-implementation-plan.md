# MCP Analysis Path Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP analysis screening/recommend internals를 facade-first로 분해하면서 공개 MCP 계약, warning 문자열, response shape, registration 동작, 그리고 기존 test patch surface를 유지한다.

**Architecture:** 공개 안정 경계는 `app/mcp_server/tooling/analysis_registration.py`와 `app/mcp_server/tooling/analysis_tool_handlers.py`에 고정한다. 내부 분해는 `app/mcp_server/tooling/analysis_screening.py`와 `app/mcp_server/tooling/analysis_screen_core.py`의 compatibility seam 뒤에서 진행하며, 순서는 crypto post-processing 추출 -> analyze 구현 이동 + alias 유지 -> recommend 단계 함수 분해 -> handler import cleanup -> test split이다.

**Tech Stack:** Python 3.13+, FastMCP, pytest, pandas, yfinance, tvscreener, Ruff, ty

---

### Task 1: `analysis_screening.py` facade surface를 먼저 고정

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `tests/test_mcp_screen_stocks.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`
- Modify: `tests/_mcp_tooling_support.py`

**Step 1: Write the failing tests**

`tests/test_mcp_screen_stocks.py`에 facade re-export를 고정하는 테스트를 추가한다.

```python
from app.mcp_server.tooling import analysis_screening


def test_analysis_screening_reexports_screen_contract_helpers() -> None:
    assert callable(analysis_screening.screen_stocks_unified)
    assert callable(analysis_screening._normalize_screen_market)
    assert callable(analysis_screening._normalize_asset_type)
    assert callable(analysis_screening._normalize_sort_by)
    assert callable(analysis_screening._normalize_sort_order)
    assert callable(analysis_screening._validate_screen_filters)
```

`tests/test_mcp_fundamentals_tools.py`에 handler가 screening facade를 경유하는지 확인하는 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_analyze_stock_tool_uses_analysis_screening_alias(monkeypatch):
    tools = build_tools()

    async def fake_impl(symbol: str, market: str | None, include_peers: bool):
        return {
            "symbol": symbol,
            "market_type": "equity_kr",
            "source": "shim-test",
            "include_peers": include_peers,
        }

    monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_impl)

    result = await tools["analyze_stock"]("005930", market="kr")

    assert result["source"] == "shim-test"
```

**Step 2: Run the targeted tests to verify failure**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks.py tests/test_mcp_fundamentals_tools.py -k "analysis_screening or shim" -q`  
Expected: FAIL because `analysis_screening.py` does not yet expose the full facade surface and handler bindings still bypass monkeypatched module attributes.

**Step 3: Implement the facade surface with minimal behavior change**

In `app/mcp_server/tooling/analysis_screening.py`:

- re-export `screen_stocks_unified`
- re-export normalization/validation helpers needed by handlers
- keep `_analyze_stock_impl` and `_recommend_stocks_impl` as stable top-level facade names

In `app/mcp_server/tooling/analysis_tool_handlers.py`:

- switch screening/analyze imports from symbol binding to module-based access
- keep public handler function names unchanged
- preserve parameter defaults and response shaping

Example pattern:

```python
from app.mcp_server.tooling import analysis_screening


normalized_market = analysis_screening._normalize_screen_market(market)
return await analysis_screening.screen_stocks_unified(...)
```

**Step 4: Update the shared patch helper only if needed**

If new shim symbols are introduced, update `tests/_mcp_tooling_support.py` so `_patch_runtime_attr()` can still find the same runtime seams.

**Step 5: Re-run the targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks.py tests/test_mcp_fundamentals_tools.py -k "analysis_screening or shim" -q`  
Expected: PASS

**Step 6: Commit**

```bash
git add app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py tests/test_mcp_screen_stocks.py tests/test_mcp_fundamentals_tools.py tests/_mcp_tooling_support.py
git commit -m "refactor: stabilize MCP analysis facades"
```

---

### Task 2: crypto 공통 후처리를 새 모듈로 추출

**Files:**
- Create: `app/mcp_server/tooling/analysis_screen_crypto.py`
- Modify: `app/mcp_server/tooling/analysis_screen_core.py`
- Modify: `tests/test_tvscreener_crypto.py`
- Modify: `tests/test_mcp_screen_stocks.py`
- Modify: `tests/test_crypto_composite_score.py`

**Step 1: Write the failing tests**

`tests/test_tvscreener_crypto.py`에 새 공통 finalizer를 직접 고정하는 테스트를 추가한다.

```python
from app.mcp_server.tooling.analysis_screen_crypto import finalize_crypto_screen


@pytest.mark.asyncio
async def test_finalize_crypto_screen_forces_rsi_sort_to_asc() -> None:
    result = await finalize_crypto_screen(
        candidates=[{"symbol": "KRW-BTC", "rsi": 45.0, "rsi_bucket": 4}],
        filters_applied={"market": "crypto", "sort_by": "rsi", "sort_order": "desc"},
        market="crypto",
        limit=20,
        max_rsi=None,
        warnings=[],
        rsi_enrichment={"attempted": 0, "succeeded": 0, "failed": 0, "timeout": 0, "error_samples": []},
        coingecko_payload={"data": {}, "cached": True, "age_seconds": 0.0, "stale": False, "error": None},
        total_markets=1,
        top_by_volume=1,
        filtered_by_warning=0,
        filtered_by_crash=0,
    )

    assert result["filters_applied"]["sort_order"] == "asc"
```

Add one integration-level regression in `tests/test_mcp_screen_stocks.py` asserting that legacy and tvscreener crypto paths still emit the same warning text and `meta` keys for RSI-sort coercion.

**Step 2: Run the targeted tests to verify failure**

Run: `uv run pytest --no-cov tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks.py -k "finalize_crypto_screen or rsi always uses ascending" -q`  
Expected: FAIL because `analysis_screen_crypto.py` and `finalize_crypto_screen(...)` do not exist yet.

**Step 3: Create the shared crypto finalizer**

In `app/mcp_server/tooling/analysis_screen_crypto.py` add:

- `TypedDict` aliases for crypto candidate/filter/response helpers
- `finalize_crypto_screen(...)`
- small private helpers for:
  - CoinGecko merge
  - warning list appends
  - `max_rsi` post-filter
  - `sort_by="rsi"` coercion
  - `meta` assembly

**Step 4: Rewire core callers without changing public entrypoints**

In `app/mcp_server/tooling/analysis_screen_core.py`:

- keep `_screen_crypto(...)` and `_screen_crypto_via_tvscreener(...)` names and signatures
- replace their duplicated tail logic with calls into `finalize_crypto_screen(...)`
- keep `screen_stocks_unified(...)` untouched at the signature level

**Step 5: Run the targeted tests**

Run: `uv run pytest --no-cov tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks.py tests/test_crypto_composite_score.py -q`  
Expected: PASS

**Step 6: Commit**

```bash
git add app/mcp_server/tooling/analysis_screen_crypto.py app/mcp_server/tooling/analysis_screen_core.py tests/test_tvscreener_crypto.py tests/test_mcp_screen_stocks.py tests/test_crypto_composite_score.py
git commit -m "refactor: extract crypto screen finalizer"
```

---

### Task 3: `_analyze_stock_impl`를 새 모듈로 옮기고 facade alias를 유지

**Files:**
- Create: `app/mcp_server/tooling/analysis_analyze.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `tests/test_mcp_fundamentals_tools.py`

**Step 1: Write the failing tests**

```python
from app.mcp_server.tooling import analysis_analyze, analysis_screening


@pytest.mark.asyncio
async def test_analysis_screening_analyze_alias_delegates_to_analysis_analyze(monkeypatch):
    called: dict[str, object] = {}

    async def fake_impl(symbol: str, market: str | None, include_peers: bool):
        called["symbol"] = symbol
        called["market"] = market
        called["include_peers"] = include_peers
        return {"symbol": symbol, "source": "analysis-analyze"}

    monkeypatch.setattr(analysis_analyze, "analyze_stock_impl", fake_impl)

    result = await analysis_screening._analyze_stock_impl("005930", "kr", False)

    assert result["source"] == "analysis-analyze"
    assert called == {"symbol": "005930", "market": "kr", "include_peers": False}
```

**Step 2: Run the targeted test to verify failure**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py -k "analysis_analyze or analyze_alias" -q`  
Expected: FAIL because `analysis_analyze.py` and the delegation alias do not exist yet.

**Step 3: Create the implementation module**

In `app/mcp_server/tooling/analysis_analyze.py`:

- move the current `_analyze_stock_impl` body into `analyze_stock_impl(...)`
- keep all helper usage and response keys unchanged

In `app/mcp_server/tooling/analysis_screening.py`:

- replace the old inline body with a thin alias/wrapper

```python
from app.mcp_server.tooling.analysis_analyze import analyze_stock_impl as _analyze_stock_impl_core


async def _analyze_stock_impl(symbol: str, market: str | None = None, include_peers: bool = False) -> dict[str, Any]:
    return await _analyze_stock_impl_core(symbol=symbol, market=market, include_peers=include_peers)
```

**Step 4: Re-run the targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_fundamentals_tools.py -k "analysis_analyze or analyze_alias or numeric_symbol" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_analyze.py app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py tests/test_mcp_fundamentals_tools.py
git commit -m "refactor: move analyze implementation behind facade"
```

---

### Task 4: `analysis_recommend.py`를 단계 함수로 분해

**Files:**
- Modify: `app/mcp_server/tooling/analysis_recommend.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `tests/test_mcp_recommend.py`

**Step 1: Write the failing tests**

`tests/test_mcp_recommend.py`에 새 helper surface를 고정하는 테스트를 추가한다.

```python
def test_empty_recommend_response_keeps_contract_shape() -> None:
    result = analysis_recommend._empty_recommend_response(
        budget=1_000_000,
        strategy="balanced",
        strategy_description="Balanced allocation",
        warnings=["example"],
        diagnostics={"phase": "test"},
        fallback_applied=False,
    )

    assert result["recommendations"] == []
    assert result["warnings"] == ["example"]
    assert result["fallback_applied"] is False
    assert result["diagnostics"] == {"phase": "test"}
```

Add one integration regression that monkeypatches `_collect_crypto_candidates(...)` or `_score_and_allocate(...)` to prove `recommend_stocks_impl(...)` flows through the new phases.

**Step 2: Run the targeted tests to verify failure**

Run: `uv run pytest --no-cov tests/test_mcp_recommend.py -k "empty_recommend_response or collect_crypto_candidates or score_and_allocate" -q`  
Expected: FAIL because the phase helpers do not exist yet.

**Step 3: Extract the phase helpers with no schema changes**

In `app/mcp_server/tooling/analysis_recommend.py`, add:

- `_prepare_recommend_request(...)`
- `_collect_kr_candidates(...)`
- `_collect_us_candidates(...)`
- `_collect_crypto_candidates(...)`
- `_apply_exclusions_and_dedupe(...)`
- `_apply_kr_relaxed_fallback(...)`
- `_enrich_missing_rsi(...)`
- `_score_and_allocate(...)`
- `_empty_recommend_response(...)`
- `_build_recommend_response(...)`

Keep `recommend_stocks_impl(...)` as the stable public implementation entrypoint in that module. Keep `analysis_screening._recommend_stocks_impl(...)` as a thin facade that delegates to it.

**Step 4: Re-run the targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_recommend.py -k "empty_recommend_response or collect_crypto_candidates or score_and_allocate or fallback_applied" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_recommend.py app/mcp_server/tooling/analysis_screening.py tests/test_mcp_recommend.py
git commit -m "refactor: split recommend workflow into phases"
```

---

### Task 5: handler import를 screening facade 하나로 수렴

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `app/mcp_server/tooling/analysis_screening.py`
- Modify: `tests/test_mcp_screen_stocks.py`
- Modify: `tests/test_mcp_recommend.py`
- Modify: `tests/_mcp_tooling_support.py`

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_screen_stocks_tool_uses_analysis_screening_facade(monkeypatch):
    tools = build_tools()

    async def fake_screen(**kwargs):
        return {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": kwargs,
            "market": kwargs["market"],
            "timestamp": "2026-03-10T00:00:00Z",
            "meta": {"source": "screening-facade"},
        }

    monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

    result = await tools["screen_stocks"](market="kr")

    assert result["meta"]["source"] == "screening-facade"
```

**Step 2: Run the targeted tests to verify failure**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks.py tests/test_mcp_recommend.py -k "screening_facade" -q`  
Expected: FAIL if handler still binds core symbols directly.

**Step 3: Clean up the imports one symbol at a time**

In `app/mcp_server/tooling/analysis_tool_handlers.py`:

- remove direct imports from `analysis_screen_core.py`
- use only `analysis_screening` for screen/analyze/recommend helpers

In `app/mcp_server/tooling/analysis_screening.py`:

- keep temporary re-export aliases needed by handlers and tests

In `tests/_mcp_tooling_support.py`:

- keep `_PATCH_MODULES` synchronized with the chosen shim ownership

**Step 4: Re-run the targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks.py tests/test_mcp_recommend.py tests/test_mcp_fundamentals_tools.py -k "screening_facade or shim or analyze_stock" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_screening.py tests/test_mcp_screen_stocks.py tests/test_mcp_recommend.py tests/test_mcp_fundamentals_tools.py tests/_mcp_tooling_support.py
git commit -m "refactor: route MCP analysis handlers through screening facade"
```

---

### Task 6: 테스트 helper를 정리한 뒤 파일을 split

**Files:**
- Modify: `tests/test_mcp_screen_stocks.py`
- Modify: `tests/test_mcp_recommend.py`
- Modify: `tests/_mcp_tooling_support.py`
- Create: `tests/_mcp_recommend_support.py`
- Create: `tests/test_mcp_screen_stocks_kr.py`
- Create: `tests/test_mcp_screen_stocks_tvscreener_contract.py`
- Create: `tests/test_mcp_screen_stocks_crypto.py`
- Create: `tests/test_mcp_screen_stocks_filters_and_rsi.py`
- Create: `tests/test_mcp_recommend_scoring.py`
- Create: `tests/test_mcp_recommend_flow.py`

**Step 1: Write the failing collection checks**

Add a temporary smoke test file or use targeted collection commands to enforce the new split layout.

Run: `uv run pytest --collect-only tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_recommend_scoring.py tests/test_mcp_recommend_flow.py -q`  
Expected: FAIL because the new files do not exist yet.

**Step 2: Extract shared helpers first**

Move reusable mocks/helpers into:

- `tests/_mcp_tooling_support.py`
- `tests/_mcp_recommend_support.py` (only if recommend-specific setup does not belong in the generic helper file)

**Step 3: Move tests without changing assertions**

- split by class/behavior boundary only
- keep test names and assertion bodies unchanged
- change import paths minimally
- leave direct-core tests (`tests/test_tvscreener_*.py`, `tests/test_crypto_composite_score.py`) where they are

**Step 4: Re-run collection and focused suites**

Run:

- `uv run pytest --collect-only tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py -q`
- `uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py -q`

Expected: PASS

**Step 5: Commit**

```bash
git add tests/_mcp_tooling_support.py tests/_mcp_recommend_support.py tests/test_mcp_screen_stocks_kr.py tests/test_mcp_screen_stocks_tvscreener_contract.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_screen_stocks_filters_and_rsi.py tests/test_mcp_recommend_scoring.py tests/test_mcp_recommend_flow.py tests/test_mcp_screen_stocks.py tests/test_mcp_recommend.py
git commit -m "test: split MCP analysis suites by ownership"
```

---

### Task 7: full verification and cleanup

**Files:**
- Modify: any touched files only if verification exposes breakage

**Step 1: Run the focused regression suites**

Run:

- `uv run pytest --no-cov tests/test_tvscreener_stocks.py tests/test_tvscreener_crypto.py tests/test_crypto_composite_score.py tests/test_mcp_fundamentals_tools.py -q`
- `uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py -q`

Expected: PASS

**Step 2: Run lint and type checks**

Run:

- `make lint`
- `uv run ty check app/mcp_server/tooling`

Expected: PASS

**Step 3: Run the MCP registration smoke check**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_*.py tests/test_mcp_recommend_*.py tests/test_mcp_fundamentals_tools.py -k "build_tools or register" -q`  
Expected: PASS and no registration drift.

**Step 4: Commit**

```bash
git add app/mcp_server/tooling tests
git commit -m "refactor: finish MCP analysis path split"
```

---

Plan complete and saved to `docs/plans/2026-03-10-mcp-analysis-path-refactor-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

Which approach?
