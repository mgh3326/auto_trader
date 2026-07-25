# Upbit Shadow-Sim (resting-limit paper orders) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add realistic **resting limit orders + a reconcile fill engine** to the existing paper-trading system, so live-Upbit-priced crypto analysis can drive mock buy/sell that only fills when the market actually crosses the limit.

**Architecture:** Additive extension of `PaperTradingService`. A pure fill engine (`paper_fills.py`) decides whether live Upbit OHLCV crossed a resting limit; a new `paper_pending_orders` table stores resting orders; `PaperLimitOrderService` places/reconciles/cancels them and books fills through the existing `PaperTradingService` path; 4 MCP tools expose it on the DEFAULT profile. Existing market orders / accounts / positions / PnL / analytics / tools are reused unchanged.

**Tech Stack:** Python 3.13, uv, SQLAlchemy async + Postgres (`public` schema — paper tables live there), FastMCP, pytest (markers unit/integration/slow/live), ruff (line 88, py313) + ty (app/ only), alembic.

## Global Constraints

- **Reuse, don't duplicate:** account/deposit(`create_paper_account`), reset, positions, cash, market orders, PnL, analytics, and their MCP tools already exist — do NOT reimplement. This project adds ONLY resting-limit + reconcile.
- **Preserve existing behavior:** do NOT change `PaperTradingService.execute_order`'s existing market or instant-limit paths. Resting limits are a NEW path.
- **Pure simulation:** touches no real broker/Upbit; reads live market data + writes only paper tables. No real order/mutation surface. ROB-501: no LLM import.
- **Reuse constants (never re-hardcode):** `FEE_RATES["crypto"]` + `calculate_fee` (`app/services/paper_trading_service.py:32,39`); min-order `DEFAULT_MINIMUM_VALUES["crypto"] == 5000.0` (`app/mcp_server/tooling/shared.py:40`); tick bands from `adjust_price_to_upbit_unit` (`app/services/brokers/upbit/orders.py:294`).
- **Loss-sell guard NOT enforced** (practice account).
- Lint gate: `uv run ruff check app/ tests/` + `uv run ruff format --check app/ tests/` + `uv run ty check app/ --error-on-warning`. Tests: `uv run --all-groups pytest ... --no-cov`.

---

## File Structure

- `app/services/paper_fills.py` — **create**: pure fill engine (`snap_limit_down`, `limit_crossed`).
- `app/models/paper_trading.py` — **modify**: add `PaperPendingOrder`.
- `app/models/__init__.py` — **modify**: import + `__all__` add `PaperPendingOrder` (so `create_all` builds it in test DB).
- `alembic/versions/<new>_paper_pending_orders.py` — **create**: additive migration.
- `app/services/paper_limit_order_service.py` — **create**: `PaperLimitOrderService`.
- `app/mcp_server/tooling/paper_limit_order_handler.py` — **create**: 4 MCP tools + `register_paper_limit_order_tools` + `PAPER_LIMIT_ORDER_TOOL_NAMES`.
- `app/mcp_server/tooling/registry.py` — **modify**: import + register on DEFAULT (after line 238 `register_paper_account_tools(mcp)`).
- `tests/test_mcp_profiles.py` — **modify**: import `PAPER_LIMIT_ORDER_TOOL_NAMES`; add to `_ALL_ORDER_TOOL_NAMES` (line 242) AND `_ORDER_SURFACE_MATRIX[McpProfile.DEFAULT]` (line 226).
- Tests: `tests/services/test_paper_fills.py`, `tests/services/test_paper_limit_order_service.py`, `tests/test_mcp_paper_limit_order.py`.

---

## Task 1: Pure fill engine (`paper_fills.py`)

**Files:**
- Create: `app/services/paper_fills.py`
- Test: `tests/services/test_paper_fills.py`

**Interfaces:**
- Produces:
  - `snap_limit_down(price: Decimal) -> Decimal` — floor `price` to the Upbit KRW tick band.
  - `limit_crossed(side: str, limit_price: Decimal, bars: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None` — `bars` = list of `(low, high)`; buy fills if any `low <= limit_price`, sell fills if any `high >= limit_price`; returns `limit_price` on fill else `None`.

- [ ] **Step 1: Write failing tests.**

```python
# tests/services/test_paper_fills.py
from decimal import Decimal
import pytest
from app.services.paper_fills import snap_limit_down, limit_crossed

@pytest.mark.unit
def test_snap_limit_down_bands():
    assert snap_limit_down(Decimal("95001234")) == Decimal("95001000")  # >=2M band=1000
    assert snap_limit_down(Decimal("1234567")) == Decimal("1234500")    # >=1M band=500
    assert snap_limit_down(Decimal("2317")) == Decimal("2315")          # >=1k band=5
    assert snap_limit_down(Decimal("3.037")) == Decimal("3.03")         # >=1 band=0.01

@pytest.mark.unit
def test_limit_crossed_buy_fills_when_low_touches():
    # buy limit 100; a bar dipped to 99 -> filled at 100
    assert limit_crossed("buy", Decimal("100"), [(Decimal("101"), Decimal("105")), (Decimal("99"), Decimal("102"))]) == Decimal("100")

@pytest.mark.unit
def test_limit_crossed_buy_no_fill_when_low_above():
    assert limit_crossed("buy", Decimal("100"), [(Decimal("101"), Decimal("110"))]) is None

@pytest.mark.unit
def test_limit_crossed_sell_fills_when_high_touches():
    assert limit_crossed("sell", Decimal("100"), [(Decimal("95"), Decimal("101"))]) == Decimal("100")

@pytest.mark.unit
def test_limit_crossed_sell_no_fill_when_high_below():
    assert limit_crossed("sell", Decimal("100"), [(Decimal("90"), Decimal("99"))]) is None

@pytest.mark.unit
def test_limit_crossed_empty_bars_none():
    assert limit_crossed("buy", Decimal("100"), []) is None
```

- [ ] **Step 2: Run — expect FAIL (module missing).** `uv run --all-groups pytest tests/services/test_paper_fills.py -v --no-cov`

- [ ] **Step 3: Implement `paper_fills.py`.**

```python
# app/services/paper_fills.py
"""Pure fill-decision helpers for the paper resting-limit sim (ROB-703).

No I/O, no LLM, no DB — Decimal in, Decimal/None out. The Upbit KRW tick
bands mirror app/services/brokers/upbit/orders.py::adjust_price_to_upbit_unit,
but we FLOOR (conservative snap-down) instead of round-to-nearest.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal

# (threshold, tick unit) — first band whose threshold <= price applies.
_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("500")),
    (Decimal("500000"), Decimal("100")),
    (Decimal("100000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("1000"), Decimal("5")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
)
_MIN_TICK = Decimal("0.00001")


def snap_limit_down(price: Decimal) -> Decimal:
    unit = next((u for thr, u in _TICK_BANDS if price >= thr), _MIN_TICK)
    return (price / unit).to_integral_value(rounding=ROUND_FLOOR) * unit


def limit_crossed(
    side: str, limit_price: Decimal, bars: Sequence[tuple[Decimal, Decimal]]
) -> Decimal | None:
    """bars = list of (low, high). Buy fills if any low <= limit; sell if any high >= limit."""
    side = side.lower()
    for low, high in bars:
        if side == "buy" and low <= limit_price:
            return limit_price
        if side == "sell" and high >= limit_price:
            return limit_price
    return None
```

- [ ] **Step 4: Run — expect PASS. Lint.** `uv run --all-groups pytest tests/services/test_paper_fills.py -v --no-cov` ; `uv run ruff check app/services/paper_fills.py tests/services/test_paper_fills.py && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add app/services/paper_fills.py tests/services/test_paper_fills.py && git commit -m "feat(ROB-703): pure fill engine for paper resting-limit orders"` (append the standard co-author + Claude-Session trailer lines to every commit).

---

## Task 2: `PaperPendingOrder` model + migration

**Files:**
- Modify: `app/models/paper_trading.py` (add the model), `app/models/__init__.py` (register)
- Create: `alembic/versions/<rev>_paper_pending_orders.py`
- Test: covered by Task 3's integration tests (model creation is exercised there).

**Interfaces:**
- Produces: `PaperPendingOrder` ORM (table `paper_pending_orders`) with columns: `id`, `account_id` (FK `paper_accounts.id`), `symbol`, `side`, `order_type` (`limit`), `limit_price`, `quantity`, `reserved_krw`, `status` (`pending`/`filled`/`cancelled`), `thesis`, `fill_price`, `paper_trade_id` (FK `paper_trades.id`, nullable), `placed_at`, `filled_at`, `cancelled_at`, `created_at`, `updated_at`.

- [ ] **Step 1: Add the model** (mirror the existing `PaperPosition`/`PaperTrade` column conventions in `app/models/paper_trading.py` — `BigInteger` PK, `Numeric(20,8)` for qty/price, `Numeric(20,4)` for KRW, `TIMESTAMP(timezone=True)` server_default `func.now()`).

```python
# app/models/paper_trading.py  (append; reuse the file's existing imports)
class PaperPendingOrder(Base):
    __tablename__ = "paper_pending_orders"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="paper_pending_orders_side"),
        CheckConstraint("order_type IN ('limit')", name="paper_pending_orders_order_type"),
        CheckConstraint(
            "status IN ('pending','filled','cancelled')",
            name="paper_pending_orders_status",
        ),
        Index("ix_paper_pending_orders_account_id", "account_id"),
        Index("ix_paper_pending_orders_status", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, server_default="limit")
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reserved_krw: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")
    thesis: Mapped[str | None] = mapped_column(Text)
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    paper_trade_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("paper_trades.id", ondelete="SET NULL")
    )
    placed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    filled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
```
Ensure `CheckConstraint`, `ForeignKey`, `Index`, `BigInteger`, `String`, `Numeric`, `Text`, `func`, `TIMESTAMP` are imported at the top of the file (add any missing).

- [ ] **Step 2: Register in `app/models/__init__.py`** — add `from .paper_trading import PaperPendingOrder` alongside the other paper imports and add `"PaperPendingOrder"` to `__all__` (so `Base.metadata.create_all` builds it in the test DB, matching how `BinanceDemoOrderLedger` is registered).

- [ ] **Step 3: Generate the migration.**

Run: `cd /Users/mgh3326/work/auto_trader.rob-703 && ENV_FILE=... uv run alembic revision --autogenerate -m "ROB-703 paper_pending_orders"` (or hand-author `op.create_table("paper_pending_orders", ..., schema=None)` with the columns above; `down_revision` = current head from `uv run alembic heads`). Review the generated file — it must ONLY create `paper_pending_orders` (no unrelated drops).

- [ ] **Step 4: Verify the model imports + table shape via a quick check.** Run: `uv run --all-groups pytest tests/services/test_paper_fills.py -v --no-cov` (sanity import) and confirm `uv run python -c "from app.models import PaperPendingOrder; print(PaperPendingOrder.__tablename__)"` prints `paper_pending_orders`.

- [ ] **Step 5: Commit.** `git add app/models/paper_trading.py app/models/__init__.py alembic/versions/*paper_pending_orders*.py && git commit -m "feat(ROB-703): paper_pending_orders table for resting limit orders"`

---

## Task 3: `PaperLimitOrderService`

**Files:**
- Create: `app/services/paper_limit_order_service.py`
- Test: `tests/services/test_paper_limit_order_service.py`

**Interfaces:**
- Consumes: `PaperPendingOrder` (Task 2), `snap_limit_down`/`limit_crossed` (Task 1), `PaperTradingService` (existing: `execute_order`, `get_account`, `_get_position`), `FEE_RATES`/`calculate_fee`, `DEFAULT_MINIMUM_VALUES["crypto"]`, `app.services.market_data.service.get_ohlcv`.
- Produces: `PaperLimitOrderService(session)` with `async place_limit_order(...) -> dict`, `async reconcile_pending_orders(*, account_id, now) -> dict`, `async cancel_pending_order(*, account_id, order_id) -> dict`, `async list_pending_orders(*, account_id) -> list[dict]`.

- [ ] **Step 1: Write failing integration tests** (`@pytest.mark.asyncio`, `db_session`; monkeypatch OHLCV with canned bars). Mirror `tests/services/brokers/binance/demo/test_ledger_service.py` fixture style.

```python
# tests/services/test_paper_limit_order_service.py
from decimal import Decimal
import pytest
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService

@pytest.mark.asyncio
async def test_place_limit_buy_rests_and_reserves_cash(db_session, monkeypatch):
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob703-a", initial_capital_krw=Decimal("1000000"))
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC", side="buy",
                                      limit_price=Decimal("90000000"), amount=Decimal("100000"))
    assert out["success"] and out["status"] == "pending"
    cash = await pts.get_cash_balance(acct.id)
    assert cash["cash_krw"] < Decimal("1000000")  # reserved

@pytest.mark.asyncio
async def test_reconcile_fills_when_market_crosses(db_session, monkeypatch):
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob703-b", initial_capital_krw=Decimal("1000000"))
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC", side="buy",
                                limit_price=Decimal("90000000"), amount=Decimal("100000"))
    # canned OHLCV whose low dipped below the limit
    async def _bars(symbol, market, period, count, end=None):
        class C:  # minimal Candle-like
            def __init__(s, lo, hi): s.low, s.high = lo, hi
        return [C(Decimal("89000000"), Decimal("91000000"))]
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 1
    positions = await pts.get_positions(acct.id)
    assert any(p["symbol"] == "KRW-BTC" for p in positions)

@pytest.mark.asyncio
async def test_reconcile_no_cross_stays_pending(db_session, monkeypatch):
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(name="rob703-c", initial_capital_krw=Decimal("1000000"))
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC", side="buy",
                                limit_price=Decimal("50000000"), amount=Decimal("100000"))
    async def _bars(symbol, market, period, count, end=None):
        class C:
            def __init__(s, lo, hi): s.low, s.high = lo, hi
        return [C(Decimal("89000000"), Decimal("91000000"))]  # never dipped to 50M
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 0
    pend = await svc.list_pending_orders(account_id=acct.id)
    assert len(pend) == 1
```

- [ ] **Step 2: Run — expect FAIL.** `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov`

- [ ] **Step 3: Implement the service.** (Adapt Candle→(low,high): `get_ohlcv` returns `list[Candle]` with `.low`/`.high` — cast to `Decimal(str(...))`. Book the fill by delegating to the existing `PaperTradingService.execute_order(..., order_type="market", price=<fill_price>...)` so position/cash/PnL are updated by the proven path; the market path's `_fetch_current_price` is bypassed because `price` is supplied for limit — verify: if `execute_order` ignores `price` for market, instead call it with `order_type="limit", price=fill_price` which uses the supplied price. Choose the branch that books at exactly `fill_price` without a live fetch, and release the reservation first so cash math isn't double-counted.)

```python
# app/services/paper_limit_order_service.py  (skeleton — fill in per the interfaces)
from __future__ import annotations
import datetime as dt
from decimal import Decimal
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.timezone import now_kst
from app.models.paper_trading import PaperAccount, PaperPendingOrder
from app.services.market_data.service import get_ohlcv
from app.services.paper_fills import limit_crossed, snap_limit_down
from app.services.paper_trading_service import (
    FEE_RATES, PaperTradingService, calculate_fee,
)

_MIN_KRW = Decimal("5000")

class PaperLimitOrderService:
    def __init__(self, session: AsyncSession) -> None:
        self.db = session
        self.pts = PaperTradingService(session)

    async def place_limit_order(self, *, account_id, symbol, side, limit_price,
                                quantity=None, amount=None, thesis=None) -> dict[str, Any]:
        # 1. validate account active; snap price; resolve qty; min-notional 5000 KRW; one-of qty/amount
        # 2. buy: gross = qty*price, fee = calculate_fee('crypto', 'buy', gross), reserve = gross+fee;
        #    check account.cash_krw >= reserve; deduct reserve from cash_krw; record reserved_krw
        #    sell: check position qty available
        # 3. insert PaperPendingOrder(status='pending', placed_at=now); flush; return summary
        ...

    async def reconcile_pending_orders(self, *, account_id, now=None) -> dict[str, Any]:
        now = now or now_kst()
        rows = (await self.db.execute(
            select(PaperPendingOrder).where(
                PaperPendingOrder.account_id == account_id,
                PaperPendingOrder.status == "pending",
            )
        )).scalars().all()
        filled = 0
        for row in rows:
            candles = await get_ohlcv(row.symbol, "crypto", "1m", 200, end=None)  # over-fetch; filter >= placed_at
            bars = [(Decimal(str(c.low)), Decimal(str(c.high))) for c in candles
                    if getattr(c, "timestamp", None) is None or c.timestamp >= row.placed_at]
            if not bars:
                continue  # data_unavailable -> stays pending
            fill = limit_crossed(row.side, Decimal(row.limit_price), bars)
            if fill is None:
                continue
            # release reservation, then book the fill through the existing execute path at `fill`
            account = await self.pts.get_account(account_id)
            account.cash_krw += Decimal(row.reserved_krw)  # release; execute_order re-charges
            await self.db.flush()
            result = await self.pts.execute_order(
                account_id=account_id, symbol=row.symbol, side=row.side,
                order_type="limit", price=fill, quantity=Decimal(row.quantity),
                reason=row.thesis or "paper resting-limit fill",
            )
            row.status = "filled"; row.fill_price = fill; row.filled_at = now
            row.paper_trade_id = result.get("trade", {}).get("id")
            filled += 1
        await self.db.commit()
        return {"success": True, "reconciled": len(rows), "filled": filled}

    async def cancel_pending_order(self, *, account_id, order_id) -> dict[str, Any]:
        # fetch pending row; release reserved_krw to cash; status='cancelled'; commit
        ...

    async def list_pending_orders(self, *, account_id) -> list[dict[str, Any]]:
        ...
```

Implement the `...` bodies to satisfy the tests. **Key correctness:** buy reservation released before `execute_order` re-charges (no double deduct); `execute_order` for `order_type="limit"` uses the supplied `price` (verify against `preview_order` line 220-223: limit uses `fill_price = price` — good, it books at `fill`, no live fetch).

- [ ] **Step 4: Run — expect PASS. Lint + type.** `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov` ; `uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add app/services/paper_limit_order_service.py tests/services/test_paper_limit_order_service.py && git commit -m "feat(ROB-703): PaperLimitOrderService (place/reconcile/cancel/list resting limits)"`

---

## Task 4: MCP tools + registration

**Files:**
- Create: `app/mcp_server/tooling/paper_limit_order_handler.py`
- Modify: `app/mcp_server/tooling/registry.py` (import + register on DEFAULT after line 238), `tests/test_mcp_profiles.py` (name-set import + `_ALL_ORDER_TOOL_NAMES` + `_ORDER_SURFACE_MATRIX[DEFAULT]`)
- Test: `tests/test_mcp_paper_limit_order.py`

**Interfaces:**
- Consumes: `PaperLimitOrderService` (Task 3).
- Produces: `register_paper_limit_order_tools(mcp)` + `PAPER_LIMIT_ORDER_TOOL_NAMES: set[str]` = `{"paper_place_limit_order","paper_reconcile_orders","paper_cancel_pending_order","paper_list_pending_orders"}`.

- [ ] **Step 1: Write the handler** (mirror `orders_kiwoom_variants.py:450-513` register()/decorator/dry_run+confirm shape; open a session via `AsyncSessionLocal`).

```python
# app/mcp_server/tooling/paper_limit_order_handler.py
from __future__ import annotations
from decimal import Decimal
from typing import Any, Literal
from fastmcp import FastMCP
from app.core.db import AsyncSessionLocal
from app.services.paper_limit_order_service import PaperLimitOrderService

PAPER_LIMIT_ORDER_TOOL_NAMES: set[str] = {
    "paper_place_limit_order", "paper_reconcile_orders",
    "paper_cancel_pending_order", "paper_list_pending_orders",
}
__all__ = ["register_paper_limit_order_tools", "PAPER_LIMIT_ORDER_TOOL_NAMES"]


def register_paper_limit_order_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="paper_place_limit_order",
              description="Place a RESTING limit order on a paper account (fills only when live Upbit price crosses). dry_run defaults to True.")
    async def paper_place_limit_order(account_id: int, symbol: str, side: Literal["buy","sell"],
                                      limit_price: float, quantity: float | None = None,
                                      amount_krw: float | None = None, thesis: str | None = None,
                                      dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
        if not dry_run and not confirm:
            return {"success": False, "error": "paper_place_limit_order requires confirm=True when dry_run=False."}
        async with AsyncSessionLocal() as db:
            svc = PaperLimitOrderService(db)
            if dry_run:
                # preview-only: validate + snapped price + projected reserve, no write
                ...
            return await svc.place_limit_order(account_id=account_id, symbol=symbol, side=side,
                limit_price=Decimal(str(limit_price)),
                quantity=Decimal(str(quantity)) if quantity is not None else None,
                amount=Decimal(str(amount_krw)) if amount_krw is not None else None, thesis=thesis)

    @mcp.tool(name="paper_reconcile_orders", description="Fill any resting paper limit orders whose live Upbit price crossed.")
    async def paper_reconcile_orders(account_id: int) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            return await PaperLimitOrderService(db).reconcile_pending_orders(account_id=account_id)

    @mcp.tool(name="paper_cancel_pending_order", description="Cancel a resting paper limit order and release its reservation.")
    async def paper_cancel_pending_order(account_id: int, order_id: int) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            return await PaperLimitOrderService(db).cancel_pending_order(account_id=account_id, order_id=order_id)

    @mcp.tool(name="paper_list_pending_orders", description="List resting paper limit orders + live distance-to-fill.")
    async def paper_list_pending_orders(account_id: int) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            return {"success": True, "pending": await PaperLimitOrderService(db).list_pending_orders(account_id=account_id)}
```
(Fill the dry_run preview branch: run the same validation/snap and return the projected order without inserting.)

- [ ] **Step 2: Wire registry** — in `app/mcp_server/tooling/registry.py`, add near line 101 the import `from app.mcp_server.tooling.paper_limit_order_handler import register_paper_limit_order_tools`, and inside `if profile is McpProfile.DEFAULT:` add `register_paper_limit_order_tools(mcp)` immediately after `register_paper_account_tools(mcp)` (line 238). Register UNCONDITIONALLY (pure sim).

- [ ] **Step 3: Wire the profile matrix (set-equality guard — BOTH places).** In `tests/test_mcp_profiles.py`: import `from app.mcp_server.tooling.paper_limit_order_handler import PAPER_LIMIT_ORDER_TOOL_NAMES`; add `| PAPER_LIMIT_ORDER_TOOL_NAMES` to `_ALL_ORDER_TOOL_NAMES` (line 242) AND to `_ORDER_SURFACE_MATRIX[McpProfile.DEFAULT]` (line 226).

- [ ] **Step 4: Write the registration + gate test** (mirror `tests/test_mcp_kiwoom_order_variants.py`).

```python
# tests/test_mcp_paper_limit_order.py
from typing import Any, cast
import pytest
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.paper_limit_order_handler import PAPER_LIMIT_ORDER_TOOL_NAMES
from tests._mcp_tooling_support import DummyMCP

@pytest.mark.unit
def test_paper_limit_tools_registered_on_default():
    mcp = DummyMCP(); register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert PAPER_LIMIT_ORDER_TOOL_NAMES <= set(mcp.tools.keys())

@pytest.mark.unit
def test_paper_limit_tools_absent_on_shadow_replay():
    mcp = DummyMCP(); register_all_tools(cast(Any, mcp), profile=McpProfile.SHADOW_REPLAY)
    assert PAPER_LIMIT_ORDER_TOOL_NAMES.isdisjoint(set(mcp.tools.keys()))
```

- [ ] **Step 5: Run all + lint.** `uv run --all-groups pytest tests/test_mcp_paper_limit_order.py tests/test_mcp_profiles.py -v --no-cov` (the set-equality matrix MUST stay green) ; `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 6: Commit.** `git add app/mcp_server/tooling/paper_limit_order_handler.py app/mcp_server/tooling/registry.py tests/test_mcp_profiles.py tests/test_mcp_paper_limit_order.py && git commit -m "feat(ROB-703): MCP tools for paper resting-limit orders on DEFAULT"`

---

## Operator follow-up (not code)
After merge + `alembic upgrade head`: run the interactive loop — `create_paper_account(initial_capital_krw=…)` → live crypto analysis via `route_request(market="crypto")` → `paper_place_limit_order(confirm=True)` → `paper_reconcile_orders` → existing `get_paper_performance`/positions → retrospect.

## Roadmap (out of scope)
Background fill engine (auto reconcile), partial fills, orderbook-depth fills, auto retrospective/forecast wiring, scheduling/headless automation.

## Self-Review
- Spec coverage: fill engine→Task 1, pending table→Task 2, service (place/reconcile/cancel/list)→Task 3, MCP tools + matrix→Task 4. ✓
- Reuse honored: accounts/market-orders/positions/PnL/analytics/tools untouched; only resting-limit added.
- Set-equality profile guard edited in BOTH required places (Task 4 steps 2-3).
- Key correctness flagged: reservation released before `execute_order` re-charges (no double-deduct); `execute_order(limit, price=fill)` books at exactly the fill price (no live re-fetch).
- Open (verify in Task 3/4): the exact `execute_order` return shape for `paper_trade_id` linkage; the existing `paper_order_handler` tool names (to avoid dup) — Task 4 adds only limit+reconcile tools.
