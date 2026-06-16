# ROB-584 — MCP Name Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `analyze_stock_batch`, `get_quote`, `get_orderbook`, `get_execution_strength` 등 MCP 도구 응답에 정확한 종목명(`name`) 및 해석 상태(`name_resolved`) 필드를 추가하여 에이전트의 오해를 방지한다.

**Architecture:** 중앙화된 `resolve_names` 헬퍼를 `app/mcp_server/tooling/name_resolution.py`에 구현하고, 각 도구의 응답 구성 단계에서 이를 호출하여 페이로드를 보강한다.

**Tech Stack:** Python, SQLAlchemy (Universe DB lookup), MCP

---

### Task 1: Create `name_resolution.py` helper

**Files:**
- Create: `app/mcp_server/tooling/name_resolution.py`

- [x] **Step 1: 구현**
  - 시장별 유니버스 서비스를 import 하여 `resolve_names` 함수 구현.
  - `equity_kr`, `equity_us`, `crypto` 지원.
  - Fallback: 이름 조회 실패 시 심볼 반환 및 `name_resolved: False`.

```python
from typing import Any
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.us_symbol_universe_service import get_us_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names

async def resolve_names(symbols: list[str], market_type: str) -> dict[str, dict[str, Any]]:
    results = {}
    if market_type == "equity_kr":
        names = await get_kr_names_by_symbols(symbols)
        for sym in symbols:
            name = names.get(sym)
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    elif market_type == "equity_us":
        names = await get_us_names_by_symbols(symbols)
        for sym in symbols:
            name = names.get(sym)
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    elif market_type == "crypto":
        # upbit_service returns {market: {"korean_name": ..., "english_name": ...}}
        display_info = await get_upbit_market_display_names(symbols)
        for sym in symbols:
            info = display_info.get(sym)
            name = (info.get("korean_name") or info.get("english_name")) if info else None
            results[sym] = {"name": name or sym, "name_resolved": name is not None}
    else:
        for sym in symbols:
            results[sym] = {"name": sym, "name_resolved": False}
    return results
```

- [x] **Step 2: Commit**
```bash
git add app/mcp_server/tooling/name_resolution.py
git commit -m "feat(ROB-584): add name resolution helper for MCP tools"
```

---

### Task 2: Integrate into `analyze_stock_batch`

**Files:**
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`

- [x] **Step 1: `_run_batch_analysis` 수정**
  - 분석 완료 후 `resolve_names`를 호출하여 각 `result`에 `name`, `name_resolved` 주입.

- [x] **Step 2: `_summarize_analysis_result` 수정**
  - 요약 페이로드에 `name`, `name_resolved` 필드 추가.

- [x] **Step 3: Commit**
```bash
git add app/mcp_server/tooling/analysis_tool_handlers.py
git commit -m "feat(ROB-584): enrich analyze_stock_batch response with stock names"
```

---

### Task 3: Integrate into Market Data Tools (`get_quote`, etc.)

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`

- [x] **Step 1: `_get_quote_impl` 수정**
  - 반환 직전 이름 해석 및 필드 추가.

- [x] **Step 2: `_build_orderbook_payload` 수정**
  - 페이로드 구성 시 이름 해석 및 필드 추가.

- [x] **Step 3: `_get_execution_strength_impl` 수정**
  - 반환 직전 이름 해석 및 필드 추가.

- [x] **Step 4: Commit**
```bash
git add app/mcp_server/tooling/market_data_quotes.py
git commit -m "feat(ROB-584): enrich quote, orderbook, and execution strength with stock names"
```

---

### Task 4: Verification

- [x] **Step 1: Unit Test 작성 및 실행**
  - `tests/test_mcp_name_resolution.py` 신규 작성하여 `resolve_names` 헬퍼 검증.
  - `tests/test_mcp_fundamentals_tools.py`의 `test_analyze_stock_batch` 관련 테스트에서 `name` 필드 존재 여부 확인.

- [x] **Step 2: 전체 테스트 실행**
```bash
uv run pytest tests/test_mcp_fundamentals_tools.py -k "analyze_stock_batch"
uv run pytest tests/test_mcp_market_data_tools.py -k "get_quote or get_orderbook"
```

- [x] **Step 3: Commit**
```bash
git add tests/
git commit -m "test(ROB-584): verify name resolution in MCP tool responses"
```
