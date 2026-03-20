# Issue #232 KR get_ohlcv Intraday Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** KR `get_ohlcv`에 `1m/5m/15m/30m/1h`를 추가하고, PR #230의 shared OHLCV contract (`include_indicators`, shared period matrix, `indicators_included`)를 복구하면서 US/crypto 기존 계약을 깨지 않는다.

**Architecture:** 먼저 shared constants와 contract tests로 MCP/service period matrix를 고정한다. 그 다음 KR intraday는 `app/services/kr_hourly_candles_read_service.py`의 기존 `1h` DB-first 경로를 interval-aware reader로 일반화하고, Timescale `kr_candles_5m/15m/30m/1h` + 최근 30분 KIS overlay 조합으로 읽는다. MCP는 KR intraday public row extras와 indicators를 노출하고, service layer는 기존 `Candle` 계약을 유지한다.

**Tech Stack:** Python 3.13+, FastMCP, pandas, SQLAlchemy async, TimescaleDB, Alembic, TaskIQ, pytest, Ruff, Pyright

---

### Task 1: Shared OHLCV period matrix를 테스트로 먼저 고정

**Files:**
- Create: `app/services/market_data/constants.py`
- Modify: `tests/test_mcp_ohlcv_tools.py`
- Modify: `tests/test_market_data_service.py`
- Reference: `app/mcp_server/tooling/market_data_quotes.py`
- Reference: `app/services/market_data/service.py`

**Step 1: Write failing MCP validation tests**

`tests/test_mcp_ohlcv_tools.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m"])
async def test_get_ohlcv_crypto_minute_periods_non_crypto_rejected(symbol, market, period):
    tools = build_tools()
    with pytest.raises(
        ValueError,
        match=rf"period '{period}' is supported only for crypto",
    ):
        await tools["get_ohlcv"](symbol, period=period, market=market)


@pytest.mark.asyncio
async def test_get_ohlcv_invalid_period_message_lists_all_supported_periods():
    tools = build_tools()
    with pytest.raises(
        ValueError,
        match="period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'",
    ):
        await tools["get_ohlcv"]("AAPL", period="hour")
```

**Step 2: Write failing service-layer validation tests**

`tests/test_market_data_service.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_non_crypto_5m_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(
        ValidationError, match="period '5m' is supported only for crypto"
    ):
        await market_data_service.get_ohlcv(
            symbol="AAPL",
            market="us",
            period="5m",
            count=10,
        )
```

**Step 3: Run tests to verify failure first**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py -k "period or invalid_period" -q`  
Expected: FAIL because shared constants and expanded validation do not exist yet.

**Step 4: Add shared constants and wire both validators to them**

Create `app/services/market_data/constants.py` with the period matrix and shared error text.

```python
OHLCV_ALLOWED_PERIODS = (
    "day",
    "week",
    "month",
    "1m",
    "5m",
    "15m",
    "30m",
    "4h",
    "1h",
)

KR_OHLCV_PERIODS = frozenset({"day", "week", "month", "1m", "5m", "15m", "30m", "1h"})
US_OHLCV_PERIODS = frozenset({"day", "week", "month", "1h"})
CRYPTO_OHLCV_PERIODS = frozenset({"day", "week", "month", "1m", "5m", "15m", "30m", "1h", "4h"})
CRYPTO_ONLY_OHLCV_PERIODS = frozenset({"1m", "5m", "15m", "30m", "4h"})

OHLCV_PERIOD_ERROR = (
    "period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'"
)
```

Update `app/mcp_server/tooling/market_data_quotes.py` and `app/services/market_data/service.py` to import and use these constants.

**Step 5: Re-run the targeted tests**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py -k "period or invalid_period" -q`  
Expected: PASS

**Step 6: Commit**

```bash
git add app/services/market_data/constants.py app/mcp_server/tooling/market_data_quotes.py app/services/market_data/service.py tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py
git commit -m "refactor: centralize OHLCV period validation"
```

---

### Task 2: `include_indicators` contract를 MCP `get_ohlcv`에 복구

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `tests/test_mcp_ohlcv_tools.py`
- Modify: `app/mcp_server/README.md`
- Reference: `app/mcp_server/tooling/market_data_indicators.py`

**Step 1: Add failing MCP tests for indicator enrichment**

`tests/test_mcp_ohlcv_tools.py`에 아래 테스트를 추가한다.

```python
@pytest.mark.asyncio
async def test_get_ohlcv_include_indicators_enriches_crypto_minute_rows(monkeypatch):
    tools = build_tools()
    df = _multi_row_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC", count=25, period="1m", include_indicators=True
    )

    assert result["indicators_included"] is True
    assert result["rows"][-1]["rsi_14"] is not None
    assert result["rows"][-1]["ema_20"] is not None
    assert result["rows"][-1]["bb_upper"] is not None
    assert result["rows"][-1]["vwap"] is not None
```

Add one KR day or US day test to lock `vwap is None` for non-intraday periods.

**Step 2: Run targeted tests to confirm failure**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py -k "include_indicators or indicators_included" -q`  
Expected: FAIL because `get_ohlcv` does not accept `include_indicators` yet.

**Step 3: Implement minimal MCP-only indicator enrichment**

In `app/mcp_server/tooling/market_data_quotes.py`:

- add `include_indicators: bool = False` to `get_ohlcv`
- add top-level `indicators_included`
- add explicit helper that appends only the allowed indicator keys to each public row
- keep crypto minute public keys stable

Use explicit key injection like this instead of leaking raw DataFrame columns:

```python
_OHLCV_INDICATOR_ROW_KEYS = (
    "rsi_14",
    "ema_20",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "vwap",
)
```

**Step 4: Update MCP README contract**

Document `include_indicators`, `indicators_included`, and the row-level indicator keys in `app/mcp_server/README.md`.

**Step 5: Re-run targeted indicator tests**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py -k "include_indicators or indicators_included" -q`  
Expected: PASS

**Step 6: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/mcp_server/README.md tests/test_mcp_ohlcv_tools.py
git commit -m "feat: restore get_ohlcv indicator contract"
```

---

### Task 3: KR intraday reader 일반화 테스트를 먼저 추가

**Files:**
- Modify: `tests/test_kr_hourly_candles_read_service.py`
- Reference: `app/services/kr_hourly_candles_read_service.py`

**Step 1: Add failing tests for 1m/5m/15m/30m reader behavior**

Extend `tests/test_kr_hourly_candles_read_service.py` with tests that cover:

- `1m` raw DB read with KRX/NTX merge
- `5m/15m/30m` history from the matching CAGG source
- recent 30-minute overlay re-aggregation
- `end_date` in the past disables live overlay
- same-day fallback uses KIS pagination only when DB count is short

Example skeleton:

```python
@pytest.mark.asyncio
async def test_read_kr_intraday_candles_5m_reaggregates_recent_overlay(monkeypatch):
    from app.services import kr_hourly_candles_read_service as svc

    out = await svc.read_kr_intraday_candles(
        symbol="005930",
        period="5m",
        count=4,
        end_date=None,
        now_kst=_dt_kst(2026, 2, 23, 14, 12, 0),
    )

    assert list(out["datetime"])[-1] == datetime.datetime(2026, 2, 23, 14, 10, 0)
    assert out.iloc[-1]["session"] == "regular"
    assert out.iloc[-1]["venues"] == ["KRX", "NTX"]
```

**Step 2: Run targeted tests to verify failure first**

Run: `uv run pytest --no-cov tests/test_kr_hourly_candles_read_service.py -k "read_kr_intraday_candles or 5m or 15m or 30m" -q`  
Expected: FAIL because `read_kr_intraday_candles` does not exist yet.

**Step 3: Implement interval config scaffolding in the reader**

Add an internal config map in `app/services/kr_hourly_candles_read_service.py`.

```python
@dataclass(frozen=True, slots=True)
class IntradayPeriodConfig:
    period: str
    bucket_minutes: int
    history_source: str


_INTRADAY_PERIOD_CONFIG = {
    "1m": IntradayPeriodConfig("1m", 1, "raw"),
    "5m": IntradayPeriodConfig("5m", 5, "public.kr_candles_5m"),
    "15m": IntradayPeriodConfig("15m", 15, "public.kr_candles_15m"),
    "30m": IntradayPeriodConfig("30m", 30, "public.kr_candles_30m"),
    "1h": IntradayPeriodConfig("1h", 60, "public.kr_candles_1h"),
}
```

Add `read_kr_intraday_candles(...)` and rewrite `read_kr_hourly_candles_1h(...)` as a thin wrapper.

**Step 4: Re-run targeted reader tests**

Run: `uv run pytest --no-cov tests/test_kr_hourly_candles_read_service.py -k "read_kr_intraday_candles or 5m or 15m or 30m or 1h" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/kr_hourly_candles_read_service.py tests/test_kr_hourly_candles_read_service.py
git commit -m "feat: generalize KR intraday candle reader"
```

---

### Task 4: Timescale 5m/15m/30m continuous aggregate를 추가

**Files:**
- Modify: `scripts/sql/kr_candles_timescale.sql`
- Create: `alembic/versions/<new_revision>_add_kr_intraday_caggs.py`
- Modify: `alembic/versions/d31f0a2b4c6d_add_kr_candles_retention_policy.py`
- Modify: `tests/test_kr_candles_sync.py`

**Step 1: Add failing schema/runtime tests**

Extend `tests/test_kr_candles_sync.py` with assertions that the SQL/migration assets now mention:

- `public.kr_candles_5m`
- `public.kr_candles_15m`
- `public.kr_candles_30m`
- `timescaledb.materialized_only = false`
- refresh policy with `5 minutes`
- unchanged cron `*/10 * * * 1-5`

Example skeleton:

```python
def test_kr_candles_timescale_sql_mentions_new_intraday_caggs():
    sql = Path("scripts/sql/kr_candles_timescale.sql").read_text()
    assert "CREATE MATERIALIZED VIEW public.kr_candles_5m" in sql
    assert "CREATE MATERIALIZED VIEW public.kr_candles_15m" in sql
    assert "CREATE MATERIALIZED VIEW public.kr_candles_30m" in sql
```

**Step 2: Run the targeted schema tests to confirm failure**

Run: `uv run pytest --no-cov tests/test_kr_candles_sync.py -k "timescale or cron" -q`  
Expected: FAIL because the new views are not declared yet.

**Step 3: Add the new SQL and Alembic migration**

In `scripts/sql/kr_candles_timescale.sql`, mirror the existing `kr_candles_1h` pattern for `5m`, `15m`, and `30m`.

```sql
CREATE MATERIALIZED VIEW public.kr_candles_5m
WITH (
  timescaledb.continuous,
  timescaledb.materialized_only = false
) AS
SELECT
  time_bucket(INTERVAL '5 minutes', time, 'Asia/Seoul') AS bucket,
  symbol,
  first(open, time) AS open,
  max(high) AS high,
  min(low) AS low,
  last(close, time) AS close,
  sum(volume) AS volume,
  sum(value) AS value,
  array_agg(DISTINCT venue ORDER BY venue) AS venues
FROM public.kr_candles_1m
GROUP BY 1, 2;
```

Repeat for `15m` and `30m`, then add matching policies and retention coverage in Alembic.

**Step 4: Re-run the targeted schema tests**

Run: `uv run pytest --no-cov tests/test_kr_candles_sync.py -k "timescale or cron" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/sql/kr_candles_timescale.sql alembic/versions/d31f0a2b4c6d_add_kr_candles_retention_policy.py alembic/versions/*.py tests/test_kr_candles_sync.py
git commit -m "feat: add KR intraday candle aggregates"
```

---

### Task 5: MCP와 service를 새 KR intraday reader로 연결

**Files:**
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/services/market_data/service.py`
- Modify: `tests/test_mcp_ohlcv_tools.py`
- Modify: `tests/test_market_data_service.py`
- Reference: `app/services/kis_ohlcv_cache.py`

**Step 1: Add failing wiring tests**

Add MCP tests that prove:

- KR `day` still uses `kis_ohlcv_cache`
- KR `1m/5m/15m/30m/1h` do not call `kis_ohlcv_cache`
- KR intraday rows expose `session` and `venues`

Add service tests that prove:

- KR `1m/5m/15m/30m/1h` validation passes
- returned items are still `Candle` objects with core fields only

**Step 2: Run targeted wiring tests to verify failure first**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py -k "equity_kr and (1m or 5m or 15m or 30m or 1h or cache)" -q`  
Expected: FAIL because the new KR intraday periods are not wired yet.

**Step 3: Implement minimal wiring**

In `app/mcp_server/tooling/market_data_quotes.py`:

- route KR intraday periods to `read_kr_intraday_candles`
- keep `day` on `kis_ohlcv_cache`
- keep `week/month` on `inquire_daily_itemchartprice`

In `app/services/market_data/service.py`:

- use the same period matrix
- route KR intraday periods to the same reader and adapt DataFrame rows into `Candle`

Keep the service return conversion explicit:

```python
rows.append(
    Candle(
        symbol=symbol,
        market=market,
        source="kis",
        period=period,
        timestamp=timestamp,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        value=float(row["value"]) if row.get("value") is not None else None,
    )
)
```

**Step 4: Re-run targeted MCP/service tests**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py -k "equity_kr and (1m or 5m or 15m or 30m or 1h or cache)" -q`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/market_data_quotes.py app/services/market_data/service.py tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py
git commit -m "feat: wire KR intraday get_ohlcv through shared reader"
```

---

### Task 6: 문서 동기화와 전체 검증

**Files:**
- Modify: `app/mcp_server/README.md`
- Verify: `tests/test_mcp_ohlcv_tools.py`
- Verify: `tests/test_market_data_service.py`
- Verify: `tests/test_kr_hourly_candles_read_service.py`
- Verify: `tests/test_kr_candles_sync.py`

**Step 1: Update the README with the final KR intraday policy**

Document all of the following in `app/mcp_server/README.md`:

- KR minute periods `1m/5m/15m/30m/1h`
- DB-first + 10-minute sync + recent 30-minute KIS overlay policy
- KR intraday cache bypass
- KR intraday graceful-degradation behavior for missing universe rows / partial KIS overlay failure
- `include_indicators` and `indicators_included`

**Step 2: Run the main regression command**

Run: `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py tests/test_kr_hourly_candles_read_service.py tests/test_kr_candles_sync.py -q`  
Expected: PASS

**Step 3: Run lint**

Run: `make lint`  
Expected: PASS

**Step 4: Run type checks for touched runtime files**

Run: `uv run pyright app/mcp_server/tooling/market_data_quotes.py app/services/market_data/service.py app/services/kr_hourly_candles_read_service.py`  
Expected: PASS

**Step 5: Commit final polish if needed**

```bash
git add app/mcp_server/README.md app/mcp_server/tooling/market_data_quotes.py app/services/market_data/service.py app/services/kr_hourly_candles_read_service.py scripts/sql/kr_candles_timescale.sql alembic/versions/*.py tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py tests/test_kr_hourly_candles_read_service.py tests/test_kr_candles_sync.py
git commit -m "docs: finalize KR intraday get_ohlcv contract"
```

---

Plan complete and saved to `docs/plans/2026-03-09-issue-232-kr-get-ohlcv-intraday-implementation-plan.md`. Two execution options:

1. Subagent-Driven (this session) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Parallel Session (separate) - Open a new session with `superpowers:executing-plans`, batch execution with checkpoints
