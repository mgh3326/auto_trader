# ROB-696 — /invest Price Fallback (KIS → Toss → snapshot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Make `/invest` current-price fetching resilient to a KIS outage (like the 2026-07-04 maintenance window that blanked ALL US + KR prices). Today `InvestQuoteService.fetch_kr_prices` / `fetch_us_prices` are KIS-single-source: any per-symbol KIS error yields `None` and floods a per-symbol WARN log. This plan adds a fail-open, per-market fallback chain — **KIS (unchanged primary) → Toss batch → last-known `market_quote_snapshots` close → None** — behind a pure, injectable `PriceFallbackResolver` that is fully unit-testable with fakes (no network, no DB). The public method contract stays exactly `dict[str, float | None]`; the price source is logged internally only.

**Architecture:**

*Current (KIS-only) flow.* `app/routers/invest_api.py:170` builds `InvestQuoteService(kis_client, db)` (`get_invest_home_service`, `invest_api.py:154`). `ManualHomeReader` consumes it: `invest_home_readers.py:761` `await self._quote_service.fetch_kr_prices(kr_tickers)` and `:769` `fetch_us_prices(us_tickers)` (typed `dict[str, float | None]` at `:751-752`).
- `InvestQuoteService.__init__` (`invest_quote_service.py:23`) sets `self._market_data = MarketDataClient(kis_client)` (`:26`).
- `fetch_kr_prices` (`:28-49`): `asyncio.gather` over per-symbol `_fetch` → `self._market_data.inquire_price(symbol, market="J")` (`:39`) → `float(df.iloc[0]["close"])`; on `Exception` logs a per-symbol WARN (`:45`) and sets `None`.
- `fetch_us_prices` (`:51-83`): per-symbol → `get_us_exchange_by_symbol(symbol, self._db)` (`:62`) then `self._market_data.inquire_overseas_daily_price(symbol, exchange_code=exchange, n=1, period="D")` (`:71`) → `float(df.iloc[0]["close"])`; per-symbol WARNs at `:64` and `:79`.
- When KIS is down every `_fetch` throws → every symbol `None` → `/invest` prices blank, and one WARN **per symbol** spams the log.

*Target (KIS → Toss → snapshot) flow.* A new pure orchestrator `PriceFallbackResolver.resolve(symbols)` runs three injected async fetchers in order, merging only non-`None` values and shrinking the "still-missing" set at each layer, returning `None` for whatever remains:
1. **KIS** — the existing per-symbol gather, moved verbatim into `_kis_fetch_kr` / `_kis_fetch_us` (byte-identical happy path; still calls `inquire_price(..., market="J")` / `inquire_overseas_daily_price(..., n=1, period="D")`). Per-symbol errors become silent (debug); the resolver emits ONE summary log per layer.
2. **Toss batch** — for the symbols KIS returned `None`, ONE (chunked ≤200) `TossReadClient.prices(symbols)` call, gated by `settings.toss_api_enabled`. Toss covers KR + US. Mirrors the `toss_symbol_master_service.py:231` precedent (`{row.symbol: row for row in await client.prices(batch)}`); the price field is `TossPrice.last_price` (`Decimal`, `dto.py:42`) → `float`.
3. **Snapshot** — for still-missing symbols, last-known close from `market_quote_snapshots` via a new `MarketQuoteSnapshotsRepository.latest_prices(market, symbols)` read helper (DISTINCT-ON latest `snapshot_at` per symbol, any source).
4. Still missing → `None` (unchanged).

`InvestQuoteService(kis_client, db, toss_client=None)` stays backward-compatible: the existing 2-arg construction at `invest_api.py:170` is untouched; `toss_client` is an optional 3rd param (default `None`), lazily built from `TossReadClient.from_settings()` only when `settings.toss_api_enabled`, and skipped entirely when Toss is disabled (behavior === today: KIS → snapshot → None).

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), SQLAlchemy async (PostgreSQL, `DISTINCT ON`), httpx, pandas. Toss Open API `GET /api/v1/prices` (MARKET_DATA group, 10 TPS). No LLM, no broker mutation.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **Fail-open at EVERY layer.** Toss disabled/errors → fall through to snapshot; snapshot missing/errors → `None`. Never raise out of `fetch_kr_prices` / `fetch_us_prices`. Each fetcher call is wrapped so any exception degrades to `{}` for that layer (logged once, not re-raised). **Toss client *construction* is itself fail-open:** `TossReadClient.from_settings()` raises `TossMissingCredentials` / `TossApiDisabled` (`app/services/brokers/toss/auth.py:80,83,106,111`) when `toss_api_enabled=True` but credentials are empty/missing, so the construction in `_build_toss_fetch` MUST be wrapped — a construction failure degrades to the Toss-skipped path `(None, None)` (logged once), never propagating out of `fetch_*`.
- **Backward-compatible.** `InvestQuoteService`'s existing `(kis_client, db)` construction (`invest_api.py:170`) must keep working; `toss_client` is an optional 3rd param (default `None`). Public method return type is unchanged: `dict[str, float | None]` keyed by every input symbol.
- **Migration-0.** `market_quote_snapshots` (`app/models/market_quote_snapshot.py`) already exists; no schema change, no alembic revision, no new column.
- **Read-only.** All calls are GETs (KIS inquire, Toss `/api/v1/prices`, snapshot `SELECT`). No broker/order/watch/order-intent mutation anywhere in this change.
- **Toss gated by `settings.toss_api_enabled` (`TOSS_API_ENABLED`, `config.py:243`, default `False`).** When off, behavior === today (KIS → snapshot → None, Toss layer skipped — no `TossReadClient` is ever constructed).
- **Do NOT change the KIS primary path behavior when KIS succeeds.** When KIS returns a price for every symbol, the result is byte-identical to today and neither Toss nor snapshot is consulted (the missing-set is empty → early return).
- **Toss batch: respect the 1..200 batch limit** (`TossReadClient._symbols_param`, `client.py:138-142` raises outside 1..200). Chunk larger lists (`_BATCH_SIZE = 200`, mirroring `toss_symbol_master_service.py:19,56`); ONE batch call per chunk for many symbols, never per-symbol.
- **Reduce log noise.** Replace the current per-symbol WARN spam (`invest_quote_service.py:45,64,79`) with at most ONE summary-level log per fallback layer (e.g. "kis resolved 3/12; 9 missing"), not one WARN per symbol.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|-----------------------|
| `app/services/market_quote_snapshots/repository.py` | Modify | Task 1 — add `latest_prices(market, symbols) -> dict[str, float]` read helper (DISTINCT-ON latest close per symbol). |
| `app/services/invest_price_fallback.py` | Create | Task 2 — `PriceFallbackResolver` (pure KIS→Toss→snapshot orchestration over injected fetchers). Task 3 — `fetch_toss_batch_prices(client, symbols)` (chunked, fail-open Toss batch adapter). |
| `app/services/invest_quote_service.py` | Modify | Task 4 — `__init__(..., toss_client=None)`; move KIS gather into `_kis_fetch_kr`/`_kis_fetch_us`; `_snapshot_latest`; lazy Toss client; wire `PriceFallbackResolver` into `fetch_kr_prices`/`fetch_us_prices`; drop per-symbol WARN. |
| `tests/test_market_quote_snapshots_latest_prices.py` | Create | Task 1 tests (db_session; seed via `MarketQuoteSnapshotsRepository.upsert`). |
| `tests/test_price_fallback_resolver.py` | Create | Task 2 tests (pure fakes; no network/DB). |
| `tests/test_fetch_toss_batch_prices.py` | Create | Task 3 tests (fake Toss client; chunking + fail-open). |
| `tests/test_invest_quote_service.py` | Modify | Task 4 tests (append fallback-chain cases; keep the 2 existing happy-path tests green). |

> **NOT touched:**
> - `app/routers/invest_api.py:170` — `InvestQuoteService(kis_client, db)` stays a 2-arg call (backward-compat proves the optional param).
> - `MarketDataClient` internals (`app/services/brokers/kis/market_data.py` + `domestic_market_data.py:191` `inquire_price` / `overseas_market_data.py:212` `inquire_overseas_daily_price`) — consumed unchanged; the KIS happy path is byte-identical.
> - `TossReadClient` (`app/services/brokers/toss/client.py`), `parse_prices`/`TossPrice` (`dto.py:38-43,120`), the Toss auth/rate-limiter/transport — consumed read-only, no edits.
> - Any order/broker/watch/order-intent path — this is a read-only price surface.
> - `market_quote_snapshots` schema/models — read-only helper only, migration-0.

---

## Task 1 — Snapshot read helper: latest close by symbol (migration-0)

**Files:**
- Modify `app/services/market_quote_snapshots/repository.py` — add `MarketQuoteSnapshotsRepository.latest_prices` after `existing_keys` (`:108-139`). Imports `MarketQuoteSnapshot` (`:14`), `select`/`func` (`:10`) already present.
- Test (create) `tests/test_market_quote_snapshots_latest_prices.py`.

**Why here:** `MarketQuoteSnapshotsRepository` (`:50`) is the canonical read/write owner for this table; it already has `upsert`/`coverage_counts`/`existing_keys` but **no** "latest close per symbol" read. The action-readiness read at `app/services/invest_view_model/action_readiness_service.py:359-366` (`sa.func.max(MarketQuoteSnapshot.snapshot_at)` filtered by `market`+`symbol`) is the single-symbol precedent; this helper generalizes it to a symbol list returning the price at the latest `snapshot_at` per symbol (any `source`). Write path normalizes `symbol.upper()` / `market.lower()` (`:43-45`), so the read must upper the query symbols and lower the market.

**Interfaces:**
```python
async def latest_prices(
    self, market: str, symbols: list[str]
) -> dict[str, float]:
    """Latest close per symbol from market_quote_snapshots (any source).

    Keyed by the stored (uppercased) symbol. Symbols with no snapshot are
    absent from the result. Read-only; used as the KIS→Toss fallback's last hop.
    """
```
- Consumes `MarketQuoteSnapshot` columns `market`, `symbol`, `snapshot_at`, `price` (`market_quote_snapshot.py:47-53`, `price` is `Numeric(20,6)` → `Decimal` → `float`).
- PostgreSQL `select(...).distinct(MarketQuoteSnapshot.symbol).order_by(MarketQuoteSnapshot.symbol, MarketQuoteSnapshot.snapshot_at.desc())` (DISTINCT ON) over `market == market.lower()` AND `symbol.in_(upper_symbols)`.

Steps:

- [ ] **Write failing test — latest close per symbol, missing absent, market-scoped.** Create `tests/test_market_quote_snapshots_latest_prices.py`:
```python
from __future__ import annotations

import datetime as dt

import pytest

from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
    MarketQuoteSnapshotUpsert,
)

# DB-backed helper test — uses the real-PostgreSQL ``db_session`` fixture (DISTINCT
# ON is Postgres-only). Marked bare ``asyncio`` to match the neighbouring
# snapshot-repository tests in ``tests/test_invest_coverage_valuation.py`` (they
# are NOT tagged ``unit``); ``make test-unit`` (``-m "not integration and not
# live"``) still runs it.
pytestmark = [pytest.mark.asyncio]


async def _seed(db_session):
    now = dt.datetime.now(dt.UTC)
    repo = MarketQuoteSnapshotsRepository(db_session)
    await repo.upsert(
        [
            # 005930: two rows — the later snapshot_at (70500) must win
            MarketQuoteSnapshotUpsert(
                market="kr", symbol="005930", source="kis",
                snapshot_at=now - dt.timedelta(hours=3), price="69000",
            ),
            MarketQuoteSnapshotUpsert(
                market="kr", symbol="005930", source="naver_finance",
                snapshot_at=now - dt.timedelta(minutes=5), price="70500",
            ),
            # 034020: single row
            MarketQuoteSnapshotUpsert(
                market="kr", symbol="034020", source="kis",
                snapshot_at=now - dt.timedelta(minutes=1), price="18000",
            ),
            # AAPL is US — must NOT leak into a kr query
            MarketQuoteSnapshotUpsert(
                market="us", symbol="AAPL", source="yahoo",
                snapshot_at=now, price="222.5",
            ),
        ]
    )
    await db_session.commit()
    return repo


async def test_latest_prices_returns_latest_close_per_symbol(db_session):
    repo = await _seed(db_session)
    out = await repo.latest_prices("kr", ["005930", "034020"])
    assert out == pytest.approx({"005930": 70500.0, "034020": 18000.0})


async def test_latest_prices_omits_symbols_without_snapshot(db_session):
    repo = await _seed(db_session)
    out = await repo.latest_prices("kr", ["005930", "999999"])
    assert set(out) == {"005930"}


async def test_latest_prices_is_market_scoped(db_session):
    repo = await _seed(db_session)
    # AAPL only exists under market="us"; a kr query must not find it
    assert await repo.latest_prices("kr", ["AAPL"]) == {}
    assert await repo.latest_prices("us", ["AAPL"]) == pytest.approx({"AAPL": 222.5})


async def test_latest_prices_empty_symbols_returns_empty(db_session):
    repo = await _seed(db_session)
    assert await repo.latest_prices("kr", []) == {}
```

- [ ] **Run it — fails.** `uv run pytest tests/test_market_quote_snapshots_latest_prices.py -v`
  Expected: `AttributeError: 'MarketQuoteSnapshotsRepository' object has no attribute 'latest_prices'`.

- [ ] **Minimal impl — add `latest_prices`.** In `app/services/market_quote_snapshots/repository.py`, append to `MarketQuoteSnapshotsRepository` (after `existing_keys`, `:139`):
```python
    async def latest_prices(
        self, market: str, symbols: list[str]
    ) -> dict[str, float]:
        """ROB-696 — latest close per symbol (any source) for the KIS→Toss→
        snapshot fallback's last hop. Read-only; missing symbols are absent."""
        if not symbols:
            return {}
        upper = [s.strip().upper() for s in symbols if s.strip()]
        if not upper:
            return {}
        stmt = (
            select(MarketQuoteSnapshot.symbol, MarketQuoteSnapshot.price)
            .where(
                MarketQuoteSnapshot.market == market.strip().lower(),
                MarketQuoteSnapshot.symbol.in_(upper),
            )
            .distinct(MarketQuoteSnapshot.symbol)
            .order_by(
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.snapshot_at.desc(),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return {row.symbol: float(row.price) for row in rows}
```
(`select` is already imported at `:10`; `MarketQuoteSnapshot` at `:14`.)

- [ ] **Run it — passes.** `uv run pytest tests/test_market_quote_snapshots_latest_prices.py -v` → 4 passed.

- [ ] **Regression — repository consumers unaffected.** `uv run pytest tests/test_invest_coverage_valuation.py -v` → all pass (only an additive method).

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-696): MarketQuoteSnapshotsRepository.latest_prices read helper (migration-0)"`

---

## Task 2 — PriceFallbackResolver: pure KIS→Toss→snapshot chain (no network/DB)

**Files:**
- Create `app/services/invest_price_fallback.py` — `PriceFallbackResolver`.
- Test (create) `tests/test_price_fallback_resolver.py`.

**Why here:** the resolver is pure orchestration over three injected async fetchers with zero broker/DB/view-model dependency, so it lives in its own module `app/services/invest_price_fallback.py` (alongside its only consumer `invest_quote_service.py`, decoupled from `invest_view_model`). Fully unit-testable with fakes.

**Interfaces:**
```python
from collections.abc import Awaitable, Callable

PriceMap = dict[str, float | None]
Fetcher = Callable[[list[str]], Awaitable[PriceMap]]


class PriceFallbackResolver:
    def __init__(
        self,
        *,
        kis_fetch: Fetcher,
        toss_fetch: Fetcher | None,   # None => Toss layer skipped (disabled)
        snapshot_fetch: Fetcher,
        market: str,                  # "kr"/"us" — for log context only
    ) -> None: ...

    async def resolve(self, symbols: list[str]) -> PriceMap:
        """KIS → Toss → snapshot → None. Every input symbol is a key.
        Each layer only runs for symbols still None; each layer is wrapped
        fail-open (exception → {} for that layer, logged once)."""
```
- Contract: result has **exactly** the input symbols as keys; a value is the first non-`None` price found across layers, else `None`. When KIS resolves everything, `toss_fetch`/`snapshot_fetch` are never awaited (byte-identical happy path). One summary log per attempted layer; no per-symbol logging.

Steps:

- [ ] **Write failing test — chain order, short-circuit, fail-open, log-once.** Create `tests/test_price_fallback_resolver.py`:
```python
from __future__ import annotations

import pytest

from app.services.invest_price_fallback import PriceFallbackResolver

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _fetcher(mapping, *, calls=None, boom=False):
    async def _f(symbols):
        if calls is not None:
            calls.append(list(symbols))
        if boom:
            raise RuntimeError("layer down")
        return {s: mapping.get(s) for s in symbols}

    return _f


async def test_kis_success_skips_toss_and_snapshot():
    toss_calls, snap_calls = [], []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"005930": 70000.0, "034020": 18000.0}),
        toss_fetch=_fetcher({}, calls=toss_calls),
        snapshot_fetch=_fetcher({}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["005930", "034020"])
    assert out == pytest.approx({"005930": 70000.0, "034020": 18000.0})
    assert toss_calls == []   # never consulted
    assert snap_calls == []


async def test_toss_fills_only_the_kis_misses():
    toss_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": None, "C": None}),
        toss_fetch=_fetcher({"B": 20.0}, calls=toss_calls),  # C stays missing
        snapshot_fetch=_fetcher({"C": 30.0}),
        market="us",
    )
    out = await resolver.resolve(["A", "B", "C"])
    assert out == pytest.approx({"A": 10.0, "B": 20.0, "C": 30.0})
    assert toss_calls == [["B", "C"]]   # only KIS misses, batched once


async def test_toss_disabled_falls_through_to_snapshot():
    snap_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": None}),
        toss_fetch=None,                      # disabled
        snapshot_fetch=_fetcher({"A": 99.0}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["A"])
    assert out == pytest.approx({"A": 99.0})
    assert snap_calls == [["A"]]


async def test_all_layers_fail_open_to_none_without_raising():
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({}, boom=True),       # KIS outage
        toss_fetch=_fetcher({}, boom=True),      # Toss also down
        snapshot_fetch=_fetcher({}, boom=True),  # snapshot query errors
        market="us",
    )
    out = await resolver.resolve(["A", "B"])
    assert out == {"A": None, "B": None}         # never raises


async def test_empty_input_returns_empty():
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({}), toss_fetch=None,
        snapshot_fetch=_fetcher({}), market="kr",
    )
    assert await resolver.resolve([]) == {}


async def test_snapshot_only_runs_for_still_missing():
    snap_calls = []
    resolver = PriceFallbackResolver(
        kis_fetch=_fetcher({"A": 10.0, "B": None}),
        toss_fetch=_fetcher({"B": None}),       # Toss has nothing for B
        snapshot_fetch=_fetcher({"B": 5.0}, calls=snap_calls),
        market="kr",
    )
    out = await resolver.resolve(["A", "B"])
    assert out == pytest.approx({"A": 10.0, "B": 5.0})
    assert snap_calls == [["B"]]                # A already resolved by KIS
```

- [ ] **Run it — fails.** `uv run pytest tests/test_price_fallback_resolver.py -v`
  Expected: `ModuleNotFoundError: No module named 'app.services.invest_price_fallback'`.

- [ ] **Minimal impl — create the resolver.** Create `app/services/invest_price_fallback.py`:
```python
"""ROB-696 — fail-open price fallback chain for /invest (KIS → Toss → snapshot)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

PriceMap = dict[str, float | None]
Fetcher = Callable[[list[str]], Awaitable[PriceMap]]


class PriceFallbackResolver:
    """Pure orchestration: run injected fetchers KIS → Toss → snapshot, merge
    only non-None values, shrink the missing-set each layer, None for the rest.
    Every layer is wrapped fail-open (exception → {} for that layer)."""

    def __init__(
        self,
        *,
        kis_fetch: Fetcher,
        toss_fetch: Fetcher | None,
        snapshot_fetch: Fetcher,
        market: str,
    ) -> None:
        self._kis_fetch = kis_fetch
        self._toss_fetch = toss_fetch
        self._snapshot_fetch = snapshot_fetch
        self._market = market

    async def resolve(self, symbols: list[str]) -> PriceMap:
        if not symbols:
            return {}
        results: PriceMap = dict.fromkeys(symbols, None)

        await self._apply_layer("kis", self._kis_fetch, symbols, results)
        missing = self._missing(symbols, results)
        if not missing:
            return results

        if self._toss_fetch is not None:
            await self._apply_layer("toss", self._toss_fetch, missing, results)
            missing = self._missing(symbols, results)
            if not missing:
                return results

        await self._apply_layer("snapshot", self._snapshot_fetch, missing, results)
        return results

    async def _apply_layer(
        self, name: str, fetch: Fetcher, symbols: list[str], results: PriceMap
    ) -> None:
        try:
            fetched = await fetch(symbols)
        except Exception as exc:  # noqa: BLE001 — fail-open per layer
            logger.warning(
                "invest price fallback: %s layer failed for market=%s (%d symbols): %s",
                name, self._market, len(symbols), exc,
            )
            return
        resolved = 0
        for sym in symbols:
            price = fetched.get(sym)
            if price is not None and results.get(sym) is None:
                results[sym] = price
                resolved += 1
        logger.info(
            "invest price fallback: %s resolved %d/%d for market=%s",
            name, resolved, len(symbols), self._market,
        )

    @staticmethod
    def _missing(symbols: list[str], results: PriceMap) -> list[str]:
        return [s for s in symbols if results.get(s) is None]
```

- [ ] **Run it — passes.** `uv run pytest tests/test_price_fallback_resolver.py -v` → 6 passed.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-696): PriceFallbackResolver KIS→Toss→snapshot chain (pure, fail-open)"`

---

## Task 3 — Toss batch price adapter (chunked, fail-open, fake client)

**Files:**
- Modify `app/services/invest_price_fallback.py` — add module fn `fetch_toss_batch_prices(client, symbols)`.
- Test (create) `tests/test_fetch_toss_batch_prices.py`.

**Why here / symbol mapping:** mirrors the shipped precedent at `toss_symbol_master_service.py:227-236` — `_chunks(symbols, 200)` then `{row.symbol: row for row in await client.prices(batch)}` — which already sends **DB symbols directly** (KR 6-digit codes and US tickers, uppercased via `_resolve_symbols` `:71`) for BOTH markets and keys the response by `TossPrice.symbol`. So `/api/v1/prices` uses the DB symbol as-is; **no `.`↔`/`↔`-` conversion is applied** (the master sync round-trips US symbols this way in production). The adapter uppercases the request and matches responses case-insensitively so it returns keys equal to the caller's requested symbols. The price is `TossPrice.last_price` (`Decimal`, `dto.py:42`) → `float`.

> **Operator smoke follow-up (documented, not code):** confirm a dotted US ticker like `BRK.B` and a KR 6-digit code round-trip through `GET /api/v1/prices` under a live `TOSS_API_ENABLED` credential. If Toss ever normalizes the echoed `symbol` differently from the request, the case-insensitive match still keys by request order; only a *different spelling* would miss — surfaced as a snapshot fallthrough, never a crash.

**Interfaces:**
```python
async def fetch_toss_batch_prices(
    client: TossPriceClient, symbols: list[str]
) -> dict[str, float | None]:
    """ONE batched Toss /api/v1/prices call per ≤200 chunk. Fail-open: any
    error (disabled/network/parse) → {} so the resolver falls through to
    snapshot. Keyed by the caller's requested symbol; unseen symbols absent."""
```
- `TossPriceClient` = a minimal `Protocol` with `async def prices(self, symbols) -> list[TossPrice]` (structurally satisfied by `TossReadClient.prices`, `client.py:161`). Keeps the adapter test-injectable with a fake — no `TossReadClient` construction, no network.
- Consumes `TossPrice.symbol` / `TossPrice.last_price` (`dto.py:40,42`).
- `_TOSS_PRICE_BATCH = 200` (mirrors `TossReadClient._symbols_param` 1..200 guard, `client.py:140`).

Steps:

- [ ] **Write failing test — one batch call, Decimal→float, chunking, fail-open.** Create `tests/test_fetch_toss_batch_prices.py`:
```python
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import TossPrice
from app.services.invest_price_fallback import fetch_toss_batch_prices

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeToss:
    def __init__(self, prices, *, boom=False):
        self._prices = prices          # {symbol: last_price}
        self._boom = boom
        self.calls: list[list[str]] = []

    async def prices(self, symbols):
        self.calls.append(list(symbols))
        if self._boom:
            raise RuntimeError("toss down")
        return [
            TossPrice(symbol=s, timestamp=None,
                      last_price=Decimal(str(self._prices[s])), currency="KRW")
            for s in symbols if s in self._prices
        ]


async def test_returns_float_prices_keyed_by_symbol():
    client = _FakeToss({"005930": "70500", "034020": "18000"})
    out = await fetch_toss_batch_prices(client, ["005930", "034020"])
    assert out == pytest.approx({"005930": 70500.0, "034020": 18000.0})
    assert client.calls == [["005930", "034020"]]   # ONE batch, not per-symbol


async def test_symbols_missing_from_toss_are_absent():
    client = _FakeToss({"005930": "70500"})
    out = await fetch_toss_batch_prices(client, ["005930", "999999"])
    assert set(out) == {"005930"}


async def test_chunks_over_200_into_multiple_batches():
    symbols = [f"S{i:04d}" for i in range(201)]
    client = _FakeToss({s: "1" for s in symbols})
    out = await fetch_toss_batch_prices(client, symbols)
    assert len(out) == 201
    assert [len(c) for c in client.calls] == [200, 1]   # 1..200 batch limit


async def test_empty_symbols_makes_no_call():
    client = _FakeToss({})
    assert await fetch_toss_batch_prices(client, []) == {}
    assert client.calls == []


async def test_error_is_fail_open_returns_empty():
    client = _FakeToss({"005930": "70500"}, boom=True)
    assert await fetch_toss_batch_prices(client, ["005930"]) == {}   # no raise


async def test_case_insensitive_match_returns_requested_symbol_key():
    # Toss echoes upper; requested key is preserved for the resolver
    client = _FakeToss({"AAPL": "222.5"})
    out = await fetch_toss_batch_prices(client, ["aapl"])
    assert out == pytest.approx({"aapl": 222.5})
```

- [ ] **Run it — fails.** `uv run pytest tests/test_fetch_toss_batch_prices.py -v`
  Expected: `ImportError: cannot import name 'fetch_toss_batch_prices'`.

- [ ] **Minimal impl — add the adapter.** Append to `app/services/invest_price_fallback.py`:
```python
from typing import Protocol

from app.services.brokers.toss.dto import TossPrice

_TOSS_PRICE_BATCH = 200


class TossPriceClient(Protocol):
    async def prices(
        self, symbols: list[str] | tuple[str, ...]
    ) -> list[TossPrice]: ...


def _chunk(symbols: list[str], size: int = _TOSS_PRICE_BATCH) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


async def fetch_toss_batch_prices(
    client: TossPriceClient, symbols: list[str]
) -> dict[str, float | None]:
    """ONE batched Toss /api/v1/prices call per ≤200 chunk; fail-open to {}."""
    if not symbols:
        return {}
    # Map uppercased-echo -> requested symbol so we return the caller's keys.
    by_upper = {s.upper(): s for s in symbols}
    out: dict[str, float | None] = {}
    try:
        for batch in _chunk([s.upper() for s in symbols]):
            for price in await client.prices(batch):
                requested = by_upper.get(str(price.symbol).upper())
                if requested is not None:
                    out[requested] = float(price.last_price)
    except Exception as exc:  # noqa: BLE001 — fail-open, resolver falls through
        logger.warning(
            "invest price fallback: toss batch prices failed (%d symbols): %s",
            len(symbols), exc,
        )
        return {}
    return out
```
(`logging`/`logger` already defined at the top of the module from Task 2.)

- [ ] **Run it — passes.** `uv run pytest tests/test_fetch_toss_batch_prices.py -v` → 6 passed.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-696): fetch_toss_batch_prices chunked fail-open Toss /prices adapter"`

---

## Task 4 — Wire InvestQuoteService: optional toss_client + fallback chain

**Files:**
- Modify `app/services/invest_quote_service.py` — `__init__(..., toss_client=None)` (`:23`); move the KIS gather into `_kis_fetch_kr`/`_kis_fetch_us`; add `_snapshot_latest`, `_build_toss_fetch`; rewrite `fetch_kr_prices` (`:28`) and `fetch_us_prices` (`:51`) to run `PriceFallbackResolver`; drop per-symbol WARN (`:45,64,79`).
- Test (modify) `tests/test_invest_quote_service.py` — keep the 2 existing happy-path tests green, append fallback-chain cases.

**Interfaces:**
```python
def __init__(
    self,
    kis_client: SafeKISClient,
    db: AsyncSession,
    toss_client: TossReadClient | None = None,   # ROB-696, optional 3rd param
) -> None: ...

async def fetch_kr_prices(self, symbols: list[str]) -> dict[str, float | None]: ...
async def fetch_us_prices(self, symbols: list[str]) -> dict[str, float | None]: ...
```
- `_kis_fetch_kr(symbols)` = today's KR gather (`inquire_price(sym, market="J")`), per-symbol errors demoted to debug, returns `{sym: float|None}`.
- `_kis_fetch_us(symbols)` = today's US gather (`get_us_exchange_by_symbol` → `inquire_overseas_daily_price(sym, exchange_code=..., n=1, period="D")`), per-symbol errors demoted to debug.
- `_snapshot_latest(market, symbols)` = `await MarketQuoteSnapshotsRepository(self._db).latest_prices(market, symbols)` (returns `dict[str, float]`; resolver treats absent as `None`).
- `_build_toss_fetch()` → `(Fetcher | None, TossReadClient | None owned_to_close)`:
  - injected `self._toss_client` present → `(closure, None)` (don't close a caller-owned client);
  - else `settings.toss_api_enabled` → `client = TossReadClient.from_settings()`, `(closure, client)` (own → close in `finally`). **The `from_settings()` call is wrapped fail-open** (it can raise `TossMissingCredentials` when enabled-but-misconfigured, `auth.py:80,83,111`): on any construction error, log once and return `(None, None)` so the Toss layer is skipped and `_resolve` never re-raises. `from_settings()` does **no** network I/O (token/httpx client are lazy), so building it does not consult Toss;
  - else Toss disabled → `(None, None)`.
- Backward-compat: `InvestQuoteService(kis_client, db)` unchanged at `invest_api.py:170`. Public return type unchanged.

Steps:

- [ ] **Write failing tests — fallback wiring (append to `tests/test_invest_quote_service.py`).** The 2 existing tests (`test_fetch_kr_prices`, `test_fetch_us_prices`) stay verbatim and MUST still pass (KIS resolves all → Toss/snapshot never consulted; `_market_data.inquire_price.assert_called_once_with("005930", market="J")` still holds). Append:
```python
async def test_fetch_kr_prices_falls_back_to_toss_then_snapshot(monkeypatch):
    from decimal import Decimal

    from app.services.brokers.toss.dto import TossPrice

    kis_client = MagicMock()
    db = MagicMock()

    class _FakeToss:  # injected via 3rd param -> exercises Toss layer w/o network
        async def prices(self, symbols):
            return [
                TossPrice(symbol="B", timestamp=None,
                          last_price=Decimal("20"), currency="KRW")
                for s in symbols if s == "B"
            ]

    service = InvestQuoteService(kis_client, db, toss_client=_FakeToss())

    # KIS: A ok, B/C fail
    service._market_data = AsyncMock()

    async def _inquire(code, market="J"):
        if code == "A":
            return pd.DataFrame([{"close": 10.0}], index=["A"])
        raise RuntimeError("KIS down")

    service._market_data.inquire_price.side_effect = _inquire
    # snapshot resolves C (Toss had nothing for C)
    service._snapshot_latest = AsyncMock(return_value={"C": 30.0})

    out = await service.fetch_kr_prices(["A", "B", "C"])
    assert out == pytest.approx({"A": 10.0, "B": 20.0, "C": 30.0})
    service._snapshot_latest.assert_awaited_once_with("kr", ["C"])


async def test_fetch_us_prices_toss_disabled_uses_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.toss_api_enabled", False,
        raising=False,
    )
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)   # no toss_client, disabled

    service._market_data = AsyncMock()
    service._market_data.inquire_overseas_daily_price.side_effect = RuntimeError(
        "KIS down"
    )
    monkeypatch.setattr(
        "app.services.invest_quote_service.get_us_exchange_by_symbol",
        AsyncMock(return_value="NASD"),
    )
    service._snapshot_latest = AsyncMock(return_value={"AAPL": 222.5})

    out = await service.fetch_us_prices(["AAPL"])
    assert out == pytest.approx({"AAPL": 222.5})
    service._snapshot_latest.assert_awaited_once_with("us", ["AAPL"])


async def test_fetch_kr_prices_all_layers_down_returns_none(monkeypatch):
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)   # toss disabled by default
    service._market_data = AsyncMock()
    service._market_data.inquire_price.side_effect = RuntimeError("KIS down")
    service._snapshot_latest = AsyncMock(return_value={})   # no snapshot

    out = await service.fetch_kr_prices(["A", "B"])
    assert out == {"A": None, "B": None}   # never raises


async def test_fetch_kr_prices_toss_enabled_but_misconfigured_is_fail_open(
    monkeypatch,
):
    # Toss enabled but from_settings() raises (empty creds). Construction runs
    # OUTSIDE _resolve's try/finally, so it MUST be guarded — the call must fall
    # through to snapshot and never propagate out of fetch_kr_prices.
    monkeypatch.setattr(
        "app.services.invest_quote_service.settings.toss_api_enabled", True,
        raising=False,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("TOSS_API_CLIENT_SECRET is empty")

    monkeypatch.setattr(
        "app.services.invest_quote_service.TossReadClient.from_settings", _boom
    )
    kis_client = MagicMock()
    db = MagicMock()
    service = InvestQuoteService(kis_client, db)   # no injected toss_client
    service._market_data = AsyncMock()
    service._market_data.inquire_price.side_effect = RuntimeError("KIS down")
    service._snapshot_latest = AsyncMock(return_value={"A": 11.0})

    out = await service.fetch_kr_prices(["A"])
    assert out == pytest.approx({"A": 11.0})   # snapshot filled, never raised
    service._snapshot_latest.assert_awaited_once_with("kr", ["A"])
```

- [ ] **Run it — new tests fail, old tests still pass.** `uv run pytest tests/test_invest_quote_service.py -v`
  Expected: the 4 appended tests FAIL (`InvestQuoteService.__init__` takes no `toss_client`; no `_snapshot_latest`; `fetch_*` don't fall through; construction not fail-open). The 2 original tests PASS unchanged.

- [ ] **Minimal impl — rewrite `invest_quote_service.py`.** Replace the module body with (preserving the KIS calls verbatim inside the fetch closures):
```python
"""Read-only quote service for investment valuation (ROB-696 fallback chain)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.brokers.kis.market_data import MarketDataClient
from app.services.brokers.toss.client import TossReadClient
from app.services.invest_price_fallback import (
    Fetcher,
    PriceFallbackResolver,
    fetch_toss_batch_prices,
)
from app.services.market_quote_snapshots.repository import (
    MarketQuoteSnapshotsRepository,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

if TYPE_CHECKING:
    from app.services.invest_home_readers import SafeKISClient

logger = logging.getLogger(__name__)


class InvestQuoteService:
    """Read-only 시세 helper with a fail-open KIS → Toss → snapshot chain."""

    def __init__(
        self,
        kis_client: SafeKISClient,
        db: AsyncSession,
        toss_client: TossReadClient | None = None,
    ) -> None:
        self._kis = kis_client
        self._db = db
        self._market_data = MarketDataClient(kis_client)
        self._toss_client = toss_client

    async def fetch_kr_prices(self, symbols: list[str]) -> dict[str, float | None]:
        return await self._resolve(symbols, market="kr", kis_fetch=self._kis_fetch_kr)

    async def fetch_us_prices(self, symbols: list[str]) -> dict[str, float | None]:
        return await self._resolve(symbols, market="us", kis_fetch=self._kis_fetch_us)

    async def _resolve(
        self, symbols: list[str], *, market: str, kis_fetch: Fetcher
    ) -> dict[str, float | None]:
        if not symbols:
            return {}
        toss_fetch, owned = self._build_toss_fetch()
        try:
            resolver = PriceFallbackResolver(
                kis_fetch=kis_fetch,
                toss_fetch=toss_fetch,
                snapshot_fetch=lambda syms: self._snapshot_latest(market, syms),
                market=market,
            )
            return await resolver.resolve(symbols)
        finally:
            if owned is not None:
                await owned.aclose()

    def _build_toss_fetch(self) -> tuple[Fetcher | None, TossReadClient | None]:
        if self._toss_client is not None:
            client = self._toss_client
            return (lambda syms: fetch_toss_batch_prices(client, syms), None)
        if bool(getattr(settings, "toss_api_enabled", False)):
            # Fail-open: enabled-but-misconfigured Toss makes from_settings()
            # raise TossMissingCredentials (auth.py:80,83,111). Since this runs
            # OUTSIDE the try/finally in _resolve, a raw raise would escape
            # fetch_kr_prices/fetch_us_prices — so guard it and skip the layer.
            try:
                client = TossReadClient.from_settings()
            except Exception as exc:  # noqa: BLE001 — fail-open, skip Toss layer
                logger.warning(
                    "invest price fallback: Toss client construction failed; "
                    "skipping Toss layer: %s",
                    exc,
                )
                return (None, None)
            return (lambda syms: fetch_toss_batch_prices(client, syms), client)
        return (None, None)

    async def _snapshot_latest(
        self, market: str, symbols: list[str]
    ) -> dict[str, float | None]:
        try:
            found = await MarketQuoteSnapshotsRepository(self._db).latest_prices(
                market, symbols
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, resolver -> None
            logger.warning("invest price snapshot read failed (%s): %s", market, exc)
            return {}
        return dict(found)

    async def _kis_fetch_kr(self, symbols: list[str]) -> dict[str, float | None]:
        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                df = await self._market_data.inquire_price(symbol, market="J")
                results[symbol] = float(df.iloc[0]["close"]) if not df.empty else None
            except Exception as exc:  # noqa: BLE001 — summarized by the resolver
                logger.debug("KIS KR price miss %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results

    async def _kis_fetch_us(self, symbols: list[str]) -> dict[str, float | None]:
        results: dict[str, float | None] = {}

        async def _fetch(symbol: str) -> None:
            try:
                exchange = await get_us_exchange_by_symbol(symbol, self._db)
                df = await self._market_data.inquire_overseas_daily_price(
                    symbol, exchange_code=exchange, n=1, period="D"
                )
                results[symbol] = float(df.iloc[0]["close"]) if not df.empty else None
            except Exception as exc:  # noqa: BLE001 — summarized by the resolver
                logger.debug("KIS US price miss %s: %s", symbol, exc)
                results[symbol] = None

        await asyncio.gather(*(_fetch(s) for s in symbols))
        return results
```
Notes for the implementer:
- The KIS `inquire_price(symbol, market="J")` and `inquire_overseas_daily_price(symbol, exchange_code=exchange, n=1, period="D")` calls are preserved **verbatim** so the 2 existing tests (which mock `service._market_data` and assert those exact calls) stay green; `self._market_data` is still an overridable instance attribute.
- Per-symbol WARN (`old :45,64,79`) is demoted to `logger.debug`; the resolver emits one summary INFO per layer (Global Constraint "reduce log noise").

- [ ] **Run it — all pass.** `uv run pytest tests/test_invest_quote_service.py -v` → 6 passed (2 original + 4 new).

- [ ] **Regression — consumer + router safety.** `uv run pytest tests/test_invest_home_readers.py tests/test_invest_api_router_safety.py -v` → pass (2-arg construction at `invest_api.py:170` still valid; the lazy import graph is unchanged — `TossReadClient` is imported at module top but only *constructed* when `toss_api_enabled`).

- [ ] **Full-module sweep + lint.** `uv run pytest tests/test_invest_quote_service.py tests/test_price_fallback_resolver.py tests/test_fetch_toss_batch_prices.py tests/test_market_quote_snapshots_latest_prices.py -v` then `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-696): wire KIS→Toss→snapshot fallback into InvestQuoteService (optional toss_client)"`
