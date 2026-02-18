# get_ohlcv Crypto 4H + Upbit Candle Core Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP `get_ohlcv`에 `period="4h"`(crypto 전용)를 추가하고, Upbit 서비스에 향후 intraday 확장을 위한 공통 캔들 코어를 도입한다.

**Architecture:** MCP 계층은 period 계약만 확장하고(crypto-only guard), 실제 캔들 획득은 `app/services/upbit.py`의 공통 interval 라우팅 코어가 담당한다. 기존 `fetch_ohlcv` 시그니처는 유지하면서 내부 구현만 공통화해 `day/week/month`와 `4h`를 같은 경로에서 처리한다. 일/주/월 캐시/closed-bucket 동작은 기존과 동일하게 유지하고 intraday는 캐시 우회한다.

**Tech Stack:** Python 3.13+, FastMCP tool handlers, pandas, pytest, Ruff, Pyright

---

### Task 1: MCP `get_ohlcv` 계약 테스트 먼저 고정

**Files:**
- Modify: `tests/test_mcp_server_tools.py`
- Reference: `app/mcp_server/tooling/market_data_quotes.py`

**Step 1: Write failing tests for new period contract**

`tests/test_mcp_server_tools.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_4h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=250, period="4h")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="4h", end_date=None
    )
    assert result["period"] == "4h"
    assert result["instrument_type"] == "crypto"


@pytest.mark.asyncio
async def test_get_ohlcv_period_4h_market_kr_rejected():
    tools = build_tools()

    with pytest.raises(ValueError, match="period '4h' is supported only for crypto"):
        await tools["get_ohlcv"]("005930", period="4h", market="kr")


@pytest.mark.asyncio
async def test_get_ohlcv_period_4h_market_us_rejected():
    tools = build_tools()

    with pytest.raises(ValueError, match="period '4h' is supported only for crypto"):
        await tools["get_ohlcv"]("AAPL", period="4h", market="us")
```

기존 invalid period 테스트 메시지 expectation도 `4h` 허용 반영으로 수정한다.

**Step 2: Run targeted tests to confirm failure first**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and (4h or invalid_period)" -q`
Expected: FAIL (`period` validation/route가 아직 구현되지 않음)

**Step 3: Commit test-only change**

```bash
git add tests/test_mcp_server_tools.py
git commit -m "test: add get_ohlcv 4h contract coverage"
```

---

### Task 2: MCP `get_ohlcv` 4H(crypto-only) 구현

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Test: `tests/test_mcp_server_tools.py`
- Doc: `app/mcp_server/README.md`

**Step 1: Implement period validation and market guard**

`get_ohlcv`에서 period 허용값을 확장하고, market resolved 뒤 crypto-only 가드를 추가한다.

```python
period = (period or "day").strip().lower()
if period not in ("day", "week", "month", "4h"):
    raise ValueError("period must be 'day', 'week', 'month', or '4h'")

market_type, symbol = _resolve_market_type(symbol, market)

if period == "4h" and market_type != "crypto":
    raise ValueError("period '4h' is supported only for crypto")
```

**Step 2: Keep existing fetch flow and clamp behavior**

기존 `_fetch_ohlcv_crypto` 경로를 유지하되, `period="4h"`가 그대로 서비스 레이어로 전달되도록 한다.

**Step 3: Update MCP docs**

`app/mcp_server/README.md`의 `get_ohlcv` 설명에 `4h (crypto only)`를 추가한다.

예시 문구:

```markdown
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None)`
  - period: `day`, `week`, `month`, `4h` (crypto only)
```

**Step 4: Run targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and (4h or invalid_period)" -q`
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/mcp_server/README.md tests/test_mcp_server_tools.py
git commit -m "feat: add crypto-only 4h period support to get_ohlcv"
```

---

### Task 3: Upbit 공통 캔들 코어 도입 (`fetch_ohlcv` 내부 사용)

**Files:**
- Modify: `app/services/upbit.py`
- Test: `tests/test_services.py`
- Test: `tests/test_upbit_service_cache.py`

**Step 1: Add interval routing map and shared raw helper**

`app/services/upbit.py`에 공통 interval 라우팅을 추가한다.

```python
_INTERVAL_TO_ENDPOINT = {
    "day": "days",
    "week": "weeks",
    "month": "months",
    "1m": "minutes/1",
    "3m": "minutes/3",
    "5m": "minutes/5",
    "10m": "minutes/10",
    "15m": "minutes/15",
    "30m": "minutes/30",
    "1h": "minutes/60",
    "4h": "minutes/240",
}


def _normalize_upbit_interval(period: str) -> str:
    normalized = str(period or "").strip().lower()
    aliases = {"hour": "1h", "240m": "4h"}
    return aliases.get(normalized, normalized)
```

공통 raw helper를 추가한다.

```python
async def _fetch_candles_raw(
    market: str,
    count: int,
    interval: str,
    end_date: datetime | None = None,
) -> pd.DataFrame:
    endpoint = _INTERVAL_TO_ENDPOINT.get(interval)
    if endpoint is None:
        raise ValueError(f"period must be one of {list(_INTERVAL_TO_ENDPOINT.keys())}")

    count = min(max(int(count), 1), 200)
    url = f"{UPBIT_REST}/candles/{endpoint}"
    params = {"market": market, "count": count}
    if end_date is not None:
        params["to"] = end_date.strftime("%Y-%m-%dT%H:%M:%S")

    rows = await _request_json(url, params)
    # 기존 컬럼 매핑 로직 재사용
```

**Step 2: Rewire `fetch_ohlcv` to use shared helper**

- `fetch_ohlcv` 내부에서 interval normalize 수행
- day/week/month 캐시 경로는 기존 유지
- raw fetch는 `_fetch_candles_raw(..., interval=normalized_period)` 호출
- count(`days`)는 200 clamp
- day/week/month만 closed-bucket filter 적용

```python
normalized_period = _normalize_upbit_interval(period)
request_count = min(max(int(days), 1), 200)
...
raw = await _fetch_candles_raw(
    market=market,
    count=request_count,
    interval=normalized_period,
    end_date=end_date,
)
```

**Step 3: Keep compatibility wrappers**

`fetch_minute_candles`는 가능하면 `_fetch_candles_raw(..., interval=f"{unit}m")` 또는 interval map을 통해 공통 경로를 재사용하도록 정리한다.

**Step 4: Add service tests**

`tests/test_services.py`에 4h 라우팅/클램프 테스트 추가:

```python
@pytest.mark.asyncio
@patch("app.services.upbit._request_json")
async def test_fetch_ohlcv_4h_uses_minutes_240(mock_request):
    mock_request.return_value = []
    await upbit_service_module.fetch_ohlcv("KRW-BTC", days=300, period="4h")

    called_url = mock_request.await_args.args[0]
    called_params = mock_request.await_args.args[1]
    assert called_url.endswith("/candles/minutes/240")
    assert called_params["count"] == 200
```

`tests/test_upbit_service_cache.py`에는 기존 day/week/month 캐시 테스트가 그대로 통과하는지 유지한다 (intraday는 캐시 미적용).

**Step 5: Run tests**

Run: `uv run pytest --no-cov tests/test_services.py tests/test_upbit_service_cache.py -q`
Expected: PASS

**Step 6: Commit**

```bash
git add app/services/upbit.py tests/test_services.py tests/test_upbit_service_cache.py
git commit -m "refactor: add shared upbit candle core and support 4h period"
```

---

### Task 4: Full regression and quality gates

**Files:**
- Verify only (no required edits)

**Step 1: MCP regression run**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv" -q`
Expected: PASS

**Step 2: Service regression run**

Run: `uv run pytest --no-cov tests/test_services.py -k "UpbitService or YahooService" -q`
Expected: PASS

**Step 3: Lint and type checks on touched files**

Run: `uv run ruff check app/mcp_server/tooling/market_data_quotes.py app/services/upbit.py tests/test_mcp_server_tools.py tests/test_services.py tests/test_upbit_service_cache.py`
Expected: PASS

Run: `uv run pyright app/mcp_server/tooling/market_data_quotes.py app/services/upbit.py`
Expected: PASS

**Step 4: Final commit for any follow-up fixes**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/services/upbit.py app/mcp_server/README.md tests/test_mcp_server_tools.py tests/test_services.py tests/test_upbit_service_cache.py
git commit -m "chore: finalize get_ohlcv 4h contract and upbit candle refactor"
```

---

## Guardrails for Execution

- Keep changes minimal and backward-compatible (DRY/YAGNI).
- Follow TDD order strictly (`@superpowers/test-driven-development`).
- Verify every “done” claim with command output (`@superpowers/verification-before-completion`).
- Do not expand public MCP period list beyond `4h` in this change.
- Do not alter indicator timeframe defaults in this change.
