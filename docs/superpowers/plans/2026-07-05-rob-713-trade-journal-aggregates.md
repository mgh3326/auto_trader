# ROB-713 Trade Journal Aggregates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute setup-tagged trading-journal aggregates (win-rate, expectancy, profit factor, R-multiple, MAE/MFE) from live-order-ledger fills, and expose them via a read-only MCP tool plus the ROB-711 `decision_history` injection.

**Architecture:** New deterministic read module `app/services/trade_journal/aggregates.py` reconstructs closed round-trips FIFO from the three live order ledgers, resolves a setup tag per trade (`strategy_key` → `intent` → `untagged`), computes per-trade R-multiple (from the linked item's planned stop) and MAE/MFE (from daily OHLCV over the hold window), and folds them into per-tag `SetupAggregate` rows. Exposed as MCP `get_trading_scoreboard` (registered unconditionally) and as a `realized_r_by_tag` field on `build_decision_context`. On-demand compute + in-process TTL cache; no schema change.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastMCP tool registration, pytest (async). Reuses `app/services/market_data.get_ohlcv`, `app/services/trade_journal/forecast_service._normalize_symbol_for_filter`, `app/core/symbol.to_db_symbol`.

## Global Constraints

- **migration 0** — no new tables/columns. Aggregates are computed on read + cached in-process.
- **ROB-501** — no in-process LLM imports anywhere under `app/**`; all code deterministic. (Existing static guard scans `app/**`.)
- **read-path only** — never touches the order hot path; no broker calls.
- **Sample honesty** — any tag with `n < 10` carries `insufficient_sample = True`; consumers must not over-read small samples.
- **Fill source = the 3 live ledgers** (`KISLiveOrderLedger`, `LiveOrderLedger`, `TossLiveOrderLedger`) — NOT `review.trades` (which lacks provenance ids needed for tagging).
- **Setup-tag precedence** — `strategy_key` (from `trade_retrospectives`) first, `intent` (from `investment_report_items`) fallback, else `untagged`. Exact provenance join is ~0% populated today (per `decision_history.py:8-12`), so symbol+recency-window join is the workhorse; label `link_quality ∈ {exact, symbol_window}`.
- **Long-only** — pair buy→sell; shorts/options out of scope.
- **Design doc:** `docs/superpowers/specs/2026-07-05-rob-713-trade-journal-aggregates-design.md`.

---

### Task 1: FIFO round-trip reconstruction (pure)

**Files:**
- Create: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Fill` with fields: `market: str`, `symbol: str`, `account: str`, `side: str`, `qty: float`, `price: float`, `fee: float`, `ts: datetime`, `item_uuid: str | None`, `correlation_id: str | None`, `source: str`.
  - `@dataclass(frozen=True) class ClosedTrade` with fields: `market: str`, `symbol: str`, `account: str`, `qty: float`, `entry_price: float`, `exit_price: float`, `entry_ts: datetime`, `exit_ts: datetime`, `pnl_abs: float`, `pnl_pct: float`, `fees: float`, `entry_item_uuids: tuple[str, ...]`, `exit_item_uuid: str | None`, `entry_correlation_ids: tuple[str, ...]`, `exit_correlation_id: str | None`.
  - `def pair_fills_fifo(fills: list[Fill]) -> list[ClosedTrade]` — groups by `(market, account, symbol)`, FIFO-matches buys against later sells; each sell fill yields one `ClosedTrade` aggregating the buy lots it consumed. Unmatched sell qty (oversell / no prior buy) is dropped. Open residual buys produce no trade.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_trade_journal_aggregates.py
from datetime import datetime, timezone

import pytest

from app.services.trade_journal.aggregates import Fill, pair_fills_fifo


def _fill(side, qty, price, day, *, fee=0.0, item="i", corr="c"):
    return Fill(
        market="kr", symbol="005930", account="acct", side=side, qty=qty,
        price=price, fee=fee, ts=datetime(2026, 6, day, tzinfo=timezone.utc),
        item_uuid=item, correlation_id=corr, source="kis",
    )


def test_single_round_trip():
    trades = pair_fills_fifo([_fill("buy", 10, 100.0, 1), _fill("sell", 10, 110.0, 3)])
    assert len(trades) == 1
    t = trades[0]
    assert t.qty == 10
    assert t.entry_price == 100.0
    assert t.exit_price == 110.0
    assert t.pnl_pct == pytest.approx(0.10)
    assert t.pnl_abs == pytest.approx(100.0)
    assert t.entry_ts.day == 1 and t.exit_ts.day == 3


def test_two_buys_one_sell_weighted_entry():
    trades = pair_fills_fifo([
        _fill("buy", 10, 100.0, 1),
        _fill("buy", 10, 120.0, 2),
        _fill("sell", 20, 130.0, 3),
    ])
    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(110.0)  # qty-weighted
    assert trades[0].qty == 20


def test_partial_close_leaves_open_residual():
    trades = pair_fills_fifo([_fill("buy", 10, 100.0, 1), _fill("sell", 4, 130.0, 3)])
    assert len(trades) == 1
    assert trades[0].qty == 4  # only closed portion counts


def test_oversell_without_prior_buy_is_dropped():
    assert pair_fills_fifo([_fill("sell", 5, 100.0, 1)]) == []


def test_fees_reduce_pnl_abs():
    trades = pair_fills_fifo([
        _fill("buy", 10, 100.0, 1, fee=5.0),
        _fill("sell", 10, 110.0, 3, fee=5.0),
    ])
    assert trades[0].fees == pytest.approx(10.0)
    assert trades[0].pnl_abs == pytest.approx(90.0)  # 100 gross - 10 fees
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trade_journal_aggregates.py -v`
Expected: FAIL — `ImportError: cannot import name 'Fill'`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/trade_journal/aggregates.py
"""ROB-713 — deterministic trade-journal aggregates (expectancy / R-multiple /
MAE) over live-ledger fills. Read-only, no LLM (ROB-501), no schema change."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

_EPS = 1e-9


@dataclass(frozen=True)
class Fill:
    market: str
    symbol: str
    account: str
    side: str
    qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None
    source: str


@dataclass(frozen=True)
class ClosedTrade:
    market: str
    symbol: str
    account: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl_abs: float
    pnl_pct: float
    fees: float
    entry_item_uuids: tuple[str, ...]
    exit_item_uuid: str | None
    entry_correlation_ids: tuple[str, ...]
    exit_correlation_id: str | None


@dataclass
class _Lot:
    qty: float
    orig_qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None


def pair_fills_fifo(fills: list[Fill]) -> list[ClosedTrade]:
    groups: dict[tuple[str, str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        groups[(f.market, f.account, f.symbol)].append(f)

    closed: list[ClosedTrade] = []
    for (market, account, symbol), group in groups.items():
        group_sorted = sorted(group, key=lambda f: f.ts)
        open_lots: deque[_Lot] = deque()
        for f in group_sorted:
            if f.side == "buy":
                open_lots.append(
                    _Lot(f.qty, f.qty, f.price, f.fee, f.ts, f.item_uuid, f.correlation_id)
                )
                continue
            if f.side != "sell":
                continue
            remaining = f.qty
            consumed: list[tuple[float, _Lot]] = []
            while remaining > _EPS and open_lots:
                lot = open_lots[0]
                take = min(remaining, lot.qty)
                consumed.append((take, lot))
                lot.qty -= take
                remaining -= take
                if lot.qty <= _EPS:
                    open_lots.popleft()
            if not consumed:
                continue  # oversell / no matching entry (long-only)
            matched_qty = sum(t for t, _ in consumed)
            entry_price = sum(t * lot.price for t, lot in consumed) / matched_qty
            entry_ts = min(lot.ts for _, lot in consumed)
            entry_fee = sum(lot.fee * (t / lot.orig_qty) for t, lot in consumed)
            exit_fee = f.fee * (matched_qty / f.qty) if f.qty else 0.0
            fees = entry_fee + exit_fee
            gross = (f.price - entry_price) * matched_qty
            closed.append(
                ClosedTrade(
                    market=market,
                    symbol=symbol,
                    account=account,
                    qty=matched_qty,
                    entry_price=entry_price,
                    exit_price=f.price,
                    entry_ts=entry_ts,
                    exit_ts=f.ts,
                    pnl_abs=gross - fees,
                    pnl_pct=(f.price - entry_price) / entry_price if entry_price else 0.0,
                    fees=fees,
                    entry_item_uuids=tuple(
                        dict.fromkeys(lot.item_uuid for _, lot in consumed if lot.item_uuid)
                    ),
                    exit_item_uuid=f.item_uuid,
                    entry_correlation_ids=tuple(
                        dict.fromkeys(
                            lot.correlation_id for _, lot in consumed if lot.correlation_id
                        )
                    ),
                    exit_correlation_id=f.correlation_id,
                )
            )
    return closed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_trade_journal_aggregates.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates.py
git commit -m "feat(ROB-713): FIFO round-trip reconstruction for trade journal aggregates"
```

---

### Task 2: Load fills from the three live ledgers

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates_load.py`

**Interfaces:**
- Consumes: `Fill` (Task 1).
- Produces: `async def load_fills(db: AsyncSession, *, market: str | None = None, account_mode: str | None = None, date_from: date | None = None, date_to: date | None = None) -> list[Fill]` — reads filled rows (`filled_qty > 0`) from `KISLiveOrderLedger`, `LiveOrderLedger`, `TossLiveOrderLedger`, normalizes symbol via `app.core.symbol.to_db_symbol`, maps each ledger to a market token in `{"kr","us","crypto"}`, drops smoke rows, returns `list[Fill]`.

**Ledger field map (verified `app/models/review.py`):**
- All three: `symbol`, `side`, `filled_qty`, `avg_fill_price`, `report_item_uuid`, `correlation_id`, `trade_date`.
- Fees: `KISLiveOrderLedger.fee`; `LiveOrderLedger` — sum available fee columns if present else 0; `TossLiveOrderLedger.commission` + `.tax`.
- Market token: `KISLiveOrderLedger` → `"kr"`; `TossLiveOrderLedger` → `"us"` if `.market == "us"` else `"kr"`; `LiveOrderLedger` → `"crypto"` if `.market == "crypto"` else `"us"`.
- Account label: use `.account_scope`/`.broker` where present, else the source name — only used to segregate FIFO lots, not surfaced.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_trade_journal_aggregates_load.py
import uuid
from datetime import datetime, timezone

import pytest

from app.models.review import KISLiveOrderLedger
from app.services.trade_journal.aggregates import load_fills


@pytest.mark.asyncio
async def test_load_fills_reads_kis_filled_rows(db_session):
    db_session.add(
        KISLiveOrderLedger(
            symbol="005930", side="buy", quantity=10, price=100,
            filled_qty=10, avg_fill_price=100, status="filled",
            trade_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
            correlation_id="corr-1", report_item_uuid=uuid.uuid4(),
        )
    )
    await db_session.commit()

    fills = await load_fills(db_session, market="kr")
    assert len(fills) == 1
    assert fills[0].symbol == "005930"
    assert fills[0].side == "buy"
    assert fills[0].filled_qty == fills[0].qty == 10
    assert fills[0].market == "kr"


@pytest.mark.asyncio
async def test_load_fills_skips_unfilled_and_smoke(db_session):
    db_session.add(
        KISLiveOrderLedger(
            symbol="005930", side="buy", quantity=10, price=100,
            filled_qty=0, status="accepted",
            trade_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()
    assert await load_fills(db_session, market="kr") == []
```

> **Note:** `db_session` is the project's async DB fixture (`conftest.py`, test DB enforced). Confirm the exact fixture name used by neighboring service tests in `tests/services/` and match it; do not hand-roll a session.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_load.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_fills'`.

- [ ] **Step 3: Write minimal implementation**

Add to `aggregates.py`:

```python
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)

_SMOKE_TOKENS = ("smoke",)


def _is_smoke(*values: str | None) -> bool:
    return any(v and any(tok in v.lower() for tok in _SMOKE_TOKENS) for v in values)


def _fee_of(row: object) -> float:
    total = 0.0
    for attr in ("fee", "commission", "tax"):
        val = getattr(row, attr, None)
        if val is not None:
            total += float(val)
    return total


def _market_for(source: str, row: object) -> str:
    if source == "kis":
        return "kr"
    raw = (getattr(row, "market", None) or "").lower()
    if source == "toss":
        return "us" if raw == "us" else "kr"
    return "crypto" if raw == "crypto" else "us"  # live ledger


def _account_of(source: str, row: object) -> str:
    return (
        getattr(row, "account_scope", None)
        or getattr(row, "broker", None)
        or source
    )


async def load_fills(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Fill]:
    fills: list[Fill] = []
    for source, model in (
        ("kis", KISLiveOrderLedger),
        ("live", LiveOrderLedger),
        ("toss", TossLiveOrderLedger),
    ):
        stmt = select(model).where(model.filled_qty.isnot(None), model.filled_qty > 0)
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            row_market = _market_for(source, r)
            if market and row_market != market:
                continue
            if r.trade_date is not None:
                d = r.trade_date.date()
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue
            if _is_smoke(getattr(r, "correlation_id", None), getattr(r, "status", None)):
                continue
            corr = getattr(r, "correlation_id", None)
            item_uuid = getattr(r, "report_item_uuid", None)
            fills.append(
                Fill(
                    market=row_market,
                    symbol=to_db_symbol(r.symbol),
                    account=_account_of(source, r),
                    side=r.side,
                    qty=float(r.filled_qty),
                    price=float(r.avg_fill_price) if r.avg_fill_price is not None else 0.0,
                    fee=_fee_of(r),
                    ts=r.trade_date,
                    item_uuid=str(item_uuid) if item_uuid else None,
                    correlation_id=corr,
                    source=source,
                )
            )
    return [f for f in fills if f.price > 0 and f.ts is not None]
```

> If `account_mode` filtering is meaningful for these live ledgers, wire it against the ledger's account/broker column; otherwise leave the param accepted-but-unused for API symmetry and note it in the docstring. Do not invent a column.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_load.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_load.py
git commit -m "feat(ROB-713): load fills from the three live order ledgers"
```

---

### Task 3: Setup-tag resolution (strategy_key → intent → untagged)

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates_tag.py`

**Interfaces:**
- Consumes: `ClosedTrade` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class TagInfo` with `tag: str`, `tag_source: str` (`"strategy_key"|"intent"|"untagged"`), `link_quality: str` (`"exact"|"symbol_window"`).
  - `async def resolve_setup_tag(db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45) -> TagInfo`.
- Resolution order per trade:
  1. `strategy_key` exact: any `entry/exit_correlation_id` → `TradeRetrospective.strategy_key` (non-null).
  2. `strategy_key` symbol_window: latest `TradeRetrospective` with matching symbol and `created_at <= exit_ts` (within `window_days` before), non-null `strategy_key`.
  3. `intent` exact: any `entry/exit_item_uuid` → `InvestmentReportItem.item_uuid` → `intent`.
  4. `intent` symbol_window: latest `InvestmentReportItem` with matching symbol and `created_at <= entry_ts` (within window), taking `intent`.
  5. `untagged` (link_quality `"symbol_window"`).
- Symbol matching uses `_normalize_symbol_for_filter(trade.symbol, instrument_type)` where `instrument_type` maps from `trade.market` via `{"kr":"equity_kr","us":"equity_us","crypto":"crypto"}`; retro/item `symbol` compared after the same normalization (mirror `decision_history` which compares on raw `symbol ==`; prefer the normalized helper for parity with forecast_service).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_trade_journal_aggregates_tag.py
from datetime import datetime, timezone

import pytest

from app.models.review import TradeRetrospective
from app.services.trade_journal.aggregates import ClosedTrade, resolve_setup_tag


def _trade(**kw):
    base = dict(
        market="kr", symbol="005930", account="acct", qty=10,
        entry_price=100.0, exit_price=110.0,
        entry_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        exit_ts=datetime(2026, 6, 5, tzinfo=timezone.utc),
        pnl_abs=100.0, pnl_pct=0.1, fees=0.0,
        entry_item_uuids=(), exit_item_uuid=None,
        entry_correlation_ids=(), exit_correlation_id=None,
    )
    base.update(kw)
    return ClosedTrade(**base)


@pytest.mark.asyncio
async def test_strategy_key_symbol_window(db_session):
    db_session.add(
        TradeRetrospective(
            symbol="005930", side="sell", strategy_key="pullback_long",
            created_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        )
    )
    await db_session.commit()
    info = await resolve_setup_tag(db_session, _trade())
    assert info.tag == "pullback_long"
    assert info.tag_source == "strategy_key"
    assert info.link_quality == "symbol_window"


@pytest.mark.asyncio
async def test_untagged_when_no_signal(db_session):
    info = await resolve_setup_tag(db_session, _trade(symbol="000660"))
    assert info.tag == "untagged"
    assert info.tag_source == "untagged"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_tag.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_setup_tag'`.

- [ ] **Step 3: Write minimal implementation**

Add to `aggregates.py`:

```python
from datetime import timedelta

from app.models.investment_reports import InvestmentReportItem
from app.models.review import TradeForecast, TradeRetrospective
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

_MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


@dataclass(frozen=True)
class TagInfo:
    tag: str
    tag_source: str
    link_quality: str


async def resolve_setup_tag(
    db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45
) -> TagInfo:
    instrument = _MARKET_TO_INSTRUMENT.get(trade.market)
    norm = _normalize_symbol_for_filter(trade.symbol, instrument)
    window_start = trade.entry_ts - timedelta(days=window_days)

    corr_ids = [c for c in (*trade.entry_correlation_ids, trade.exit_correlation_id) if c]
    if corr_ids:
        row = (
            await db.execute(
                select(TradeRetrospective.strategy_key)
                .where(
                    TradeRetrospective.correlation_id.in_(corr_ids),
                    TradeRetrospective.strategy_key.isnot(None),
                )
                .order_by(TradeRetrospective.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row and not _is_smoke(row):
            return TagInfo(row, "strategy_key", "exact")

    retro_key = (
        await db.execute(
            select(TradeRetrospective.strategy_key)
            .where(
                TradeRetrospective.symbol == norm,
                TradeRetrospective.strategy_key.isnot(None),
                TradeRetrospective.created_at <= trade.exit_ts,
                TradeRetrospective.created_at >= window_start,
            )
            .order_by(TradeRetrospective.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if retro_key and not _is_smoke(retro_key):
        return TagInfo(retro_key, "strategy_key", "symbol_window")

    item_uuids = [u for u in (*trade.entry_item_uuids, trade.exit_item_uuid) if u]
    if item_uuids:
        intent = (
            await db.execute(
                select(InvestmentReportItem.intent)
                .where(InvestmentReportItem.item_uuid.in_(item_uuids))
                .order_by(InvestmentReportItem.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if intent:
            return TagInfo(intent, "intent", "exact")

    intent_win = (
        await db.execute(
            select(InvestmentReportItem.intent)
            .where(
                InvestmentReportItem.symbol == norm,
                InvestmentReportItem.created_at <= trade.entry_ts,
                InvestmentReportItem.created_at >= window_start,
            )
            .order_by(InvestmentReportItem.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if intent_win:
        return TagInfo(intent_win, "intent", "symbol_window")

    return TagInfo("untagged", "untagged", "symbol_window")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_tag.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_tag.py
git commit -m "feat(ROB-713): setup-tag resolution (strategy_key -> intent -> untagged)"
```

---

### Task 4: Per-trade metrics — R-multiple + MAE/MFE

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates_metrics.py`

**Interfaces:**
- Consumes: `ClosedTrade` (Task 1), `get_ohlcv` (`app/services/market_data`).
- Produces:
  - `def compute_r_multiple(trade: ClosedTrade, planned_stop: float | None) -> float | None` — `None` when `planned_stop` is missing or `entry_price == planned_stop`; else `(exit_price - entry_price) / abs(entry_price - planned_stop)`.
  - `async def planned_stop_for(db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45) -> float | None` — reads the linked item's `evidence_snapshot["trade_setup"]["stop"]` (exact via item_uuid → symbol_window fallback); returns `float` or `None`.
  - `async def compute_excursions(trade: ClosedTrade) -> tuple[float | None, float | None, bool]` — returns `(mae, mfe, degraded)`. `mae = (min(low) - entry)/entry`, `mfe = (max(high) - entry)/entry` over daily candles whose date ∈ `[entry_ts.date, exit_ts.date]`. `degraded=True` when the span exceeds 200 trading days (count cap). `(None, None, degraded)` when no candles.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_trade_journal_aggregates_metrics.py
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.services.trade_journal import aggregates as agg
from app.services.trade_journal.aggregates import ClosedTrade, compute_r_multiple


def _trade(**kw):
    base = dict(
        market="kr", symbol="005930", account="acct", qty=10,
        entry_price=100.0, exit_price=112.0,
        entry_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        exit_ts=datetime(2026, 6, 5, tzinfo=timezone.utc),
        pnl_abs=120.0, pnl_pct=0.12, fees=0.0,
        entry_item_uuids=(), exit_item_uuid=None,
        entry_correlation_ids=(), exit_correlation_id=None,
    )
    base.update(kw)
    return ClosedTrade(**base)


def test_r_multiple_with_stop():
    # entry 100, stop 96 -> risk 4; exit 112 -> reward 12 -> R = 3.0
    assert compute_r_multiple(_trade(), 96.0) == pytest.approx(3.0)


def test_r_multiple_none_without_stop():
    assert compute_r_multiple(_trade(), None) is None


@pytest.mark.asyncio
async def test_excursions_from_stubbed_ohlcv(monkeypatch):
    @dataclass
    class C:
        timestamp: datetime
        high: float
        low: float

    async def fake_get_ohlcv(symbol, market, period, count, end=None):
        return [
            C(datetime(2026, 6, 1, tzinfo=timezone.utc), 101, 95),   # low 95
            C(datetime(2026, 6, 3, tzinfo=timezone.utc), 118, 99),   # high 118
            C(datetime(2026, 6, 5, tzinfo=timezone.utc), 113, 108),
        ]

    monkeypatch.setattr(agg, "get_ohlcv", fake_get_ohlcv)
    mae, mfe, degraded = await agg.compute_excursions(_trade())
    assert mae == pytest.approx((95 - 100) / 100)   # -0.05
    assert mfe == pytest.approx((118 - 100) / 100)   # +0.18
    assert degraded is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_metrics.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_r_multiple'`.

- [ ] **Step 3: Write minimal implementation**

Add to `aggregates.py`:

```python
from app.services.market_data import get_ohlcv

_MAX_OHLCV_BARS = 200


def compute_r_multiple(trade: ClosedTrade, planned_stop: float | None) -> float | None:
    if planned_stop is None:
        return None
    risk = abs(trade.entry_price - planned_stop)
    if risk <= _EPS:
        return None
    return (trade.exit_price - trade.entry_price) / risk


async def planned_stop_for(
    db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45
) -> float | None:
    instrument = _MARKET_TO_INSTRUMENT.get(trade.market)
    norm = _normalize_symbol_for_filter(trade.symbol, instrument)
    window_start = trade.entry_ts - timedelta(days=window_days)

    item_uuids = [u for u in (*trade.entry_item_uuids, trade.exit_item_uuid) if u]
    stmt = (
        select(InvestmentReportItem.evidence_snapshot)
        .where(InvestmentReportItem.item_uuid.in_(item_uuids))
        .order_by(InvestmentReportItem.created_at.desc())
        .limit(1)
        if item_uuids
        else select(InvestmentReportItem.evidence_snapshot)
        .where(
            InvestmentReportItem.symbol == norm,
            InvestmentReportItem.created_at <= trade.entry_ts,
            InvestmentReportItem.created_at >= window_start,
        )
        .order_by(InvestmentReportItem.created_at.desc())
        .limit(1)
    )
    snapshot = (await db.execute(stmt)).scalar_one_or_none()
    if not isinstance(snapshot, dict):
        return None
    stop = (snapshot.get("trade_setup") or {}).get("stop")
    try:
        return float(stop) if stop is not None else None
    except (TypeError, ValueError):
        return None


async def compute_excursions(
    trade: ClosedTrade,
) -> tuple[float | None, float | None, bool]:
    span_days = (trade.exit_ts.date() - trade.entry_ts.date()).days + 1
    degraded = span_days > _MAX_OHLCV_BARS
    count = min(max(span_days + 2, 2), _MAX_OHLCV_BARS)
    candles = await get_ohlcv(
        trade.symbol, trade.market, period="day", count=count, end=trade.exit_ts
    )
    window = [
        c
        for c in candles
        if trade.entry_ts.date() <= c.timestamp.date() <= trade.exit_ts.date()
    ]
    if not window:
        return None, None, degraded
    entry = trade.entry_price
    if entry <= _EPS:
        return None, None, degraded
    mae = (min(float(c.low) for c in window) - entry) / entry
    mfe = (max(float(c.high) for c in window) - entry) / entry
    return mae, mfe, degraded
```

> `get_ohlcv` is imported at module scope so tests can `monkeypatch.setattr(aggregates, "get_ohlcv", ...)`. Confirm `market="kr"|"us"|"crypto"` is accepted by `get_ohlcv._normalize_market`; if it expects `equity_us` etc., map via `_MARKET_TO_INSTRUMENT` before the call.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_metrics.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_metrics.py
git commit -m "feat(ROB-713): per-trade R-multiple + MAE/MFE metrics"
```

---

### Task 5: Per-tag aggregate + scoreboard orchestrator + cache

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `@dataclass class TradeMetrics` bundling a `ClosedTrade` with `tag: TagInfo`, `r_multiple: float | None`, `mae: float | None`, `mfe: float | None`.
  - `def aggregate_by_tag(rows: list[TradeMetrics]) -> list[dict]` — pure; one dict per tag with keys: `tag, tag_source, link_quality, n, wins, losses, win_rate, expectancy_pct, expectancy_r, profit_factor, avg_r, median_r, r_coverage, avg_mae, avg_mfe, worst_mae, insufficient_sample`. Sorted by `n` desc.
  - `async def build_trading_scoreboard(db, *, market=None, account_mode=None, date_from=None, date_to=None, setup_tag=None, min_sample=1, use_cache=True) -> dict` — orchestrates load→pair→resolve→metrics→aggregate; returns `{"groups": [...], "overall": {...}, "as_of": iso, "count": n_trades}`. `overall` is `aggregate_by_tag` over all rows relabeled `tag="__overall__"`. Filters groups by `setup_tag` and `n >= min_sample` (overall always computed on the full set). In-process TTL cache keyed by the filter tuple.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_trade_journal_aggregates_scoreboard.py
from datetime import datetime, timezone

import pytest

from app.services.trade_journal.aggregates import (
    ClosedTrade, TagInfo, TradeMetrics, aggregate_by_tag,
)


def _tm(pnl_pct, r, tag="pullback_long"):
    ct = ClosedTrade(
        market="kr", symbol="005930", account="a", qty=10,
        entry_price=100.0, exit_price=100.0 * (1 + pnl_pct),
        entry_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        exit_ts=datetime(2026, 6, 2, tzinfo=timezone.utc),
        pnl_abs=1000.0 * pnl_pct, pnl_pct=pnl_pct, fees=0.0,
        entry_item_uuids=(), exit_item_uuid=None,
        entry_correlation_ids=(), exit_correlation_id=None,
    )
    return TradeMetrics(
        trade=ct, tag=TagInfo(tag, "strategy_key", "symbol_window"),
        r_multiple=r, mae=-0.03, mfe=0.08,
    )


def test_aggregate_math():
    rows = [_tm(0.10, 2.0), _tm(-0.05, -1.0), _tm(0.20, 3.0)]
    [g] = aggregate_by_tag(rows)
    assert g["tag"] == "pullback_long"
    assert g["n"] == 3
    assert g["wins"] == 2 and g["losses"] == 1
    assert g["win_rate"] == pytest.approx(2 / 3)
    assert g["expectancy_pct"] == pytest.approx((0.10 - 0.05 + 0.20) / 3)
    assert g["expectancy_r"] == pytest.approx((2.0 - 1.0 + 3.0) / 3)
    # profit factor = gross wins / |gross losses| = (100+200)/50
    assert g["profit_factor"] == pytest.approx(300 / 50)
    assert g["insufficient_sample"] is True  # n < 10


def test_insufficient_sample_flag_clears_at_10():
    rows = [_tm(0.01, 1.0) for _ in range(10)]
    [g] = aggregate_by_tag(rows)
    assert g["n"] == 10
    assert g["insufficient_sample"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -v`
Expected: FAIL — `ImportError: cannot import name 'TradeMetrics'`.

- [ ] **Step 3: Write minimal implementation**

Add to `aggregates.py`:

```python
from statistics import fmean, median
from datetime import datetime, timezone

_INSUFFICIENT_SAMPLE_N = 10
_SCOREBOARD_TTL_SECONDS = 300
_scoreboard_cache: dict[tuple, tuple[float, dict]] = {}


@dataclass
class TradeMetrics:
    trade: ClosedTrade
    tag: TagInfo
    r_multiple: float | None
    mae: float | None
    mfe: float | None


def _agg_one(tag: str, rows: list[TradeMetrics]) -> dict:
    pnls = [r.trade.pnl_pct for r in rows if r.trade.pnl_pct is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(r.trade.pnl_abs for r in rows if r.trade.pnl_abs > 0)
    gross_loss = abs(sum(r.trade.pnl_abs for r in rows if r.trade.pnl_abs < 0))
    rs = [r.r_multiple for r in rows if r.r_multiple is not None]
    maes = [r.mae for r in rows if r.mae is not None]
    mfes = [r.mfe for r in rows if r.mfe is not None]
    n = len(rows)
    sources = {r.tag.tag_source for r in rows}
    quals = {r.tag.link_quality for r in rows}
    return {
        "tag": tag,
        "tag_source": next(iter(sources)) if len(sources) == 1 else "mixed",
        "link_quality": "exact" if quals == {"exact"} else "symbol_window",
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "expectancy_pct": fmean(pnls) if pnls else None,
        "expectancy_r": fmean(rs) if rs else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > _EPS else None,
        "avg_r": fmean(rs) if rs else None,
        "median_r": median(rs) if rs else None,
        "r_coverage": (len(rs) / n) if n else None,
        "avg_mae": fmean(maes) if maes else None,
        "avg_mfe": fmean(mfes) if mfes else None,
        "worst_mae": min(maes) if maes else None,
        "insufficient_sample": n < _INSUFFICIENT_SAMPLE_N,
    }


def aggregate_by_tag(rows: list[TradeMetrics]) -> list[dict]:
    by_tag: dict[str, list[TradeMetrics]] = defaultdict(list)
    for r in rows:
        by_tag[r.tag.tag].append(r)
    groups = [_agg_one(tag, tag_rows) for tag, tag_rows in by_tag.items()]
    groups.sort(key=lambda g: g["n"], reverse=True)
    return groups


async def build_trading_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
    use_cache: bool = True,
    now: datetime | None = None,
) -> dict:
    key = (market, account_mode, date_from, date_to, setup_tag, min_sample)
    stamp = (now or datetime.now(timezone.utc)).timestamp()
    if use_cache:
        cached = _scoreboard_cache.get(key)
        if cached and stamp - cached[0] < _SCOREBOARD_TTL_SECONDS:
            return cached[1]

    fills = await load_fills(
        db, market=market, account_mode=account_mode,
        date_from=date_from, date_to=date_to,
    )
    trades = pair_fills_fifo(fills)
    rows: list[TradeMetrics] = []
    for t in trades:
        tag = await resolve_setup_tag(db, t)
        stop = await planned_stop_for(db, t)
        mae, mfe, _degraded = await compute_excursions(t)
        rows.append(
            TradeMetrics(t, tag, compute_r_multiple(t, stop), mae, mfe)
        )

    groups = aggregate_by_tag(rows)
    if setup_tag:
        groups = [g for g in groups if g["tag"] == setup_tag]
    groups = [g for g in groups if g["n"] >= min_sample]
    result = {
        "groups": groups,
        "overall": _agg_one("__overall__", rows) if rows else None,
        "as_of": (now or datetime.now(timezone.utc)).isoformat(),
        "count": len(rows),
    }
    if use_cache:
        _scoreboard_cache[key] = (stamp, result)
    return result
```

> `compute_excursions` may raise on external OHLCV errors — wrap the per-trade metric calls in a `try/except` that degrades that trade's `mae/mfe/r_multiple` to `None` (fail-open) so one bad symbol never sinks the whole scoreboard. Add a test in Task 5 covering a raising `get_ohlcv`.

- [ ] **Step 4: Add the fail-open test + run**

```python
# append to test_trade_journal_aggregates_scoreboard.py
@pytest.mark.asyncio
async def test_scoreboard_fail_open_on_ohlcv_error(db_session, monkeypatch):
    from app.services.trade_journal import aggregates as agg

    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(agg, "get_ohlcv", boom)
    # no fills seeded -> empty but must not raise
    result = await agg.build_trading_scoreboard(db_session, use_cache=False)
    assert result["count"] == 0
    assert result["groups"] == []
```

Run: `uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -v`
Expected: PASS (3 tests). If the fail-open test fails, add the `try/except` around `compute_excursions`/`planned_stop_for` in `build_trading_scoreboard`.

- [ ] **Step 5: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "feat(ROB-713): per-tag aggregate + scoreboard orchestrator with TTL cache"
```

---

### Task 6: MCP tool `get_trading_scoreboard`

**Files:**
- Create: `app/mcp_server/tooling/trading_scoreboard_tools.py`
- Create: `app/mcp_server/tooling/trading_scoreboard_registration.py`
- Modify: `app/mcp_server/tooling/registry.py` (Always block, ~line 197)
- Test: `tests/mcp/test_trading_scoreboard.py`

**Interfaces:**
- Consumes: `build_trading_scoreboard` (Task 5).
- Produces:
  - `async def get_trading_scoreboard(market=None, account_mode=None, date_from=None, date_to=None, setup_tag=None, min_sample=1) -> dict` (opens its own session like sibling read tools).
  - `TRADING_SCOREBOARD_TOOL_NAMES: set[str] = {"get_trading_scoreboard"}` and `register_trading_scoreboard_tools(mcp)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_trading_scoreboard.py
import pytest

from app.mcp_server.tooling.trading_scoreboard_tools import get_trading_scoreboard


@pytest.mark.asyncio
async def test_scoreboard_tool_empty_db_shape():
    result = await get_trading_scoreboard()
    assert set(result) >= {"groups", "overall", "as_of", "count"}
    assert result["count"] == 0
    assert result["groups"] == []
```

> Match how sibling tools acquire a session in `forecast_tools.py` / `trade_retrospective_tools.py` (e.g. an `async with get_session()` / `AsyncSessionLocal()` context). Use the identical pattern; the test above assumes the tool self-manages its session against the test DB.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mcp/test_trading_scoreboard.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the tool + registration**

```python
# app/mcp_server/tooling/trading_scoreboard_tools.py
"""ROB-713 — read-only MCP surface for setup-tagged trade-journal aggregates."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.trade_journal.aggregates import build_trading_scoreboard
# Use the SAME session accessor as forecast_tools.py / trade_retrospective_tools.py:
from app.db.session import AsyncSessionLocal  # adjust import to match siblings


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


async def get_trading_scoreboard(
    market: str | None = None,
    account_mode: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        return await build_trading_scoreboard(
            db,
            market=market,
            account_mode=account_mode,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            setup_tag=setup_tag,
            min_sample=min_sample,
        )
```

```python
# app/mcp_server/tooling/trading_scoreboard_registration.py
"""ROB-713 — MCP registration for the trading scoreboard read tool."""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.trading_scoreboard_tools import get_trading_scoreboard

TRADING_SCOREBOARD_TOOL_NAMES: set[str] = {"get_trading_scoreboard"}


def register_trading_scoreboard_tools(mcp: Any) -> None:
    _ = mcp.tool(
        name="get_trading_scoreboard",
        description=(
            "Setup-tagged trade-journal aggregates over closed round-trips "
            "reconstructed from live-order-ledger fills: per setup tag "
            "(strategy_key -> intent -> untagged) win-rate, expectancy (% and "
            "R-multiple), profit factor, average/worst MAE and MFE. Tags with "
            "n<10 are flagged insufficient_sample. Filters: market, "
            "account_mode, date_from/date_to (YYYY-MM-DD), setup_tag, "
            "min_sample. Read-only; deterministic."
        ),
    )(get_trading_scoreboard)


__all__ = ["TRADING_SCOREBOARD_TOOL_NAMES", "register_trading_scoreboard_tools"]
```

Modify `registry.py` — add import near the other tooling imports and register in the **Always** block (right after `register_forecast_tools(mcp)`):

```python
from app.mcp_server.tooling.trading_scoreboard_registration import (
    register_trading_scoreboard_tools,
)
# ...
    register_forecast_tools(mcp)
    register_trading_scoreboard_tools(mcp)  # ROB-713: read-only, no profile gate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mcp/test_trading_scoreboard.py -v`
Expected: PASS. Fix the `AsyncSessionLocal` import path to match siblings if it errors.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/trading_scoreboard_tools.py \
        app/mcp_server/tooling/trading_scoreboard_registration.py \
        app/mcp_server/tooling/registry.py tests/mcp/test_trading_scoreboard.py
git commit -m "feat(ROB-713): get_trading_scoreboard read-only MCP tool"
```

---

### Task 7: Inject `realized_r_by_tag` into decision_history

**Files:**
- Modify: `app/services/decision_history.py`
- Test: `tests/services/test_decision_history_realized_r.py` (or extend the existing decision_history test module)

**Interfaces:**
- Consumes: `build_trading_scoreboard` (Task 5), the existing `build_decision_context` return dict.
- Produces: `build_decision_context(...)` return dict gains a `realized_r_by_tag: dict[str, dict]` key — a bounded map (≤3 tags relevant to this symbol) of `{tag: {n, expectancy_r, win_rate, profit_factor, avg_mae, insufficient_sample}}`. When `setup_tag` is passed, that tag is included first if present.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/test_decision_history_realized_r.py
import pytest

from app.services import decision_history as dh


@pytest.mark.asyncio
async def test_realized_r_by_tag_present_and_bounded(db_session, monkeypatch):
    async def fake_scoreboard(db, *, market=None, **kw):
        return {
            "groups": [
                {"tag": f"t{i}", "n": 12, "expectancy_r": 1.0, "win_rate": 0.6,
                 "profit_factor": 2.0, "avg_mae": -0.03, "insufficient_sample": False}
                for i in range(5)
            ],
            "overall": None, "as_of": "2026-07-05T00:00:00+00:00", "count": 60,
        }

    monkeypatch.setattr(dh, "build_trading_scoreboard", fake_scoreboard)
    # Seed at least one prior item so build_decision_context does not return None,
    # or call the internal builder directly; match the existing test's seeding style.
    ctx = await dh.build_decision_context(db_session, "005930", "kr")
    assert ctx is not None
    assert "realized_r_by_tag" in ctx
    assert len(ctx["realized_r_by_tag"]) <= 3
    first = next(iter(ctx["realized_r_by_tag"].values()))
    assert set(first) == {
        "n", "expectancy_r", "win_rate", "profit_factor",
        "avg_mae", "insufficient_sample",
    }
```

> Reuse the existing decision_history test's fixture/seeding approach so `build_decision_context` returns non-`None` (it returns `None` when there is no signal). Check `tests/` for the current ROB-711 test module and mirror its setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_decision_history_realized_r.py -v`
Expected: FAIL — `KeyError: 'realized_r_by_tag'`.

- [ ] **Step 3: Write minimal implementation**

In `decision_history.py`, add the import and a helper, and add the key to the `ctx` dict (the block ending at `return ctx`, ~line 104-115):

```python
from app.services.trade_journal.aggregates import build_trading_scoreboard

_MAX_TAGS = 3
_R_KEYS = ("n", "expectancy_r", "win_rate", "profit_factor", "avg_mae", "insufficient_sample")


async def _realized_r_by_tag(
    db: AsyncSession, market: str, setup_tag: str | None
) -> dict[str, dict[str, Any]]:
    board = await build_trading_scoreboard(db, market=market)
    groups = board.get("groups", [])
    ordered = sorted(groups, key=lambda g: (g["tag"] != setup_tag, -int(g["n"])))
    out: dict[str, dict[str, Any]] = {}
    for g in ordered[:_MAX_TAGS]:
        if g["tag"] == "untagged":
            continue
        out[g["tag"]] = {k: g.get(k) for k in _R_KEYS}
    return out
```

Then in `build_decision_context`, before building `ctx`:

```python
    realized_r = await _realized_r_by_tag(db, market, setup_tag)
```

and add to the `ctx` dict literal:

```python
        "realized_r_by_tag": realized_r,
```

> Keep the injection cheap: `build_trading_scoreboard` is TTL-cached, so repeated symbol calls in one batch reuse the computed board. Do not thread per-symbol filters into the scoreboard call — the tag map is portfolio-wide, sliced to the tags with the largest samples (and `setup_tag` first).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_decision_history_realized_r.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full new-file suite + lint + commit**

```bash
uv run pytest tests/services/test_trade_journal_aggregates*.py tests/mcp/test_trading_scoreboard.py tests/services/test_decision_history_realized_r.py -v
uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -v   # ROB-501 guard
make lint
git add app/services/decision_history.py tests/services/test_decision_history_realized_r.py
git commit -m "feat(ROB-713): inject realized_r_by_tag into decision_history"
```

---

## Final Verification

- [ ] `uv run pytest tests/services/test_trade_journal_aggregates*.py tests/mcp/test_trading_scoreboard.py tests/services/test_decision_history_realized_r.py -v` — all green.
- [ ] `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py` — ROB-501 static guard green (new module imports no LLM provider).
- [ ] `make lint` + `make typecheck` clean on the new/modified files.
- [ ] Manual MCP smoke against the dev DB: call `get_trading_scoreboard` and confirm it returns `{groups, overall, as_of, count}` without error; if any real closed round-trips exist, confirm at least the shape (n, expectancy_pct/expectancy_r, insufficient_sample) is sane.
- [ ] Confirm `git grep -n "realized_r_by_tag"` shows the reserved hook (decision_history docstring `:75-76`) is now implemented.
- [ ] No new alembic migration (`git status` shows nothing under `alembic/versions/`).

## Out of scope (fast-follow issues)

- Extend ROB-691 `JudgmentScoreboardPanel` with `group_by=setup` + expectancy/R/MAE columns (frontend + `invest_retrospectives.py` router/schema).
- Snapshot table + scheduled builder (would be a migration) — only if on-demand compute proves too slow at scale.
- Shorts/options/intraday MAE.
