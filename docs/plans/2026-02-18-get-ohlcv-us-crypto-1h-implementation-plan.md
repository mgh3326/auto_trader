# get_ohlcv US/Crypto 1H Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP `get_ohlcv`에 `period="1h"`를 추가해 US equity/crypto에서 시간봉 조회를 지원하고, 기존 `4h`(crypto 전용) 및 일/주/월 동작을 그대로 유지한다.

**Architecture:** MCP 계층은 period 계약과 시장별 guard만 확장하고, 실제 캔들 수집은 기존 서비스 경로를 재사용한다. US는 `app/services/yahoo.py`에 `1h -> 60m` 라우팅을 추가하고 intraday(`1h`)는 기존 cache/closed-bucket 대상에서 제외한다. Crypto는 현재 `app/services/upbit.py` interval 라우팅을 그대로 사용한다.

**Tech Stack:** Python 3.13+, FastMCP, yfinance, pandas, pytest, Ruff, Pyright

---

### Task 1: MCP `get_ohlcv` 1H 계약 테스트 추가 (먼저 실패 고정)

**Files:**
- Modify: `tests/test_mcp_server_tools.py`
- Reference: `app/mcp_server/tooling/market_data_quotes.py`

**Step 1: Write failing tests for `1h` contract**

`tests/test_mcp_server_tools.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_period_1h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=150, period="1h")

    mock_fetch.assert_awaited_once_with(
        ticker="AAPL", days=100, period="1h", end_date=None
    )
    assert result["period"] == "1h"
    assert result["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_1h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=250, period="1h")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="1h", end_date=None
    )
    assert result["period"] == "1h"
    assert result["instrument_type"] == "crypto"


@pytest.mark.asyncio
async def test_get_ohlcv_period_1h_market_kr_rejected():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="period '1h' is not supported for korean equity"
    ):
        await tools["get_ohlcv"]("005930", period="1h", market="kr")
```

기존 invalid-period expectation도 `1h`가 허용값에 포함되도록 갱신한다.

**Step 2: Run targeted tests to verify failure first**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and (1h or invalid_period)" -q`  
Expected: FAIL (`get_ohlcv` period validation/guard가 아직 `1h`를 반영하지 않음)

**Step 3: Commit test-only change**

```bash
git add tests/test_mcp_server_tools.py
git commit -m "test: add get_ohlcv 1h contract coverage"
```

---

### Task 2: MCP `get_ohlcv` 1H 라우팅/가드 구현

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_server_tools.py`

**Step 1: Extend period validation and market guards**

`get_ohlcv`의 period validation을 확장한다.

```python
period = (period or "day").strip().lower()
if period not in ("day", "week", "month", "4h", "1h"):
    raise ValueError("period must be 'day', 'week', 'month', '4h', or '1h'")
```

market resolve 이후 guard를 추가한다.

```python
if period == "4h" and market_type != "crypto":
    raise ValueError("period '4h' is supported only for crypto")
if period == "1h" and market_type == "equity_kr":
    raise ValueError("period '1h' is not supported for korean equity")
```

**Step 2: Keep existing fetcher boundaries and caps**

- US `1h`는 기존 `_fetch_ohlcv_equity_us` 경로로 전달 (cap=100 유지)
- crypto `1h`는 기존 `_fetch_ohlcv_crypto` 경로로 전달 (cap=200 유지)

**Step 3: Update MCP README contract**

`app/mcp_server/README.md` `get_ohlcv` 설명을 아래처럼 갱신한다.

```markdown
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None)`
  - period: `day`, `week`, `month`, `4h`, `1h`
  - `4h`: crypto only
  - `1h`: US equity + crypto (`kr` unsupported)
```

**Step 4: Run targeted MCP tests**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv and (1h or 4h or invalid_period)" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/mcp_server/README.md tests/test_mcp_server_tools.py
git commit -m "feat: add get_ohlcv 1h support for us and crypto"
```

---

### Task 3: Yahoo 서비스 `1h` interval TDD

**Files:**
- Modify: `tests/test_services.py`
- Modify: `app/services/yahoo.py`

**Step 1: Add failing Yahoo service test**

`tests/test_services.py` `TestYahooService`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
@patch("app.services.yahoo.yf.download")
async def test_fetch_ohlcv_period_1h_uses_60m_interval(self, mock_download, monkeypatch):
    tracing_session = object()
    monkeypatch.setattr(
        "app.services.yahoo.build_yfinance_tracing_session",
        lambda: tracing_session,
    )
    monkeypatch.setattr(
        "app.services.yahoo.settings.yahoo_ohlcv_cache_enabled",
        False,
        raising=False,
    )
    mock_download.return_value = pd.DataFrame(
        {
            "open": [100, 101],
            "high": [105, 106],
            "low": [95, 96],
            "close": [103, 104],
            "volume": [1000, 1100],
        }
    )

    from app.services.yahoo import fetch_ohlcv

    result = await fetch_ohlcv("AAPL", days=2, period="1h")

    assert len(result) == 2
    assert mock_download.call_args.kwargs["interval"] == "60m"
```

**Step 2: Run targeted test to confirm failure**

Run: `uv run pytest --no-cov tests/test_services.py -k "YahooService and period_1h" -q`  
Expected: FAIL (`period` 미지원 ValueError)

**Step 3: Implement minimal Yahoo period map support**

`app/services/yahoo.py` `_fetch_ohlcv_raw`에 `1h` 매핑을 추가한다.

```python
period_map = {
    "day": "1d",
    "week": "1wk",
    "month": "1mo",
    "1h": "60m",
}
```

필요 시 lookback multiplier에 `1h` 항목을 추가한다 (예: `1h: 2`).

**Step 4: Re-run targeted Yahoo test**

Run: `uv run pytest --no-cov tests/test_services.py -k "YahooService and period_1h" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/yahoo.py tests/test_services.py
git commit -m "feat: add yahoo 1h ohlcv interval mapping"
```

---

### Task 4: Yahoo cache 경계 테스트 (`1h`는 캐시 우회)

**Files:**
- Modify: `tests/test_yahoo_service_cache.py`
- Reference: `app/services/yahoo.py`

**Step 1: Add failing cache-boundary test**

`tests/test_yahoo_service_cache.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_fetch_ohlcv_1h_bypasses_cache(monkeypatch):
    cache_mock = AsyncMock(return_value=None)
    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 16),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            }
        ]
    )
    raw_mock = AsyncMock(return_value=raw)

    monkeypatch.setattr(yahoo_cache, "get_closed_candles", cache_mock)
    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", raw_mock)
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=1, period="1h")

    assert len(result) == 1
    cache_mock.assert_not_called()
    raw_mock.assert_awaited_once()
```

**Step 2: Run targeted test to verify behavior**

Run: `uv run pytest --no-cov tests/test_yahoo_service_cache.py -k "1h_bypasses_cache" -q`  
Expected: PASS (or FAIL if `1h`가 cache allowlist에 잘못 포함되어 있으면 수정 필요)

**Step 3: If needed, fix cache allowlist guard**

`app/services/yahoo.py`에서 cache/closed-bucket 대상은 `{"day", "week", "month"}`만 유지한다.

```python
if normalized_period in {"day", "week", "month"} and settings.yahoo_ohlcv_cache_enabled:
    ...
```

**Step 4: Re-run cache tests**

Run: `uv run pytest --no-cov tests/test_yahoo_service_cache.py -k "fetch_ohlcv" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_yahoo_service_cache.py app/services/yahoo.py
git commit -m "test: lock yahoo 1h cache bypass behavior"
```

---

### Task 5: Regression + quality gates + final polish

**Files:**
- Verify touched files only

**Step 1: MCP `get_ohlcv` regression**

Run: `uv run pytest --no-cov tests/test_mcp_server_tools.py -k "get_ohlcv" -q`  
Expected: PASS

**Step 2: Service regression**

Run: `uv run pytest --no-cov tests/test_services.py -k "UpbitService or YahooService" -q`  
Expected: PASS

Run: `uv run pytest --no-cov tests/test_yahoo_service_cache.py -q`  
Expected: PASS

**Step 3: Lint/type checks on touched files**

Run: `uv run ruff check app/mcp_server/tooling/market_data_quotes.py app/services/yahoo.py tests/test_mcp_server_tools.py tests/test_services.py tests/test_yahoo_service_cache.py`  
Expected: PASS

Run: `uv run pyright app/mcp_server/tooling/market_data_quotes.py app/services/yahoo.py`  
Expected: PASS

**Step 4: Final commit for follow-up fixes (if any)**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/services/yahoo.py app/mcp_server/README.md tests/test_mcp_server_tools.py tests/test_services.py tests/test_yahoo_service_cache.py
git commit -m "chore: finalize get_ohlcv 1h contract for us and crypto"
```

---

## Guardrails for Execution

- DRY/YAGNI: `get_ohlcv` 내부 분기 최소화, 기존 fetcher 경계 유지.
- Strict TDD: `@superpowers/test-driven-development` 순서 준수.
- Verification first: `@superpowers/verification-before-completion`로 명령 출력 확인 후 완료 선언.
- Backward compatibility: `day/week/month` + `4h` 기존 계약/동작을 깨지 않는다.
- Scope control: KR `1h` 지원은 이 변경에서 제외.
