# ROB-830 get_holdings residual N+1s — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two remaining `get_holdings` fan-outs: resolve all crypto instrument IDs with one batched `IN` query and serve KR current-price enrichment from fresh `kr_candles_1d` rows before falling back to KIS.

**Architecture:** The crypto strategy-signal stage will resolve its complete symbol set once, then pass immutable `instrument_id` values through the existing indicator/cache-first call chain so neither candle reads nor write-back re-query `crypto_instruments`. The KR price stage will call the existing ROB-639/ROB-812 `cache_first_kr(symbol, 2)` reader and invoke the unchanged KIS quote helper only when the DB reader returns no usable fresh frame.

**Tech Stack:** Python 3.13, FastMCP handlers, SQLAlchemy async, PostgreSQL/TimescaleDB, pandas, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- TDD only: every production change follows a test that was observed failing for the intended reason.
- Return schema and values remain unchanged; only data acquisition changes from per-symbol lookup/live HTTP to batched/DB-first reads.
- Read path only. Order, preview, modify, cancel, reconcile, and journal paths are unchanged.
- migration-0: no Alembic revision and no schema/index/config change.
- Crypto instrument resolution is request-scoped. Do not add a process-global mutable cache or invalidation policy.
- Missing crypto instrument rows remain fail-open for strategy enrichment: the holding remains in the response without a fabricated `strategy_signal`.
- KR DB frames are accepted only through `cache_first_kr`, which already enforces sufficient row count, latest closed-session freshness, historical-query bypass, and the forming-bar rule. KIS remains the fallback on `None`, empty, invalid close, or DB/calendar error.
- Do not change `_fetch_quote_equity_kr` globally; the DB-first routing belongs only to the `get_holdings` price-refresh path.
- Do not update `app/mcp_server/README.md`: public tool parameters, defaults, response fields, and semantics do not change.

---

## Root-cause trace and current call sites

### Residual 1: `crypto_instruments` SELECT fan-out

The actual `get_holdings` chain on current `origin/main` (`6e5795e2`) is:

1. `app/mcp_server/tooling/portfolio_holdings.py:1126-1158` builds all crypto positions and runs `_compute_crypto_signals_for_position` once per position via `bounded_gather`.
2. `app/mcp_server/tooling/portfolio_holdings.py:259-290` calls `_fetch_ohlcv_for_indicators(symbol, "crypto", count=50)` for each position.
3. `app/mcp_server/tooling/market_data_indicators.py:90-97` dispatches crypto to `_cache_first_crypto`; `app/mcp_server/tooling/market_data_indicators.py:269-312` creates a separate session/repository per symbol, reads candles, and writes fetched rows back on a miss/stale cache.
4. `app/services/daily_candles/repository.py:427-449` resolves one instrument before every crypto `fetch_recent`.
5. `app/services/daily_candles/repository.py:192-213` also calls `_resolve_instrument_id` inside the row loop during crypto write-back. The SELECT itself is at `app/services/daily_candles/repository.py:169-190`.

Therefore the root cause is not the indicator math. The lookup boundary only accepts one symbol, so the holdings fan-out cannot share identity resolution and a stale/miss write-back may repeat the same static lookup for every returned candle row.

### Residual 2: KIS `inquire-daily-itemchartprice` fan-out

The actual chain is:

1. `app/mcp_server/tooling/portfolio_holdings.py:619-794` refreshes every KR equity pair; `fetch_equity_price` calls `_fetch_quote_equity_kr` at lines `710-723`.
2. `app/mcp_server/tooling/market_data_quotes.py:587-617` calls `KISClient.inquire_daily_itemchartprice(..., n=2)` and builds `price`, `previous_close`, OHLC, volume, and value.
3. `portfolio_holdings.py:715-723` consumes only `quote["price"]`; all other daily-candle enrichment fields are discarded by `get_holdings`.
4. `app/services/daily_candles/read_service.py:240-303` already provides the required DB-first contract, and `app/services/daily_candles/repository.py:70-92,427-500` contains ROB-812's bounded predicate.

The enrichment is therefore **KR current-price refresh**. It is not R:R, strategy scoring, or a retained previous-close enrichment.

---

## File Structure

- `app/services/daily_candles/repository.py` — add one-query crypto identity resolution plus direct-by-ID recent-read/write-back methods; preserve existing public paths by delegation.
- `app/mcp_server/tooling/market_data_indicators.py` — accept an optional pre-resolved crypto instrument ID and use the direct-by-ID repository methods.
- `app/mcp_server/tooling/portfolio_holdings.py` — batch-resolve the holdings crypto universe once; pass IDs to strategy-signal tasks; add KR DB-first price selection before the existing KIS fallback.
- `tests/services/daily_candles/test_repository_crypto_path.py` — prove multi-symbol identity resolution executes one `crypto_instruments` SELECT and preserves unknown-row behavior.
- `tests/mcp_server/tooling/test_get_holdings_n1_residuals.py` — regression-lock the actual holdings orchestration, strategy output, DB-hit result equivalence, and KIS fallback.
- `docs/plans/ROB-830-get-holdings-n1-residuals.md` — this plan and root-cause evidence.

---

### Task 1: Batch crypto instrument identity at the repository boundary

**Files:**
- Modify: `app/services/daily_candles/repository.py:169-230,427-469`
- Modify: `tests/services/daily_candles/test_repository_crypto_path.py`

**Interfaces:**
- Produces: `DailyCandlesRepository.resolve_crypto_instrument_ids(*, symbols: list[str], partition: str) -> dict[str, int]`.
- Produces: `DailyCandlesRepository.fetch_recent_crypto_by_instrument_id(*, instrument_id: int, symbol: str, partition: str, count: int) -> list[DailyCandleRow]`.
- Produces: `DailyCandlesRepository.upsert_crypto_rows_by_instrument_id(*, instrument_id: int, rows: list[DailyCandleRow]) -> int`.
- Preserves: `_resolve_instrument_id`, `fetch_recent`, and `upsert_rows` behavior for every existing caller.

- [ ] **Step 1: Write the failing one-query test**

Append to `tests/services/daily_candles/test_repository_crypto_path.py`. Change the existing SQLAlchemy import to `from sqlalchemy import event, text`, then install the event hook only after fixture rows are flushed so setup INSERTs are not counted.

```python
@pytest.mark.asyncio
async def test_resolve_crypto_instrument_ids_batches_symbols_in_one_select(
    db_session: AsyncSession,
) -> None:
    instruments = [
        CryptoInstrument(
            venue="upbit",
            product="spot",
            venue_symbol=symbol,
            base_asset=symbol.removeprefix("KRW-"),
            quote_asset="KRW",
            status="active",
        )
        for symbol in ("KRW-BTC", "KRW-ETH", "KRW-XRP")
    ]
    db_session.add_all(instruments)
    await db_session.flush()

    statements: list[str] = []
    engine = db_session.get_bind()

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        if "crypto_instruments" in statement and statement.lstrip().upper().startswith(
            "SELECT"
        ):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        resolved = await DailyCandlesRepository(
            session=db_session
        ).resolve_crypto_instrument_ids(
            symbols=["KRW-XRP", "KRW-BTC", "KRW-ETH", "KRW-BTC"],
            partition="upbit_krw",
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert resolved == {item.venue_symbol: item.id for item in instruments}
    assert len(statements) == 1
    assert " IN " in statements[0].upper()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/services/daily_candles/test_repository_crypto_path.py::test_resolve_crypto_instrument_ids_batches_symbols_in_one_select -v -p no:cacheprovider
```

Expected: FAIL with `AttributeError: 'DailyCandlesRepository' object has no attribute 'resolve_crypto_instrument_ids'`.

- [ ] **Step 3: Implement the batch resolver**

In `repository.py`, import `bindparam`, centralize the existing partition-to-venue conversion in a pure private helper, and use an expanding bind parameter:

```python
from sqlalchemy import bindparam, text


def _crypto_venue_for_partition(partition: str) -> str:
    return "upbit" if partition == "upbit_krw" else partition.split("_")[0]
```

```python
    async def resolve_crypto_instrument_ids(
        self, *, symbols: list[str], partition: str
    ) -> dict[str, int]:
        normalized = sorted({str(symbol).strip().upper() for symbol in symbols if symbol})
        if not normalized:
            return {}
        sql = text(
            "SELECT venue_symbol, id FROM crypto_instruments "
            "WHERE venue = :venue AND product = 'spot' "
            "AND venue_symbol IN :symbols"
        ).bindparams(bindparam("symbols", expanding=True))
        result = await self._session.execute(
            sql,
            {
                "venue": _crypto_venue_for_partition(partition),
                "symbols": normalized,
            },
        )
        return {str(row.venue_symbol): int(row.id) for row in result}
```

Change `_resolve_instrument_id` to call `resolve_crypto_instrument_ids` with a one-item list and retain its existing `LookupError` text. This keeps non-holdings callers compatible.

- [ ] **Step 4: Add direct-by-ID read and write methods**

Extract the existing crypto SQL/mapping body from `fetch_recent` into `fetch_recent_crypto_by_instrument_id`. Extract the INSERT body from `_upsert_crypto_rows` into `upsert_crypto_rows_by_instrument_id`. Neither method may query `crypto_instruments`.

```python
    async def fetch_recent_crypto_by_instrument_id(
        self,
        *,
        instrument_id: int,
        symbol: str,
        partition: str,
        count: int,
    ) -> list[DailyCandleRow]:
        sql = text(_CRYPTO_RECENT_SQL)
        result = await self._session.execute(
            sql,
            {
                "iid": int(instrument_id),
                "symbol": symbol,
                "partition": partition,
                "count": int(count),
                "time_floor": _recent_time_floor(int(count), now=datetime.now(UTC)),
            },
        )
        out: list[DailyCandleRow] = []
        for row in result.mappings().all():
            out.append(
                DailyCandleRow(
                    time_utc=row["time"],
                    symbol=row["symbol"],
                    partition=row["partition"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    adj_close=None,
                    volume=(
                        float(row["volume"]) if row["volume"] is not None else 0.0
                    ),
                    value=float(row["value"]) if row["value"] is not None else 0.0,
                    source=row["source"],
                )
            )
        return list(reversed(out))
```

Add the direct write method with the existing conflict policy unchanged:

```python
    async def upsert_crypto_rows_by_instrument_id(
        self, *, instrument_id: int, rows: list[DailyCandleRow]
    ) -> int:
        if not rows:
            return 0
        payload = [
            {
                "instrument_id": int(instrument_id),
                "time": row.time_utc,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "base_volume": row.volume,
                "quote_volume": row.value,
                "is_closed": True,
                "source": row.source,
            }
            for row in rows
        ]
        sql = text(
            """
            INSERT INTO public.crypto_candles_1d (
                instrument_id, time, open, high, low, close,
                base_volume, quote_volume, is_closed, source
            ) VALUES (
                :instrument_id, :time, :open, :high, :low, :close,
                :base_volume, :quote_volume, :is_closed, :source
            )
            ON CONFLICT (instrument_id, time) DO UPDATE
            SET open         = EXCLUDED.open,
                high         = EXCLUDED.high,
                low          = EXCLUDED.low,
                close        = EXCLUDED.close,
                base_volume  = EXCLUDED.base_volume,
                quote_volume = EXCLUDED.quote_volume,
                is_closed    = EXCLUDED.is_closed,
                source       = EXCLUDED.source,
                ingested_at  = now()
            WHERE
                NOT (public.crypto_candles_1d.is_closed = TRUE
                     AND EXCLUDED.is_closed = TRUE
                     AND public.crypto_candles_1d.source = EXCLUDED.source)
            """
        )
        result = cast(
            "_RowcountResult",
            cast(object, await self._session.execute(sql, payload)),
        )
        return max(int(result.rowcount or 0), 0)
```

`_upsert_crypto_rows` must resolve all unique `(symbol, partition)` identities before its payload loop, raise the same `LookupError` before INSERT if any identity is missing, and call the direct method for a single-identity list. For multi-identity lists, build one combined payload from the batch mapping and execute the same INSERT statement with executemany parameters.

- [ ] **Step 5: Add an unknown-symbol regression test**

```python
@pytest.mark.asyncio
async def test_resolve_crypto_instrument_ids_returns_only_known_symbols(
    db_session: AsyncSession,
) -> None:
    known = CryptoInstrument(
        venue="upbit",
        product="spot",
        venue_symbol="KRW-SOL",
        base_asset="SOL",
        quote_asset="KRW",
        status="active",
    )
    db_session.add(known)
    await db_session.flush()

    resolved = await DailyCandlesRepository(
        session=db_session
    ).resolve_crypto_instrument_ids(
        symbols=["KRW-SOL", "KRW-NOT-SEEDED"],
        partition="upbit_krw",
    )

    assert resolved == {"KRW-SOL": known.id}
```

- [ ] **Step 6: Verify GREEN and repository regressions**

Run:

```bash
uv run pytest tests/services/daily_candles/test_repository_crypto_path.py -v -p no:cacheprovider
```

Expected: all tests PASS, including the pre-existing unknown-pair `LookupError`, latest-time, ascending-order, and write-through contracts.

- [ ] **Step 7: Commit Task 1**

```bash
git add app/services/daily_candles/repository.py tests/services/daily_candles/test_repository_crypto_path.py
git commit -m "perf(ROB-830): batch crypto instrument identity resolution"
```

---

### Task 2: Resolve the holdings crypto universe once and preserve strategy output

**Files:**
- Modify: `app/mcp_server/tooling/market_data_indicators.py:90-97,269-312`
- Modify: `app/mcp_server/tooling/portfolio_holdings.py:259-290,1126-1158`
- Create: `tests/mcp_server/tooling/test_get_holdings_n1_residuals.py`

**Interfaces:**
- Produces: `_resolve_crypto_instrument_ids_for_holdings(positions: list[dict[str, Any]]) -> dict[str, int]` in `portfolio_holdings.py`.
- Changes: `_compute_crypto_signals_for_position(position, *, instrument_id: int) -> tuple[float | None, VotingResult | None]`.
- Extends compatibly: `_fetch_ohlcv_for_indicators(..., crypto_instrument_id: int | None = None)` and `_cache_first_crypto(..., instrument_id: int | None = None)`. All non-holdings callers retain the current fallback lookup by omitting the new keyword.

- [ ] **Step 1: Write the failing orchestration test**

Create `tests/mcp_server/tooling/test_get_holdings_n1_residuals.py` with a position builder and this test. Patch only collection, the new resolver, and signal calculation; exercise real `_get_holdings_impl` grouping/output logic.

```python
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import portfolio_holdings


def _crypto_position(symbol: str, profit_rate: float) -> dict[str, object]:
    price = 100_000.0
    return {
        "account": "upbit",
        "account_name": "Upbit Main",
        "broker": "upbit",
        "source": "upbit_api",
        "instrument_type": "crypto",
        "market": "crypto",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": price / (1.0 + profit_rate / 100.0),
        "current_price": price,
        "evaluation_amount": price,
        "profit_loss": price - price / (1.0 + profit_rate / 100.0),
        "profit_rate": profit_rate,
    }


@pytest.mark.asyncio
async def test_get_holdings_resolves_crypto_instruments_once_and_keeps_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [
        _crypto_position("KRW-BTC", -6.0),
        _crypto_position("KRW-ETH", 10.0),
    ]
    collect = AsyncMock(return_value=(positions, [], "crypto", "upbit"))
    resolve = AsyncMock(return_value={"KRW-BTC": 101, "KRW-ETH": 202})

    async def compute(position, *, instrument_id):
        assert instrument_id in {101, 202}
        return (50.0 if position["symbol"] == "KRW-ETH" else 35.0, None)

    monkeypatch.setattr(portfolio_holdings, "_collect_portfolio_positions", collect)
    monkeypatch.setattr(
        portfolio_holdings, "_resolve_crypto_instrument_ids_for_holdings", resolve
    )
    monkeypatch.setattr(
        portfolio_holdings, "_compute_crypto_signals_for_position", compute
    )

    result = await portfolio_holdings._get_holdings_impl(
        account="upbit", market="crypto", minimum_value=0
    )

    resolve.assert_awaited_once_with(positions)
    by_symbol = {
        row["symbol"]: row for row in result["accounts"][0]["positions"]
    }
    assert by_symbol["KRW-BTC"]["strategy_signal"] == {
        "action": "sell",
        "reason": "stop_loss",
        "threshold_pct": -4.5,
    }
    assert by_symbol["KRW-ETH"]["strategy_signal"] == {
        "action": "sell",
        "reason": "mean_reversion_exit",
        "rsi_14": 50.0,
    }
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py::test_get_holdings_resolves_crypto_instruments_once_and_keeps_signals -v -p no:cacheprovider
```

Expected: FAIL because `_resolve_crypto_instrument_ids_for_holdings` does not exist and `_compute_crypto_signals_for_position` does not accept `instrument_id`.

- [ ] **Step 3: Add request-scoped batch resolution**

In `portfolio_holdings.py`, use one `AsyncSessionLocal` and one repository call:

```python
async def _resolve_crypto_instrument_ids_for_holdings(
    positions: list[dict[str, Any]],
) -> dict[str, int]:
    symbols = sorted(
        {
            str(position.get("symbol") or "").strip().upper()
            for position in positions
            if position.get("instrument_type") == "crypto"
            and position.get("symbol")
        }
    )
    if not symbols:
        return {}
    async with AsyncSessionLocal() as session:
        repo = DailyCandlesRepository(session=session)
        return await repo.resolve_crypto_instrument_ids(
            symbols=symbols,
            partition="upbit_krw",
        )
```

Call it exactly once after `crypto_positions` is built. For a missing mapping, return `(None, None)` from the per-position closure without calling Upbit or issuing a second identity SELECT. This matches the current fail-open output: the holding stays present and has no strategy signal.

- [ ] **Step 4: Thread the resolved ID through the indicator cache path**

Add the optional keyword only at the generic indicator boundary:

```python
async def _fetch_ohlcv_for_indicators(
    symbol: str,
    market_type: str,
    count: int = 250,
    *,
    crypto_instrument_id: int | None = None,
) -> pd.DataFrame:
    if market_type == "crypto":
        return await _cache_first_crypto(
            symbol=symbol,
            count=count,
            instrument_id=crypto_instrument_id,
        )
```

When `_cache_first_crypto` receives an ID, it calls `fetch_recent_crypto_by_instrument_id` and `upsert_crypto_rows_by_instrument_id`. When it receives `None`, it retains the current `fetch_recent`/`upsert_rows` path for all other callers.

Change `_compute_crypto_signals_for_position` to require the ID and call:

```python
df = await _fetch_ohlcv_for_indicators(
    symbol,
    "crypto",
    count=50,
    crypto_instrument_id=instrument_id,
)
```

- [ ] **Step 5: Add a missing-instrument fail-open test**

```python
@pytest.mark.asyncio
async def test_get_holdings_missing_crypto_instrument_keeps_position_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = [_crypto_position("KRW-NOT-SEEDED", -6.0)]
    monkeypatch.setattr(
        portfolio_holdings,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(positions, [], "crypto", "upbit")),
    )
    monkeypatch.setattr(
        portfolio_holdings,
        "_resolve_crypto_instrument_ids_for_holdings",
        AsyncMock(return_value={}),
    )
    compute = AsyncMock()
    monkeypatch.setattr(
        portfolio_holdings, "_compute_crypto_signals_for_position", compute
    )

    result = await portfolio_holdings._get_holdings_impl(
        account="upbit", market="crypto", minimum_value=0
    )

    position = result["accounts"][0]["positions"][0]
    assert position["symbol"] == "KRW-NOT-SEEDED"
    assert "strategy_signal" not in position
    compute.assert_not_awaited()
```

- [ ] **Step 6: Verify GREEN and existing crypto strategy contracts**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py tests/test_mcp_portfolio_tools.py -k "crypto or strategy_signal or n1_residuals" -v -p no:cacheprovider
```

Expected: all selected tests PASS; stop-loss, mean-reversion, voting metadata, JSON-native scalar, snapshot-price reuse, and no-signal cases remain unchanged.

- [ ] **Step 7: Commit Task 2**

```bash
git add app/mcp_server/tooling/market_data_indicators.py app/mcp_server/tooling/portfolio_holdings.py tests/mcp_server/tooling/test_get_holdings_n1_residuals.py
git commit -m "perf(ROB-830): batch get_holdings crypto instrument lookup"
```

---

### Task 3: Make KR current-price enrichment DB-first with identical output

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py:619-794`
- Modify: `tests/mcp_server/tooling/test_get_holdings_n1_residuals.py`

**Interfaces:**
- Consumes: `cache_first_kr(symbol: str, count: int, end: datetime | None = None) -> DataFrame | None` from `app.services.daily_candles.read_service`.
- Preserves: `_fetch_price_map_for_positions(...)` tuple shape and all `get_holdings` response fields.
- Fallback: `_fetch_quote_equity_kr(symbol)` remains the only KIS daily HTTP path and runs only when DB cannot provide a valid close.

- [ ] **Step 1: Write the failing DB-hit and fallback equivalence tests**

Append:

```python
import pandas as pd


def _kr_refresh_position(symbol: str = "005930") -> dict[str, object]:
    return {
        "instrument_type": "equity_kr",
        "symbol": symbol,
        "source": "manual",
        "current_price": None,
        "evaluation_amount": None,
        "profit_loss": None,
        "profit_rate": None,
    }


@pytest.mark.asyncio
async def test_kr_price_enrichment_db_hit_matches_legacy_result_and_skips_kis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-07-09", periods=2, freq="D"),
            "open": [60_000.0, 61_000.0],
            "high": [62_000.0, 63_000.0],
            "low": [59_000.0, 60_000.0],
            "close": [61_500.0, 62_000.0],
            "volume": [1_000.0, 1_100.0],
            "value": [61_500_000.0, 68_200_000.0],
        }
    )
    db_read = AsyncMock(return_value=db_frame)
    kis_quote = AsyncMock(return_value={"price": 62_000.0})
    monkeypatch.setattr(portfolio_holdings, "cache_first_kr", db_read)
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", kis_quote)

    actual = await portfolio_holdings._fetch_price_map_for_positions(
        [_kr_refresh_position()]
    )

    assert actual == (
        {("equity_kr", "005930"): 62_000.0},
        [],
        {},
    )
    db_read.assert_awaited_once_with("005930", 2)
    kis_quote.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cached",
    [None, pd.DataFrame(), pd.DataFrame({"close": [None]})],
)
async def test_kr_price_enrichment_db_miss_falls_back_to_legacy_kis_result(
    monkeypatch: pytest.MonkeyPatch,
    cached: pd.DataFrame | None,
) -> None:
    db_read = AsyncMock(return_value=cached)
    kis_quote = AsyncMock(return_value={"price": 62_000.0})
    monkeypatch.setattr(portfolio_holdings, "cache_first_kr", db_read)
    monkeypatch.setattr(portfolio_holdings, "_fetch_quote_equity_kr", kis_quote)

    actual = await portfolio_holdings._fetch_price_map_for_positions(
        [_kr_refresh_position()]
    )

    assert actual == ({("equity_kr", "005930"): 62_000.0}, [], {})
    kis_quote.assert_awaited_once_with("005930")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py -k "kr_price_enrichment" -v -p no:cacheprovider
```

Expected: FAIL because `portfolio_holdings` has no `cache_first_kr` binding and the current path awaits `_fetch_quote_equity_kr` unconditionally. Both the DB-hit contract and every fallback shape fail for that intended reason.

- [ ] **Step 3: Implement DB-first selection with KIS fallback**

Import `cache_first_kr` from `app.services.daily_candles.read_service`. In the KR branch of nested `fetch_equity_price`, accept only a numeric positive final close:

```python
        if instrument_type == "equity_kr":
            try:
                cached = await cache_first_kr(symbol, 2)
                if cached is not None and not cached.empty and "close" in cached.columns:
                    cached_price = _to_optional_float(cached["close"].iloc[-1])
                    if cached_price is not None and cached_price > 0:
                        return instrument_type, symbol, cached_price, None, "db"
            except Exception:
                logger.debug(
                    "KR daily DB enrichment failed for %s; falling back to KIS",
                    symbol,
                    exc_info=True,
                )
            try:
                quote = await _fetch_quote_equity_kr(symbol)
                price = quote.get("price")
                return (
                    instrument_type,
                    symbol,
                    float(price) if price is not None else None,
                    None,
                    "kis",
                )
            except Exception as exc:
                error_msg = str(exc)
                logger.debug(
                    "Failed to fetch equity price for %s: %s", symbol, error_msg
                )
                return instrument_type, symbol, None, error_msg, "kis"
```

The `source="db"` value is internal and never emitted on successful holdings responses. Error source remains `kis`, matching the existing contract.

- [ ] **Step 4: Verify GREEN for DB hit and all fallback shapes**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py -k "kr_price_enrichment" -v -p no:cacheprovider
```

Expected: PASS for the DB-hit case and all three parametrized DB-miss/invalid-close cases. The hit case proves KIS is not called; every fallback case proves the legacy KIS price tuple is unchanged.

- [ ] **Step 5: Verify Task 3 and related price-refresh regressions**

Run:

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py tests/test_mcp_portfolio_tools.py -k "fetch_price_map or current_price or n1_residuals" -v -p no:cacheprovider
```

Expected: all selected tests PASS. US KIS-primary/Yahoo-fallback behavior and crypto batched ticker prices remain unchanged.

- [ ] **Step 6: Commit Task 3**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py tests/mcp_server/tooling/test_get_holdings_n1_residuals.py
git commit -m "perf(ROB-830): use DB-first KR holdings enrichment"
```

---

### Task 4: Verify scope, migration-0, and quality gates

**Files:**
- Verify only: all files changed in Tasks 1-3.

**Interfaces:**
- Produces: fresh test/lint evidence and a reviewable migration-free diff.

- [ ] **Step 1: Run the focused ROB-830 suite**

```bash
uv run pytest tests/mcp_server/tooling/test_get_holdings_n1_residuals.py tests/services/daily_candles/test_repository_crypto_path.py -v -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 2: Run the related MCP and candle suites**

```bash
uv run pytest tests/test_mcp_portfolio_tools.py tests/unit/mcp_server/tooling/test_market_data_indicators_cache_first.py tests/test_mcp_indicator_tools.py tests/services/daily_candles -v -m "not live" -p no:cacheprovider
```

Expected: all selected non-live tests PASS.

- [ ] **Step 3: Run the repository lint gate**

```bash
make lint
```

Expected: Ruff check, Ruff format check, and `ty check app/ --error-on-warning` all exit 0.

- [ ] **Step 4: Prove migration-0 and order-path isolation**

```bash
git diff --name-only origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- alembic app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/orders_kis_variants.py app/mcp_server/tooling/orders_modify_cancel.py
```

Expected:

- no path under `alembic/`;
- no order-path diff output;
- only the plan, three read-path production files, and two test files listed.

- [ ] **Step 5: Re-read the requirements against the diff**

Confirm from code and tests:

- one `IN` SELECT resolves all crypto holding symbols;
- a resolved ID is reused by both crypto candle read and write-back;
- missing IDs leave holdings present without a fabricated signal;
- fresh/sufficient `kr_candles_1d` data skips KIS;
- DB miss/stale/error invokes KIS and returns the same price tuple;
- `get_holdings` arguments and response schema are unchanged;
- no order path, migration, config, or MCP documentation change exists.

- [ ] **Step 6: Commit the plan if it was not committed before execution**

```bash
git add docs/plans/ROB-830-get-holdings-n1-residuals.md
git commit -m "docs(ROB-830): plan get_holdings residual N+1 fixes"
```

---

## PR handoff

After every task is complete and the fresh verification gate is green, use the repository ship workflow to create a PR against `main`. The PR summary must name both measured removals, include the focused test commands and `make lint`, state `migration-0`, and state that order paths are unchanged. Stop after PR creation; do not merge.
