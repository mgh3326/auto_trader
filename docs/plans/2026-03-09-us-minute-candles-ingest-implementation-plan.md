# US Minute Candles Ingest Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a US-only minute-candle ingest pipeline that mirrors the existing KR candle sync structure, stores held-symbol minute data plus Timescale aggregates, and leaves FastAPI, MCP read paths, and `get_ohlcv(market="us")` unchanged.

**Architecture:** Add a dedicated US sync service that sources symbols from `KISClient.fetch_my_us_stocks()` plus manual holdings with `MarketType.US`, validates exchange metadata through `us_symbol_universe`, and pages the KIS overseas minute API backward with `KEYB` during XNYS regular-session minutes only. Store UTC 1-minute rows in `public.us_candles_1m`, build 5m/15m/30m/1h continuous aggregates, and keep job/task/script wrappers thin like the KR pipeline.

**Tech Stack:** Python 3.13, pandas, SQLAlchemy async sessions, TaskIQ, exchange_calendars (`XNYS`), KIS Open API, TimescaleDB, pytest.

---

## Recommended Approach

1. **Recommended: dedicated US clone of the KR sync pipeline.** Reuse the same task/job/service layering, SQL patterns, and operator script shape, but keep US-specific exchange/session logic isolated in new files.
2. **Alternative: shared market-agnostic candle sync core with KR/US adapters.** This reduces duplication later but expands scope now by refactoring working KR code and adds review risk to an issue that is explicitly scoped as a KR-pattern clone.
3. **Rejected: non-CAgg hourly rollup or read-time aggregation.** This avoids some Timescale complexity but violates the issue requirement for persistent 5m/15m/30m/1h Timescale aggregates.

This plan follows option 1.

## Critical Risk To Resolve Early

- The repo currently pins the KR Timescale guard at `2.8.1`, but Timescale added **continuous aggregate support for `time_bucket` origin/offset in 2.15.0**. Because `public.us_candles_1h` must align to `09:30/10:30/... America/New_York`, the new US migration and `scripts/sql/us_candles_timescale.sql` should explicitly enforce **TimescaleDB >= 2.15.0** for the US candle objects.
- The existing KIS constant `OVERSEAS_MINUTE_CHART_TR` is currently wrong for the overseas minute endpoint. Official KIS examples use `HHDFS76950200`, not the domestic TR ID.
- The existing KIS request wrapper returns JSON only, so the new paging logic should rely on `output1.next` / `output1.more` plus `KEYB`, not response headers.

### Task 1: Extend the KIS client for overseas minute pages

**Files:**
- Modify: `app/services/brokers/kis/constants.py`
- Modify: `app/services/brokers/kis/market_data.py`
- Modify: `app/services/brokers/kis/client.py`
- Modify: `tests/test_services.py`

**Step 1: Write the failing tests**

Add focused tests in `tests/test_services.py` for a new public client method, for example:

```python
@pytest.mark.asyncio
async def test_inquire_overseas_minute_chart_maps_exchange_and_parses_output2(monkeypatch):
    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "1", "more": "Y"},
            "output2": [
                {
                    "xymd": "20260306",
                    "xhms": "155900",
                    "open": "173.1",
                    "high": "173.4",
                    "low": "172.9",
                    "last": "173.2",
                    "evol": "1200",
                    "eamt": "207840",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL", exchange_code="NASD")

    assert list(page.frame.columns) == [
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    assert page.has_more is True
    assert page.next_keyb == "20260306155800"
```

Cover these cases:
- `NASD/NYSE/AMEX` input maps to KIS `NAS/NYS/AMS`
- request params use `NMIN="1"`, `NREC="120"`, `AUTH=""`, `FILL=""`
- empty payload returns an empty normalized frame
- token refresh on `EGW00123` / `EGW00121`
- malformed `output2` raises a controlled error
- `KEYB` pagination cursor is computed from the oldest `xymd+xhms` row minus one minute

**Step 2: Run the focused tests and confirm they fail**

Run: `uv run pytest tests/test_services.py -k "overseas_minute_chart" -q`

Expected: FAIL because the method does not exist yet and the TR ID is still wrong.

**Step 3: Implement the minimal client support**

- Fix `app/services/brokers/kis/constants.py`:
  - keep `OVERSEAS_MINUTE_CHART_URL = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"`
  - change `OVERSEAS_MINUTE_CHART_TR` to `"HHDFS76950200"`
- Add a small page return type in `app/services/brokers/kis/market_data.py`, for example:

```python
@dataclass(frozen=True, slots=True)
class OverseasMinutePage:
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None
```

- Add `MarketDataClient.inquire_overseas_minute_chart(...)` that:
  - accepts `symbol`, `exchange_code`, optional `keyb`, and `nrec=120`
  - maps `exchange_code` with `constants.OVERSEAS_EXCHANGE_MAP`
  - calls the overseas minute endpoint with `SYMB=to_kis_symbol(symbol)`
  - uses `PINC="1"` so continuation across the prior-day boundary works consistently
  - determines `has_more` from `output1.next` / `output1.more`
  - computes `next_keyb` from the oldest returned row using `xymd + xhms - interval '1 minute'`
  - normalizes `output2` into `datetime/date/time/open/high/low/close/volume/value`
  - prefers `last` for close, falls back to `clos`; prefers `evol` for volume and `eamt` for value
- Expose the method on `KISClient` in `app/services/brokers/kis/client.py`.

**Step 4: Re-run the focused tests**

Run: `uv run pytest tests/test_services.py -k "overseas_minute_chart" -q`

Expected: PASS.

### Task 2: Add Timescale schema and operator SQL for US candles

**Files:**
- Create: `alembic/versions/<revision>_add_us_candles_timescale.py`
- Create: `alembic/versions/<revision>_add_us_candles_retention_policy.py`
- Create: `scripts/sql/us_candles_timescale.sql`
- Create: `tests/test_us_candles_sync.py`
- Reference: `tests/test_kr_candles_sync.py`

**Step 1: Write migration and SQL assertions first**

Add tests in `tests/test_us_candles_sync.py` that assert:
- `public.us_candles_1m` appears in the new migration and SQL helper script
- `public.us_candles_5m`, `public.us_candles_15m`, `public.us_candles_30m`, `public.us_candles_1h` all appear
- `add_continuous_aggregate_policy`, `remove_continuous_aggregate_policy`, `add_retention_policy`, and `remove_retention_policy` are present
- the SQL checks for TimescaleDB `2.15.0` or newer
- the 1h definition is not naive top-of-hour UTC/KST bucketing

**Step 2: Run the new SQL tests and confirm they fail**

Run: `uv run pytest tests/test_us_candles_sync.py -k "timescale or retention or migration" -q`

Expected: FAIL because the US SQL artifacts do not exist yet.

**Step 3: Implement the schema and SQL helper**

In the new migration and `scripts/sql/us_candles_timescale.sql`:
- add a version guard for TimescaleDB `>= 2.15.0`
- create `public.us_candles_1m` with:
  - `time TIMESTAMPTZ NOT NULL`
  - `symbol TEXT NOT NULL`
  - `exchange TEXT NOT NULL`
  - `open/high/low/close/volume/value NUMERIC NOT NULL`
  - `CHECK (exchange IN ('NASD', 'NYSE', 'AMEX'))`
  - `UNIQUE (time, symbol, exchange)`
- convert it to a hypertable on `time`
- add an index on `(symbol, exchange, time DESC)`
- create continuous aggregates:
  - `public.us_candles_5m`
  - `public.us_candles_15m`
  - `public.us_candles_30m`
  - `public.us_candles_1h`

For aggregate definitions:
- `5m/15m/30m`: use straightforward `time_bucket` rollups on UTC minute rows
- `1h`: use the Timescale 2.15+ `time_bucket` origin/offset form that aligns bucket starts to `09:30 America/New_York` so buckets land on `09:30/10:30/11:30/12:30/13:30/14:30/15:30 ET`
- aggregate OHLC using `FIRST/MAX/MIN/LAST` and sum `volume/value`

For policies:
- mark all aggregates `timescaledb.materialized_only = false`
- add continuous aggregate refresh policies for each view
- add 90-day retention policies for `us_candles_1m` and all US aggregate views in the follow-up retention migration
- add a final manual refresh block similar to KR so operators can populate aggregates immediately after migration/backfill

**Step 4: Re-run the SQL tests**

Run: `uv run pytest tests/test_us_candles_sync.py -k "timescale or retention or migration" -q`

Expected: PASS.

### Task 3: Build the US sync service around held symbols and XNYS sessions

**Files:**
- Create: `app/services/us_candles_sync_service.py`
- Create: `tests/test_us_candles_sync.py`
- Reference: `app/services/kr_candles_sync_service.py`
- Reference: `app/services/us_symbol_universe_service.py`
- Reference: `app/services/manual_holdings_service.py`
- Reference: `app/services/brokers/kis/account.py`
- Reference: `tests/test_kr_candles_sync.py`

**Step 1: Write the failing service tests**

Add tests for these helpers and flows:
- symbol union combines:
  - current KIS US holdings from `ovrs_pdno`
  - manual holdings with `MarketType.US`
- manual holdings stay normalized through `to_db_symbol()` semantics (`BRK/B` -> `BRK.B`)
- exchange resolution uses `get_us_exchange_by_symbol(symbol, db=session)` so empty/missing/inactive cases reuse the exact existing sync-hint messages
- current minute gate skips when `xnys.is_trading_minute(now)` is false
- DST-aware closed-session selection returns UTC open/close windows for recent sessions
- incremental mode subtracts a 5-minute overlap from the saved cursor
- backward paging stops once the lower bound or session open is reached

**Step 2: Run the service tests and confirm they fail**

Run: `uv run pytest tests/test_us_candles_sync.py -k "symbol_union or overlap or trading_minute or closed_sessions" -q`

Expected: FAIL because the service does not exist yet.

**Step 3: Implement `app/services/us_candles_sync_service.py`**

Mirror the KR service shape, but keep these US-specific rules:
- use `_NY = ZoneInfo("America/New_York")`
- build the XNYS calendar once with `xcals.get_calendar("XNYS", side="left")`
- gate incremental runs with:

```python
now_utc = pd.Timestamp.now(tz="UTC").floor("min")
should_sync = xnys.is_trading_minute(now_utc)
```

- source symbols with:

```python
kis_holdings = await kis.fetch_my_us_stocks()
manual_holdings = await ManualHoldingsService(session).get_holdings_by_user(
    user_id=user_id,
    market_type=MarketType.US,
)
```

- do **not** query separate NYSE/AMEX holding pages; `fetch_my_us_stocks()` is already documented in-repo as the America-wide holdings wrapper
- resolve per-symbol exchange codes via `get_us_exchange_by_symbol(..., db=session)` and keep stored `exchange` values as `NASD/NYSE/AMEX`
- incremental mode:
  - read `MAX(time)` from `public.us_candles_1m` per `(symbol, exchange)`
  - subtract 5 minutes for overlap
  - set the lower bound to `max(cutoff_utc, session_open_utc)`
  - page backward with the new KIS method until the page is empty, `has_more` is false, or the next page would fall before the lower bound
- backfill mode:
  - use `minute_to_past_session(now, count=1)` plus `sessions_window(last_closed, -sessions)` to enumerate **closed** XNYS sessions only
  - page each session backward from `session_last_minute` to `session_first_minute`
- normalize each row to UTC and upsert into `public.us_candles_1m`
- return KR-style payload keys: `mode`, `sessions`, `skipped`, `skip_reasons`, `symbols_total`, `pairs_processed`, `rows_upserted`, `pages_fetched`

**Step 4: Re-run the focused service tests**

Run: `uv run pytest tests/test_us_candles_sync.py -k "symbol_union or overlap or trading_minute or closed_sessions" -q`

Expected: PASS.

### Task 4: Add thin job, task, and CLI entrypoints

**Files:**
- Create: `app/jobs/us_candles.py`
- Create: `app/tasks/us_candles_tasks.py`
- Modify: `app/tasks/__init__.py`
- Create: `scripts/sync_us_candles.py`
- Create: `tests/test_us_candles_sync.py`
- Reference: `tests/test_kr_candles_sync.py`

**Step 1: Add failing payload tests first**

Cover:
- job success payload wraps service result with `status="completed"`
- job failure payload returns `status="failed"` and `error`
- TaskIQ task `candles.us.sync` is scheduled every minute with `cron_offset="Asia/Seoul"`
- task returns failure payload on exception
- CLI exits `0` on completed payload and `1` on failed/crashed payload

**Step 2: Run the wrapper tests and confirm they fail**

Run: `uv run pytest tests/test_us_candles_sync.py -k "payload or task or script" -q`

Expected: FAIL because the wrappers do not exist yet.

**Step 3: Implement the wrappers**

- `app/jobs/us_candles.py`:

```python
async def run_us_candles_sync(*, mode: str, sessions: int = 10, user_id: int = 1) -> dict[str, object]:
    try:
        result = await sync_us_candles(mode=mode, sessions=sessions, user_id=user_id)
        return {"status": "completed", **result}
    except Exception as exc:
        logger.error("US candles sync failed: %s", exc, exc_info=True)
        return {"status": "failed", "mode": mode, "error": str(exc)}
```

- `app/tasks/us_candles_tasks.py`:

```python
@broker.task(
    task_name="candles.us.sync",
    schedule=[{"cron": "* * * * *", "cron_offset": "Asia/Seoul"}],
)
async def sync_us_candles_incremental_task() -> dict[str, object]:
    return await run_us_candles_sync(mode="incremental")
```

- register the task module in `app/tasks/__init__.py`
- `scripts/sync_us_candles.py` should follow the KR script exactly, but expose `--mode`, `--sessions`, and `--user-id`

**Step 4: Re-run the wrapper tests**

Run: `uv run pytest tests/test_us_candles_sync.py -k "payload or task or script" -q`

Expected: PASS.

### Task 5: Finish end-to-end test coverage and guardrails

**Files:**
- Create: `tests/test_us_candles_sync.py`
- Modify: `tests/test_services.py`
- Reference: `tests/test_kr_candles_sync.py`

**Step 1: Add the remaining issue-specific tests**

Ensure `tests/test_us_candles_sync.py` covers:
- union of KIS US holdings + manual `MarketType.US` holdings
- empty/missing/inactive `us_symbol_universe` failures with sync hint text
- XNYS market-open vs market-closed gating
- DST-aware recent-session selection
- early-close sessions (close at 13:00 ET, last trading minute at 12:59 ET)
- 5-minute overlap behavior
- KEYB stop conditions when the next page crosses the lower bound
- SQL helper and migration assertions for all four aggregates and retention policies

Ensure `tests/test_services.py` covers:
- exchange-code mapping (`NASD -> NAS`, `NYSE -> NYS`, `AMEX -> AMS`)
- empty payload handling
- malformed payload handling
- token refresh retry
- pagination cursor handling

**Step 2: Run the two focused suites**

Run:
- `uv run pytest tests/test_services.py -k "overseas_minute_chart" -q`
- `uv run pytest tests/test_us_candles_sync.py -q`

Expected: PASS.

### Task 6: Full verification and acceptance smoke flow

**Files:**
- Reference: `scripts/sql/us_candles_timescale.sql`
- Reference: `scripts/sync_us_candles.py`
- Reference: `alembic/versions/<revision>_add_us_candles_timescale.py`
- Reference: `alembic/versions/<revision>_add_us_candles_retention_policy.py`

**Step 1: Run repository verification**

Run:
- `make lint`
- `uv run pytest tests/test_services.py -k "overseas_minute_chart" -q`
- `uv run pytest tests/test_us_candles_sync.py -q`

Expected: exit code `0` for all commands.

**Step 2: Run the acceptance smoke flow from the issue**

Before the smoke flow, establish a concrete verification symbol:
- use the real KIS account or manual holdings data for the chosen `--user-id`
- confirm that the user has at least one US holding available to ingest
- record one actual target symbol as `US_TEST_SYMBOL`
- if the chosen user has no US holdings, either pick the correct user id or create a temporary manual `MarketType.US` holding before running the backfill

Run:
- `uv run alembic upgrade head`
- `make sync-us-symbol-universe`
- `uv run python scripts/sync_us_candles.py --mode backfill --sessions 3 --user-id 1`

**Step 3: Verify database state**

Run:

```sql
SELECT symbol, exchange, COUNT(*) AS rows, MIN(time) AS first_time, MAX(time) AS last_time
FROM public.us_candles_1m
WHERE symbol = :'US_TEST_SYMBOL'
GROUP BY symbol, exchange;

SELECT *
FROM public.us_candles_5m
WHERE symbol = :'US_TEST_SYMBOL'
ORDER BY bucket DESC
LIMIT 3;

SELECT *
FROM public.us_candles_1h
WHERE symbol = :'US_TEST_SYMBOL'
ORDER BY bucket DESC
LIMIT 3;
```

Expected:
- at least one held US symbol has rows in `public.us_candles_1m`
- at least one aggregate view returns rows for the same symbol
- `public.us_candles_1h.bucket` values land on `:30` ET starts, not naive UTC/KST top-of-hour boundaries

## Scope Guardrails

- Do **not** change `get_ohlcv(market="us")`.
- Do **not** add MCP read integration in this issue.
- Do **not** ingest pre-market / post-market / weekly-session codes (`BAY`, `BAQ`, `BAA`).
- Do **not** refactor the KR candle sync service into a shared base during this issue.
- Do **not** rely on `output1.next` headers or TaskIQ cron timing alone for session correctness; XNYS code gating is the source of truth.
