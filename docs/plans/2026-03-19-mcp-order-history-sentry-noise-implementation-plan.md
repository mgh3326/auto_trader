# MCP order_history Sentry noise implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `get_order_history`의 의도된 입력 제약을 MCP 계약에 명확히 반영하고, 그 제약을 어긴 호출이 Sentry에 중복 에러로 쌓이지 않도록 정리한다.

**Architecture:** 이번 수정은 기능 확장이 아니라 계약 정렬과 관측성 노이즈 제거다. `status != "pending"`에서 `symbol`이 필요한 현재 동작은 유지하고, MCP tool description/README/tests를 그 동작에 맞춘다. 동시에 Sentry `before_send` 필터를 확장해 `get_order_history`의 예상된 인자 오류가 `ValueError`/`ToolError` 이중 이벤트로 남지 않게 한다.

**Tech Stack:** Python 3.13, FastMCP, sentry-sdk, pytest

**References:**
- Sentry Issue: [AUTO_TRADER-40](https://mgh3326-daum.sentry.io/issues/AUTO_TRADER-40/)
- Sentry Issue: [AUTO_TRADER-41](https://mgh3326-daum.sentry.io/issues/AUTO_TRADER-41/)
- Validation gate: `app/mcp_server/tooling/orders_history.py:56-69`
- MCP tool registration: `app/mcp_server/tooling/orders_registration.py:24-49`
- Sentry filtering: `app/monitoring/sentry.py:90-95`, `app/monitoring/sentry.py:178-208`

---

## Root Cause

실제 실패 지점은 [`app/mcp_server/tooling/orders_history.py:56`](../../app/mcp_server/tooling/orders_history.py) 근처다. 현재 구현은 `status != "pending"` 이면서 `symbol`이 비어 있으면 즉시 `ValueError`를 던진다.

하지만 MCP 공개 계약은 아직 [`app/mcp_server/tooling/orders_registration.py:27`](../../app/mcp_server/tooling/orders_registration.py) 와 [`app/mcp_server/README.md:66`](../../app/mcp_server/README.md) 에서 `symbol=None` 을 일반적으로 허용하는 것처럼 읽힌다. 그 결과 LLM/debug client가 `status="filled"` + `symbol=None` 호출을 시도하고, FastMCP가 같은 실패를:

1. `fastmcp.server.server` 로그 이벤트 (`AUTO_TRADER-41`)
2. MCP integration의 `ToolError` 래핑 이벤트 (`AUTO_TRADER-40`)

로 각각 남긴다.

`app/services/n8n_daily_brief_service.py` 가 filled 주문 조회를 심볼별 fan-out으로 우회하고 있는 점을 보면, "심볼 없이 filled/cancelled/all 조회 지원"은 버그 수정이 아니라 별도 기능 작업이다. 이번 플랜에서는 그 확장을 하지 않는다.

## Recommended Scope

1. `get_order_history`의 현재 제약을 테스트와 문서에 명시한다.
2. Sentry에서 이 특정 인자 오류를 "예상된 MCP client misuse"로 분류해 드롭한다.
3. 실제 실행 실패까지 숨기지 않도록 메시지 패턴을 좁게 유지한다.

---

### Task 1: Lock the current `get_order_history` contract with tests

**Files:**
- Modify: `tests/test_mcp_order_tools.py:18-24`
- Modify: `app/mcp_server/tooling/orders_history.py:56-69`

**Step 1: Replace the single validation test with a parameterized failing-contract test**

기존 `test_get_order_history_validation_error()`를 아래 형태로 확장:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["all", "filled", "cancelled"])
async def test_get_order_history_requires_symbol_for_non_pending_status(status):
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required when status="):
        await tools["get_order_history"](status=status, order_id="some-id")
```

핵심 확인점:
- `order_id`가 있어도 우회되지 않아야 한다.
- `pending`만 예외라는 계약이 테스트에 드러나야 한다.

**Step 2: Run the targeted test**

Run: `uv run pytest tests/test_mcp_order_tools.py::test_get_order_history_requires_symbol_for_non_pending_status -xvs`

Expected: PASS on current code

**Step 3: Tighten the validation message only if needed**

현재 메시지가 충분히 구체적이므로 코드 변경은 선택 사항이다. 다만 테스트가 애매하면 메시지를 다음처럼 고정:

```python
raise ValueError(
    f"symbol is required when status='{status}'. "
    "Use status='pending' for symbol-free queries, or provide a symbol "
    "(e.g. symbol='KRW-BTC')."
)
```

`status="all"` 도 이 메시지에 포함되어야 한다.

**Step 4: Re-run the surrounding order-tool tests**

Run: `uv run pytest tests/test_mcp_order_tools.py -xvs`

Expected: Existing order history tests remain PASS

**Step 5: Commit**

```bash
git add tests/test_mcp_order_tools.py app/mcp_server/tooling/orders_history.py
git commit -m "test: lock get_order_history symbol requirement"
```

---

### Task 2: Make the MCP contract explicit for LLM clients

**Files:**
- Modify: `app/mcp_server/tooling/orders_registration.py:25-40`
- Modify: `app/mcp_server/README.md:66-69`

**Step 1: Update the tool description**

`register_order_tools()` 안의 `get_order_history` 설명을 다음 수준으로 명확화:

```python
description=(
    "Get order history for a symbol. Supports Upbit (crypto) and KIS "
    "(KR/US equities). Pending orders can be queried without a symbol, "
    "but filled/cancelled/all queries require symbol."
)
```

가능하면 `order_id`만으로 filled 주문을 찾을 수 없다는 점도 한 문장으로 적는다.

**Step 2: Update README tool list and behavior note**

`app/mcp_server/README.md` 의 단순 시그니처 줄 아래에 behavior bullet 추가:

```md
- `get_order_history(symbol=None, status="all", order_id=None, limit=50)`
  - `status="pending"` 만 symbol 없이 호출 가능
  - `status in {"all", "filled", "cancelled"}` 는 symbol 필요
  - filled/cancelled 조회는 시장별 historical endpoint 제약 때문에 symbol fan-out을 자동 수행하지 않음
```

**Step 3: Verify the docs mention the same rule as the code**

Run: `rg -n "get_order_history|symbol 없이|filled/cancelled" app/mcp_server/README.md app/mcp_server/tooling/orders_registration.py`

Expected: Description and README both mention the same restriction

**Step 4: Sanity-check the existing test suite again**

Run: `uv run pytest tests/test_mcp_order_tools.py::test_get_order_history_requires_symbol_for_non_pending_status -xvs`

Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_registration.py app/mcp_server/README.md
git commit -m "docs: clarify get_order_history symbol requirement"
```

---

### Task 3: Add failing Sentry regression tests for this exact noise pattern

**Files:**
- Modify: `tests/test_sentry_init.py:650-730`

**Step 1: Add a log-path regression test for `AUTO_TRADER-41`**

```python
def test_fastmcp_get_order_history_symbol_requirement_log_dropped(self):
    event: Event = {
        "logger": "fastmcp.server.server",
        "logentry": {
            "message": (
                "Error calling tool 'get_order_history': symbol is required when "
                "status='filled'. Use status='pending' for symbol-free queries, "
                "or provide a symbol (e.g. symbol='KRW-BTC')."
            ),
            "formatted": (
                "Error calling tool 'get_order_history': symbol is required when "
                "status='filled'. Use status='pending' for symbol-free queries, "
                "or provide a symbol (e.g. symbol='KRW-BTC')."
            ),
        },
    }
    assert sentry_module._before_send(event, {}) is None
```

**Step 2: Add an exception-path regression test for `AUTO_TRADER-40`**

`ToolError` 래핑 이벤트를 흉내 내는 테스트 추가:

```python
def test_fastmcp_get_order_history_symbol_requirement_toolerror_dropped(self):
    event: Event = {
        "exception": {
            "values": [
                {
                    "type": "ToolError",
                    "value": (
                        "Error calling tool 'get_order_history': symbol is required "
                        "when status='filled'. Use status='pending' for symbol-free "
                        "queries, or provide a symbol (e.g. symbol='KRW-BTC')."
                    ),
                }
            ]
        }
    }
    assert sentry_module._before_send(event, {}) is None
```

**Step 3: Add a guard test so real tool failures are still kept**

```python
def test_fastmcp_real_runtime_error_kept(self):
    event: Event = {
        "exception": {
            "values": [
                {"type": "ToolError", "value": "Error calling tool 'get_order_history': upstream timeout"}
            ]
        }
    }
    assert sentry_module._before_send(event, {}) is not None
```

**Step 4: Run the focused Sentry tests to confirm they fail first**

Run: `uv run pytest tests/test_sentry_init.py -k "get_order_history_symbol_requirement or fastmcp_real_runtime_error_kept" -xvs`

Expected:
- New "dropped" assertions FAIL on current code
- "real runtime error kept" either PASS immediately or after naming adjustment

**Step 5: Commit failing tests**

```bash
git add tests/test_sentry_init.py
git commit -m "test: reproduce get_order_history sentry noise"
```

---

### Task 4: Filter expected `get_order_history` argument misuse in Sentry

**Files:**
- Modify: `app/monitoring/sentry.py:90-95`
- Modify: `app/monitoring/sentry.py:178-208`
- Modify: `tests/test_sentry_init.py:650-730`

**Step 1: Introduce a narrow helper for expected MCP argument noise**

`_is_fastmcp_tool_validation_error()` 옆에 전용 helper 추가:

```python
def _is_expected_mcp_argument_noise(
    logger_name: str | None,
    message: str | None,
    event: Event | None = None,
) -> bool:
    expected_snippet = (
        "symbol is required when status="
    )

    if logger_name == "fastmcp.server.server" and message:
        if (
            message.startswith("Error calling tool 'get_order_history'")
            and expected_snippet in message
        ):
            return True

    if not event:
        return False

    values = event.get("exception", {}).get("values", [])
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, dict):
                continue
            exc_type = value.get("type")
            exc_value = value.get("value")
            if exc_type in {"ToolError", "ValueError"} and isinstance(exc_value, str):
                if "get_order_history" in exc_value and expected_snippet in exc_value:
                    return True

    return False
```

포인트:
- `get_order_history` + `symbol is required when status=` 조합만 드롭
- `upstream timeout`, `HTTP 500`, provider failure 같은 실제 런타임 에러는 유지

**Step 2: Wire the helper into `_before_send()` and `_before_send_log()`**

```python
if _is_expected_mcp_argument_noise(logger_name, message, event):
    return None
```

로그 경로는 `event`가 없으므로 message 기반으로만 판정:

```python
if _is_expected_mcp_argument_noise(logger_name, message):
    return None
```

기존 healthcheck/yfinance/validation filter 순서는 그대로 유지한다.

**Step 3: Run the targeted Sentry tests**

Run: `uv run pytest tests/test_sentry_init.py -k "fastmcp" -xvs`

Expected: All fastmcp noise-filter tests PASS

**Step 4: Run cross-check tests for MCP scope behavior**

Run: `uv run pytest tests/test_mcp_sentry_middleware.py -xvs`

Expected: PASS, because scope enrichment behavior should remain unchanged

**Step 5: Commit**

```bash
git add app/monitoring/sentry.py tests/test_sentry_init.py
git commit -m "fix: drop expected get_order_history argument noise in sentry

Fixes AUTO_TRADER-40
Fixes AUTO_TRADER-41"
```

---

### Task 5: Final verification

**Step 1: Run lint**

Run: `make lint`

Expected: PASS

**Step 2: Run focused regression suite**

Run: `uv run pytest tests/test_mcp_order_tools.py tests/test_sentry_init.py tests/test_mcp_sentry_middleware.py -xvs`

Expected: PASS

**Step 3: Optional live confirmation in staging/prod-like MCP runtime**

수동 확인:
1. MCP 서버 실행
2. `get_order_history(status="filled", market="crypto", limit=3)` 호출
3. MCP client에는 동일한 user-facing error가 보이되, 새로운 Sentry issue는 생성되지 않는지 확인

**Step 4: Merge note**

배포 후 Sentry에서 다음 검색으로 잔여 노이즈 여부 확인:

```text
project:python-fastapi mcp.tool.name:get_order_history "symbol is required when status="
```

Expected: 신규 이벤트 0건
