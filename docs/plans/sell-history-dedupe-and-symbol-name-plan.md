# Sell-History De-dup + Symbol-Name Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On `/invest/my?tab=sellHistory`, collapse the duplicate "보정"(reconciler) + "실시간"(websocket) rows for the same fill into one authoritative row, and show the stock name (e.g. NAVER) instead of the code (035420).

**Architecture:** Read-path fix only. (1) A pure de-dup helper in the execution-ledger query service drops provisional `websocket` rows for any order an authoritative `reconciler`/`manual_import` row already covers (order-level supersession, reconciler wins). Applied to the displayed rows **and** the FIFO history input so totals and realized-P&L stop double-counting. (2) A best-effort name resolver populates a new `symbol_name` field on the read schema by batch-joining the per-market universe tables. No DB migration, no write-path change, no broker mutation.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Pydantic v2 (backend); React + TypeScript + Vitest (frontend); pytest (backend tests). Package manager `uv`.

---

## Background & Evidence (prod-confirmed, 2026-06-14)

Data flow: `SellHistoryPanel.tsx` → `GET /trading/api/invest/fills/sell-history` → `ExecutionLedgerQueryService.list_sell_history()` → `review.execution_ledger`.

**Why duplicates exist.** Two independent writers insert into `review.execution_ledger`:
- `websocket_monitor.py::_record_execution_ledger_fill` → `source="websocket"` ("실시간"), real-time.
- `app/services/execution_ledger/reconciler.py` → `source="reconciler"` ("보정"), from the broker's authoritative filled-orders REST endpoint.

The unique key `uq_execution_ledger_fill = (broker, account_mode, venue, broker_order_id, fill_seq)` **excludes `source`**. Both writers derive `fill_seq` from *independent* hashes (`websocket_monitor._ledger_fill_seq` vs `normalizers._domestic_fill_seq`/`_overseas_fill_seq`/`_upbit_trade_fill_seq`), so the same fill lands as **two rows with different `fill_seq`** → no upsert collision. `list_sell_history` does no de-dup → both render.

**Prod query of `review.execution_ledger` confirms:**
- 520 order-groups carry **both** a websocket and a reconciler row; **0 websocket-only groups** (every realtime row currently has a reconciler twin). 521 websocket rows total — nearly all superseded duplicates.
- For each duplicate pair, the rows **share** `(broker, account_mode, venue, instrument_type, symbol, side, broker_order_id, filled_qty, filled_price)` but **differ** in `source`, `fill_seq`, `filled_at` (by minutes — sometimes ~1 day), `correlation_id` (websocket set / reconciler NULL), `fee_amount` (websocket NULL / reconciler 0).
- **37 dup-groups disagree on `filled_price`, 1 on `filled_qty`.** ⟹ an "economic tuple" key (qty/price) would FAIL to collapse them, and proves **reconciler must win** when the two disagree. Order-level supersession keyed on `broker_order_id` (NOT qty/price/fill_seq/filled_at) is the only correct approach.
- `kr_symbol_universe.name['035420'] = "NAVER"` ✓; `stock_info.name['035420'] = "035420"` (unpopulated → useless fallback).

**Concrete impact (035420 alone):** 3 real sells (10@251 000, 26@300 000, 1@262 500) render as **6 rows**; "총 판매금액" shows ~₩21.1M instead of the real ~₩10.6M (exactly 2×). FIFO also double-consumes buy lots (duplicate buys exist too: orders `0018700900`, `0011012000`, `0018718000`).

**Source authority order:** `reconciler` > `manual_import` (opening-lot seed, synthetic order id) > `websocket` (provisional). The reconciler re-fetches the broker's complete filled-order set and aggregates partials, so once an order is reconciled, every websocket row for it is redundant.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/schemas/execution_ledger.py` | Read DTO | Add `symbol_name: str \| None = None` to `ExecutionLedgerRead`. |
| `app/services/execution_ledger/query_service.py` | Read projection | Add pure `_supersede_provisional_fills()` + async `ExecutionLedgerQueryService._attach_symbol_names()`; wire into all 3 list methods (+ FIFO history input). |
| `app/services/us_symbol_universe_service.py` | US universe reads | Add `get_us_names_by_symbols()` (mirror `get_us_common_stock_flags`). |
| `frontend/invest/src/components/my/SellHistoryPanel.tsx` | Sell-history UI | Drop hardcoded `FALLBACK_SYMBOL_NAMES`; render backend `symbol_name`. |
| `tests/services/execution_ledger/test_query_service_dedup.py` | New test | De-dup helper + FIFO double-consume regression. |
| `tests/services/test_us_symbol_universe_names.py` | New test | `get_us_names_by_symbols` DB-backed. |
| `tests/services/execution_ledger/test_query_service_names.py` | New test | `_attach_symbol_names` (monkeypatched resolvers, fail-open). |
| `tests/routers/test_invest_fills_router.py` | Existing test | Replace the "DB-level dedup guarantee" assumption with a read-path dedup test. |
| `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx` | Existing test | Assert backend `symbol_name` rendering + de-dup totals. |

**No DB migration** — `symbol_name` is a response-schema field only (migration count: 0).

---

### Task 0: Worktree + branch

**Files:** none (setup only)

- [ ] **Step 1: Create an isolated worktree off latest `origin/main`**

Run:
```bash
cd /Users/mgh3326/work/auto_trader
git fetch --prune origin
git worktree add ../auto_trader.sell-history-dedup -b fix/sell-history-dedupe-and-name origin/main
cd ../auto_trader.sell-history-dedup
```
Expected: new worktree on a fresh branch, clean `git status`.

- [ ] **Step 2: Sanity-check the test toolchain**

Run: `uv run pytest tests/services/execution_ledger/test_query_service_profit.py -q`
Expected: existing FIFO tests PASS (baseline green).

---

### Task 1: De-dup helper (pure function, TDD)

**Files:**
- Modify: `app/services/execution_ledger/query_service.py` (add module-level helpers near `_ledger_item_key`, ~line 60)
- Test: `tests/services/execution_ledger/test_query_service_dedup.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/execution_ledger/test_query_service_dedup.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger.query_service import (
    _annotate_realized_profit,
    _supersede_provisional_fills,
)


def _item(
    *,
    source: str,
    side: str,
    qty: str,
    price: str,
    order_id: str,
    fill_seq: int,
    filled_at: datetime,
    symbol: str = "035420",
    instrument_type: str = "equity_kr",
    venue: str = "krx",
    currency: str = "KRW",
) -> ExecutionLedgerRead:
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    return ExecutionLedgerRead(
        id=None,
        broker="kis",
        account_mode="live",
        venue=venue,
        instrument_type=instrument_type,
        symbol=symbol,
        raw_symbol=symbol,
        side=side,
        broker_order_id=order_id,
        fill_seq=fill_seq,
        filled_qty=quantity,
        filled_price=unit_price,
        filled_notional=quantity * unit_price,
        filled_at=filled_at,
        currency=currency,
        source=source,
    )


def test_supersede_drops_websocket_when_reconciler_covers_order() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec = _item(source="reconciler", side="sell", qty="10", price="251000",
                order_id="0006366300", fill_seq=1511940115, filled_at=base)
    ws = _item(source="websocket", side="sell", qty="10", price="251000",
               order_id="0006366300", fill_seq=654241537,
               filled_at=base + timedelta(minutes=2))

    kept = _supersede_provisional_fills([rec, ws])

    assert len(kept) == 1
    assert kept[0].source == "reconciler"


def test_supersede_prefers_reconciler_even_when_price_disagrees() -> None:
    # 37 prod groups disagree on price; reconciler is authoritative.
    base = datetime(2026, 6, 1, tzinfo=UTC)
    ws = _item(source="websocket", side="sell", qty="26", price="299000",
               order_id="0000342400", fill_seq=1529477675, filled_at=base)
    rec = _item(source="reconciler", side="sell", qty="26", price="300000",
                order_id="0000342400", fill_seq=1124223453,
                filled_at=base + timedelta(days=1))

    kept = _supersede_provisional_fills([ws, rec])

    assert [k.source for k in kept] == ["reconciler"]
    assert kept[0].filled_price == Decimal("300000")


def test_supersede_keeps_websocket_only_orders() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    ws = _item(source="websocket", side="sell", qty="5", price="100",
               order_id="ws-only-1", fill_seq=42, filled_at=base)

    assert _supersede_provisional_fills([ws]) == [ws]


def test_supersede_preserves_distinct_orders_and_order_is_stable() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec_a = _item(source="reconciler", side="buy", qty="2", price="196500",
                  order_id="0018700900", fill_seq=1829863901, filled_at=base)
    ws_a = _item(source="websocket", side="buy", qty="2", price="196500",
                 order_id="0018700900", fill_seq=778408146, filled_at=base)
    rec_b = _item(source="reconciler", side="buy", qty="2", price="252000",
                  order_id="0011012000", fill_seq=313337176,
                  filled_at=base + timedelta(days=1))

    kept = _supersede_provisional_fills([rec_a, ws_a, rec_b])

    assert [k.broker_order_id for k in kept] == ["0018700900", "0011012000"]


def test_supersede_normalizes_leading_zero_order_id() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    rec = _item(source="reconciler", side="sell", qty="1", price="262500",
                order_id="0019990600", fill_seq=1889703609, filled_at=base)
    ws = _item(source="websocket", side="sell", qty="1", price="262500",
               order_id="19990600", fill_seq=877103355, filled_at=base)

    assert len(_supersede_provisional_fills([rec, ws])) == 1


def test_supersede_then_fifo_does_not_double_consume() -> None:
    base = datetime(2026, 6, 1, tzinfo=UTC)
    buy = _item(source="reconciler", side="buy", qty="10", price="200000",
                order_id="buy-1", fill_seq=1, filled_at=base)
    sell_rec = _item(source="reconciler", side="sell", qty="10", price="251000",
                     order_id="sell-1", fill_seq=111, filled_at=base + timedelta(days=1))
    sell_ws = _item(source="websocket", side="sell", qty="10", price="251000",
                    order_id="sell-1", fill_seq=222, filled_at=base + timedelta(days=1))

    history = _supersede_provisional_fills([buy, sell_rec, sell_ws])
    sells = [i for i in history if i.side == "sell"]
    annotated = _annotate_realized_profit(sells, history)

    # One sell, cost basis from the single 10-share lot (not double-consumed).
    assert len(annotated) == 1
    assert annotated[0].cost_basis_notional == Decimal("2000000")
    assert annotated[0].realized_profit == Decimal("510000")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/execution_ledger/test_query_service_dedup.py -q`
Expected: FAIL — `ImportError: cannot import name '_supersede_provisional_fills'`.

- [ ] **Step 3: Write minimal implementation**

In `app/services/execution_ledger/query_service.py`, add after `_ledger_item_key` (~line 67):
```python
# Source authority: reconciler (broker REST) and manual_import (seeded opening
# lots) are authoritative; websocket rows are provisional real-time notifications.
_PROVISIONAL_SOURCE = "websocket"


def _supersede_key(item: ExecutionLedgerRead) -> tuple[str, str, str, str, str, str, str]:
    """Order-level identity shared across sources for one logical order.

    Excludes fill_seq, filled_at and correlation_id on purpose: the websocket
    monitor and the reconciler derive divergent fill_seq (independent hashes) and
    timestamps for the same order, so only the order-level tuple links the two
    sources. broker_order_id is leading-zero-normalized to absorb formatting drift.
    """
    normalized_order_id = item.broker_order_id.lstrip("0") or item.broker_order_id
    return (
        item.broker,
        item.account_mode,
        item.venue,
        item.instrument_type,
        item.symbol,
        item.side,
        normalized_order_id,
    )


def _supersede_provisional_fills(
    items: list[ExecutionLedgerRead],
) -> list[ExecutionLedgerRead]:
    """Drop provisional websocket rows for orders an authoritative row covers.

    The ledger unique key excludes ``source`` and the two writers derive different
    ``fill_seq`` for the same fill, so one order can land as two+ rows. Once the
    reconciler books an order it is the authoritative record (it re-fetches the
    broker's complete filled-order set, aggregating partials), so any websocket row
    for that order is a duplicate. Websocket rows for not-yet-reconciled orders are
    preserved. Input order is preserved.
    """
    authoritative_orders = {
        _supersede_key(item)
        for item in items
        if item.source != _PROVISIONAL_SOURCE
    }
    return [
        item
        for item in items
        if item.source != _PROVISIONAL_SOURCE
        or _supersede_key(item) not in authoritative_orders
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/execution_ledger/test_query_service_dedup.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/execution_ledger/query_service.py tests/services/execution_ledger/test_query_service_dedup.py
git commit -m "feat(execution-ledger): add order-level provisional-fill supersession helper"
```

---

### Task 2: Wire de-dup into the three list methods

**Files:**
- Modify: `app/services/execution_ledger/query_service.py` (`list_recent` ~189-211, `list_by_symbol` ~213-239, `list_sell_history` ~241-290)
- Test: `tests/routers/test_invest_fills_router.py` (replace stale assumption)

- [ ] **Step 1: Write the failing test (router-level dedup)**

In `tests/routers/test_invest_fills_router.py`, **replace** `test_recent_fills_no_duplicate_rows` (currently ~line 176-189) with:
```python
@pytest.mark.unit
def test_recent_fills_supersedes_websocket_duplicate():
    """A reconciler + websocket row for the same order collapse to the reconciler row."""
    rows = [
        _ledger_row(id=1, broker_order_id="0006366300", fill_seq=1511940115, source="reconciler"),
        _ledger_row(id=2, broker_order_id="0006366300", fill_seq=654241537, source="websocket"),
    ]
    run = _reconcile_run_row("kis")
    db = _make_db(rows, [run])
    client = TestClient(_make_app(db))

    resp = client.get("/trading/api/invest/fills/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["source"] == "reconciler"
    assert data["source_breakdown"]["websocket"] == 0
    assert data["source_breakdown"]["reconciler"] == 1
```
Confirm `_ledger_row` accepts a `source=` kwarg; if it does not, add `source: str = "reconciler"` to its signature and pass it through to the constructed row (search `def _ledger_row` in the same file).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/routers/test_invest_fills_router.py::test_recent_fills_supersedes_websocket_duplicate -q`
Expected: FAIL — `count == 2` (no dedup wired yet).

- [ ] **Step 3: Write minimal implementation**

In `query_service.py`, apply the helper in each method.

`list_recent` — after `items = [ExecutionLedgerRead.model_validate(row) for row in rows]` (~line 199):
```python
        items = _supersede_provisional_fills(items)
```

`list_by_symbol` — after its `items = [...]` (~line 224):
```python
        items = _supersede_provisional_fills(items)
```

`list_sell_history` — after its `items = [...]` (~line 254) **and** after `history_items = [...]` (~line 277):
```python
        items = _supersede_provisional_fills(items)
        if items:
            # ... unchanged symbol/broker/... set derivation + history_stmt ...
            history_items = [
                ExecutionLedgerRead.model_validate(row) for row in history_rows
            ]
            history_items = _supersede_provisional_fills(history_items)
            items = _annotate_realized_profit(items, history_items)
```
(`source_breakdown=_compute_source_breakdown(items)` then naturally reflects the deduped set.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/routers/test_invest_fills_router.py tests/services/execution_ledger -q`
Expected: PASS (router dedup test + all existing query-service tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/execution_ledger/query_service.py tests/routers/test_invest_fills_router.py
git commit -m "fix(invest-fills): de-dup provisional websocket fills in read path (display + FIFO)"
```

---

### Task 3: `symbol_name` schema field + `get_us_names_by_symbols`

**Files:**
- Modify: `app/schemas/execution_ledger.py` (`ExecutionLedgerRead`, ~line 88)
- Modify: `app/services/us_symbol_universe_service.py` (add after `get_us_common_stock_flags`, ~line 307)
- Test: `tests/services/test_us_symbol_universe_names.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_us_symbol_universe_names.py`:
```python
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.us_symbol_universe import USSymbolUniverse
from app.services.us_symbol_universe_service import get_us_names_by_symbols


async def _session_with(rows):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(USSymbolUniverse.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    session.add_all(rows)
    await session.commit()
    return session


@pytest.mark.asyncio
async def test_get_us_names_prefers_korean_then_english_and_canonicalizes() -> None:
    rows = [
        USSymbolUniverse(symbol="TSLA", name_kr="테슬라", name_en="Tesla", is_active=True),
        USSymbolUniverse(symbol="BRK.B", name_kr="", name_en="Berkshire Hathaway", is_active=True),
        USSymbolUniverse(symbol="QQQ", name_kr="", name_en="", is_active=True),
        USSymbolUniverse(symbol="DEAD", name_kr="옛이름", name_en="Old", is_active=False),
    ]
    session = await _session_with(rows)
    try:
        # caller passes the ledger's symbol form; BRK-B must canonicalize to BRK.B
        out = await get_us_names_by_symbols(["TSLA", "BRK-B", "QQQ", "DEAD"], session)
    finally:
        await session.close()

    assert out["TSLA"] == "테슬라"
    assert out["BRK-B"] == "Berkshire Hathaway"  # keyed by caller's original string
    assert "QQQ" not in out   # no usable name -> omitted
    assert "DEAD" not in out  # inactive -> omitted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/test_us_symbol_universe_names.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_us_names_by_symbols'`.

- [ ] **Step 3a: Add the schema field**

In `app/schemas/execution_ledger.py`, inside `ExecutionLedgerRead`, after `updated_at` (line 88), add:
```python
    symbol_name: str | None = None
```

- [ ] **Step 3b: Add the US name resolver**

In `app/services/us_symbol_universe_service.py`, after `get_us_common_stock_flags` (~line 307), add:
```python
async def get_us_names_by_symbols(
    symbols: list[str],
    db: AsyncSession | None = None,
) -> dict[str, str]:
    """Return ``{input_symbol: display_name}`` for active US symbols.

    Name precedence mirrors ``search_us_symbols``: ``name_kr`` then ``name_en``.
    The dict is keyed by the caller's original symbol string. Symbols absent from
    the active universe (or with no usable name) are omitted, so callers keep
    showing the raw ticker on a miss.
    """
    canon_to_original: dict[str, str] = {}
    for raw in symbols:
        if not raw:
            continue
        canon = to_db_symbol(_normalize_symbol(raw))
        if canon:
            canon_to_original.setdefault(canon, raw)
    if not canon_to_original:
        return {}

    async def _run(session: AsyncSession) -> dict[str, str]:
        stmt = select(
            USSymbolUniverse.symbol,
            USSymbolUniverse.name_kr,
            USSymbolUniverse.name_en,
        ).where(
            USSymbolUniverse.symbol.in_(list(canon_to_original)),
            USSymbolUniverse.is_active.is_(True),
        )
        result = await session.execute(stmt)
        names: dict[str, str] = {}
        for canon_symbol, name_kr, name_en in result.all():
            original = canon_to_original.get(canon_symbol)
            if original is None:
                continue
            display = (name_kr or "").strip() or (name_en or "").strip()
            if display:
                names[original] = display
        return names

    if db is not None:
        return await _run(db)

    async with AsyncSessionLocal() as session:  # pyright: ignore[reportGeneralTypeIssues]
        return await _run(session)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/test_us_symbol_universe_names.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/execution_ledger.py app/services/us_symbol_universe_service.py tests/services/test_us_symbol_universe_names.py
git commit -m "feat(us-universe): add get_us_names_by_symbols + symbol_name read field"
```

---

### Task 4: `_attach_symbol_names` wiring (fail-open)

**Files:**
- Modify: `app/services/execution_ledger/query_service.py` (imports at top; new method on `ExecutionLedgerQueryService`; call in all 3 list methods)
- Test: `tests/services/execution_ledger/test_query_service_names.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/execution_ledger/test_query_service_names.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.execution_ledger import ExecutionLedgerRead
from app.services.execution_ledger import query_service
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService


def _item(symbol: str, instrument_type: str, raw_symbol: str | None = None) -> ExecutionLedgerRead:
    return ExecutionLedgerRead(
        id=None, broker="kis", account_mode="live", venue="krx",
        instrument_type=instrument_type, symbol=symbol, raw_symbol=raw_symbol or symbol,
        side="sell", broker_order_id="o1", fill_seq=1,
        filled_qty=Decimal("1"), filled_price=Decimal("1"), filled_notional=Decimal("1"),
        filled_at=datetime(2026, 6, 1, tzinfo=UTC), currency="KRW", source="reconciler",
    )


@pytest.mark.asyncio
async def test_attach_symbol_names_resolves_per_market(monkeypatch) -> None:
    async def fake_kr(symbols, db): return {"035420": "NAVER"}
    async def fake_us(symbols, db): return {"TSLA": "테슬라"}
    async def fake_crypto(markets, db): return {"KRW-BTC": {"korean_name": "비트코인", "english_name": "Bitcoin"}}
    monkeypatch.setattr(query_service, "get_kr_names_by_symbols", fake_kr)
    monkeypatch.setattr(query_service, "get_us_names_by_symbols", fake_us)
    monkeypatch.setattr(query_service, "get_upbit_market_display_names", fake_crypto)

    svc = ExecutionLedgerQueryService(db=object())  # db unused (resolvers faked)
    items = [
        _item("035420", "equity_kr"),
        _item("TSLA", "equity_us"),
        _item("BTC", "crypto", raw_symbol="KRW-BTC"),
        _item("999999", "equity_kr"),  # unresolved -> stays None
    ]
    out = await svc._attach_symbol_names(items)

    by_symbol = {i.symbol: i.symbol_name for i in out}
    assert by_symbol["035420"] == "NAVER"
    assert by_symbol["TSLA"] == "테슬라"
    assert by_symbol["BTC"] == "비트코인"
    assert by_symbol["999999"] is None


@pytest.mark.asyncio
async def test_attach_symbol_names_fails_open_on_resolver_error(monkeypatch) -> None:
    async def boom(symbols, db): raise RuntimeError("universe empty")
    monkeypatch.setattr(query_service, "get_kr_names_by_symbols", boom)

    svc = ExecutionLedgerQueryService(db=object())
    out = await svc._attach_symbol_names([_item("035420", "equity_kr")])

    assert out[0].symbol_name is None  # never breaks the endpoint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/execution_ledger/test_query_service_names.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_attach_symbol_names'`.

- [ ] **Step 3: Write minimal implementation**

In `query_service.py`, add imports near the top (after the existing `from app.services.execution_ledger.repository import ...`):
```python
import logging

from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols

logger = logging.getLogger(__name__)
```

Add this method to `ExecutionLedgerQueryService`:
```python
    async def _attach_symbol_names(
        self, items: list[ExecutionLedgerRead]
    ) -> list[ExecutionLedgerRead]:
        """Best-effort: populate symbol_name from the per-market universe tables.

        Names are cosmetic, so every resolver call fails open — a lookup error
        leaves symbol_name None and the UI falls back to the raw symbol.
        """
        if not items:
            return items

        kr_symbols = sorted({i.symbol for i in items if i.instrument_type == "equity_kr"})
        us_symbols = sorted({i.symbol for i in items if i.instrument_type == "equity_us"})
        crypto_markets = sorted({i.raw_symbol for i in items if i.instrument_type == "crypto"})

        async def _safe(coro, label):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - names are best-effort
                logger.warning("symbol-name resolution failed for %s", label, exc_info=True)
                return {}

        kr_names = await _safe(get_kr_names_by_symbols(kr_symbols, self.db), "kr") if kr_symbols else {}
        us_names = await _safe(get_us_names_by_symbols(us_symbols, self.db), "us") if us_symbols else {}
        crypto_disp = (
            await _safe(get_upbit_market_display_names(crypto_markets, self.db), "crypto")
            if crypto_markets
            else {}
        )

        def _name_for(item: ExecutionLedgerRead) -> str | None:
            if item.instrument_type == "equity_kr":
                return kr_names.get(item.symbol)
            if item.instrument_type == "equity_us":
                return us_names.get(item.symbol)
            if item.instrument_type == "crypto":
                disp = crypto_disp.get(item.raw_symbol)
                if disp:
                    return disp.get("korean_name") or disp.get("english_name")
            return None

        annotated: list[ExecutionLedgerRead] = []
        for item in items:
            name = _name_for(item)
            if name and name != item.symbol:
                annotated.append(item.model_copy(update={"symbol_name": name}))
            else:
                annotated.append(item)
        return annotated
```

Then call it in each list method, immediately **before** building the response (after dedup / FIFO annotation):
- `list_recent`: `items = await self._attach_symbol_names(items)` before `freshness = await self.freshness()`.
- `list_by_symbol`: same, before `freshness = ...`.
- `list_sell_history`: same, after `items = _annotate_realized_profit(...)` and before `freshness = ...`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/execution_ledger -q`
Expected: PASS (dedup + names + existing profit tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/execution_ledger/query_service.py tests/services/execution_ledger/test_query_service_names.py
git commit -m "feat(invest-fills): populate symbol_name from universe tables (fail-open)"
```

---

### Task 5: Frontend — render backend name, drop hardcoded map

**Files:**
- Modify: `frontend/invest/src/components/my/SellHistoryPanel.tsx` (remove `FALLBACK_SYMBOL_NAMES` lines 12-26; simplify `symbolDisplayName` lines 105-109)
- Test: `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx`

- [ ] **Step 1: Write the failing test**

In `frontend/invest/src/__tests__/SellHistoryPanel.test.tsx`, locate the helper that builds a mock `FillRow` (search for an object with `broker_order_id`/`filled_qty`). Add a `symbol_name` field to it where needed, then add:
```tsx
it("renders backend symbol_name instead of the code", async () => {
  mockFetchSellHistory({
    count: 1,
    items: [makeRow({ symbol: "035420", symbol_name: "NAVER" })],
    data_state: "fresh",
    source_breakdown: { reconciler: 1, websocket: 0, manual_import: 0 },
    empty_reason: null,
  });
  render(<SellHistoryPanel />);
  expect(await screen.findByText("NAVER")).toBeInTheDocument();
  // the code still appears in the secondary line
  expect(screen.getByText(/035420 ·/)).toBeInTheDocument();
});

it("falls back to the code when symbol_name is absent", async () => {
  mockFetchSellHistory({
    count: 1,
    items: [makeRow({ symbol: "035420", symbol_name: null })],
    data_state: "fresh",
    source_breakdown: { reconciler: 1, websocket: 0, manual_import: 0 },
    empty_reason: null,
  });
  render(<SellHistoryPanel />);
  expect(await screen.findAllByText(/035420/)).not.toHaveLength(0);
});
```
Match `mockFetchSellHistory`/`makeRow` to the file's existing mocking style (the existing tests already stub `fetchSellHistory` — reuse that exact mechanism; rename the helpers in the snippet to whatever the file already uses).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend/invest && npm test -- SellHistoryPanel`
Expected: FAIL (no `symbol_name` plumbing assertion yet / map still used).

- [ ] **Step 3: Write minimal implementation**

In `SellHistoryPanel.tsx`, delete the `FALLBACK_SYMBOL_NAMES` constant (lines 12-26) and simplify:
```tsx
function symbolDisplayName(row: FillRow): string | null {
  const name = row.symbol_name ?? row.symbolName;
  if (!name || name === row.symbol) return null;
  return name;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend/invest && npm test -- SellHistoryPanel && npm run typecheck`
Expected: PASS + clean typecheck.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/my/SellHistoryPanel.tsx frontend/invest/src/__tests__/SellHistoryPanel.test.tsx
git commit -m "feat(invest-my): render backend symbol_name in sell history, drop hardcoded map"
```

---

### Task 6: Full verification

**Files:** none (gates only)

- [ ] **Step 1: Backend lint + typecheck (app/ AND tests/)**

Run:
```bash
uv run ruff format app/ tests/
uv run ruff check app/ tests/
uv run ty check app/
```
Expected: all clean (CI lints both `app/` and `tests/`).

- [ ] **Step 2: Backend test sweep (touched areas)**

Run: `uv run pytest tests/services/execution_ledger tests/routers/test_invest_fills_router.py tests/services/test_us_symbol_universe_names.py -q`
Expected: all PASS.

- [ ] **Step 3: Frontend gate**

Run: `cd frontend/invest && npm test && npm run typecheck`
Expected: all PASS.

- [ ] **Step 4: Commit any formatting deltas**

```bash
git add -A && git commit -m "chore: ruff format" --allow-empty
```

---

## Out of scope / deferred follow-ups

- **One-time DB cleanup** of the ~521 superseded `websocket` rows (`DELETE` keyed on order-groups that also have a reconciler row). Not needed for correctness — the read-path handles display/totals/FIFO — but would shrink the table. Operator-gated, separate change.
- **Write-path convergence** (make `websocket_monitor` and the reconciler agree on `fill_seq`/`broker_order_id` so future rows upsert into one). Fragile: both currently fall back to *independent* hashes, and prod shows the two sources even disagree on price/qty for 37/520 groups, so reconciler-wins read-path dedup is the robust answer regardless.
- **`filled_at` discrepancy** between websocket and reconciler (occasionally ~1 day). Superseding to the reconciler row already shows the authoritative time; a dedicated audit is separate.
- **`recent`/`by-symbol` UI**: those endpoints now also de-dup, but have no live UI consumer today — covered defensively, no extra UI work.

---

## Self-Review

**1. Spec coverage.** (a) Duplicate "보정/실시간" → Task 1 helper + Task 2 wiring (display + FIFO + breakdown). (b) Totals/realized-P&L double-count → Task 2 applies dedup to `items` and FIFO `history_items`; Task 1 has the no-double-consume test. (c) Code→name → Task 3 (schema field + US resolver) + Task 4 (KR/US/crypto wiring) + Task 5 (frontend). All requirements mapped.

**2. Placeholder scan.** All code steps contain full code; test bodies are concrete. Two intentional "match the existing file" notes (router `_ledger_row` `source=` kwarg; frontend `mockFetchSellHistory`/`makeRow`) are adaptation instructions, not missing logic — both name the exact symbol to locate and the exact change.

**3. Type consistency.** `_supersede_provisional_fills` / `_supersede_key` / `_PROVISIONAL_SOURCE` names match across Tasks 1-2. `get_us_names_by_symbols(symbols, db) -> dict[str,str]` defined in Task 3, called in Task 4. `_attach_symbol_names(items) -> list[ExecutionLedgerRead]` defined and called consistently. `symbol_name` field name matches schema (Task 3), backend population (Task 4), and frontend `FillRow.symbol_name` (existing type, Task 5). Crypto resolver keyed on `raw_symbol`; return shape `{market: {korean_name, english_name}}` matches `get_upbit_market_display_names` signature.
