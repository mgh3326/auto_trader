# Upbit Shadow-Sim — Reconcile Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the 3 confirmed defects in `PaperLimitOrderService.reconcile_pending_orders` / `place_limit_order` (2 blockers + 1 major) surfaced by adversarial review, and close the false-green test gaps that hid them.

**Architecture:** All changes are local to `app/services/paper_limit_order_service.py` + its test file. No schema/migration/MCP change. Existing `PaperTradingService.execute_order` is NOT modified.

**Tech Stack:** Python 3.13, SQLAlchemy async + Postgres, pytest (`@pytest.mark.asyncio` + `db_session`), ruff (88, py313) + ty.

## Global Constraints

- Do NOT modify `PaperTradingService.execute_order` (shared by existing callers). Fixes live in `paper_limit_order_service.py`.
- `KST` timezone: `from app.core.timezone import KST` (`timezone(timedelta(hours=9))`). `now_kst()` is tz-aware; `placed_at` (timestamptz) round-trips tz-aware; real Upbit `Candle.timestamp` is tz-**naive** KST wall-clock.
- `execute_order` commits internally (`paper_trading_service.py:405`) and raises `ValueError` on insufficient cash/position/qty. It returns `{"success",...,"execution":{...}}` with **no** trade id — the trade id is recovered via the existing `_latest_trade_id` query.
- Reservation invariant: on a buy fill, `reserved_krw` (computed at snapped limit price incl. fee) equals `execute_order`'s `total_cost` at the same price/qty, so release-then-execute nets a single charge.
- Lint gate: `uv run ruff check app/ tests/` + `uv run ruff format --check` + `uv run ty check app/ --error-on-warning`. Tests: `uv run --all-groups pytest <file> -v --no-cov`.
- Every commit ends with the standard `Co-Authored-By: Claude Opus 4.8 (1M context)` + `Claude-Session:` trailer lines.

---

## Task 1: tz-safe reconcile comparison (BLOCKER #1) + close the false-green

**Files:**
- Modify: `app/services/paper_limit_order_service.py` (add `_as_aware_kst` helper; use it in `reconcile_pending_orders` bar filter)
- Modify: `tests/services/test_paper_limit_order_service.py` (`_candle` helper accepts a timestamp; add tz-naive regression test)

**Interfaces:**
- Produces: `_as_aware_kst(ts: dt.datetime | None) -> dt.datetime | None` — returns `ts` unchanged if aware, `ts.replace(tzinfo=KST)` if naive, `None` if `None`.

- [ ] **Step 1: Write the failing regression test.** First change the `_candle` helper to accept a timestamp, then add the test.

```python
# tests/services/test_paper_limit_order_service.py — replace _candle (lines 21-29)
import datetime as dt
from app.core.timezone import KST, now_kst

def _candle(low: Decimal, high: Decimal, timestamp: dt.datetime | None = None) -> Any:
    class _C:
        pass
    c = _C()
    c.low = low
    c.high = high
    c.timestamp = timestamp
    return c
```

```python
# tests/services/test_paper_limit_order_service.py — add this test
@pytest.mark.asyncio
async def test_reconcile_handles_tz_naive_upbit_timestamps(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Upbit candles carry tz-NAIVE timestamps; placed_at is tz-aware.
    Reconcile must not raise TypeError comparing them (blocker #1)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-tz"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(
        account_id=acct.id, symbol="KRW-BTC", side="buy",
        limit_price=Decimal("90000000"), amount=Decimal("100000"),
    )
    # candle AFTER placement, tz-NAIVE (as real Upbit candle_date_time_kst is), low crosses
    naive_after = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)
    async def _bars(symbol, market, period, count, end=None):
        return [_candle(Decimal("89000000"), Decimal("91000000"), naive_after)]
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 1, res  # must fill, not crash

@pytest.mark.asyncio
async def test_reconcile_excludes_bars_before_placement(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit must not fill on price action BEFORE it was placed (no look-ahead)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-pre"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(
        account_id=acct.id, symbol="KRW-BTC", side="buy",
        limit_price=Decimal("90000000"), amount=Decimal("100000"),
    )
    # only a crossing candle BEFORE placement exists -> must stay pending
    naive_before = now_kst().replace(tzinfo=None) - dt.timedelta(minutes=5)
    async def _bars(symbol, market, period, count, end=None):
        return [_candle(Decimal("89000000"), Decimal("91000000"), naive_before)]
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 0, res
    assert len(await svc.list_pending_orders(account_id=acct.id)) == 1
```

- [ ] **Step 2: Run — expect FAIL** (first test raises TypeError, or both mis-behave). `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov -k "tz_naive or before_placement"`

- [ ] **Step 3: Add the helper + use it.** In `app/services/paper_limit_order_service.py`, add the import `from app.core.timezone import KST, now_kst` (keep `now_kst`), and add the helper near `_quantize_fill_price`:

```python
def _as_aware_kst(ts: dt.datetime | None) -> dt.datetime | None:
    """Upbit candle timestamps (candle_date_time_kst) are tz-naive KST wall-clock;
    placed_at is tz-aware. Coerce naive -> KST-aware so comparisons never raise."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=KST)
    return ts
```

Then in `reconcile_pending_orders`, replace the bar-building block (current lines 336-341):

```python
            placed_at = _as_aware_kst(order.placed_at)
            bars: list[tuple[Decimal, Decimal]] = []
            for c in candles:
                c_ts = _as_aware_kst(getattr(c, "timestamp", None))
                if c_ts is not None and placed_at is not None and c_ts < placed_at:
                    continue
                bars.append((Decimal(str(c.low)), Decimal(str(c.high))))
```

- [ ] **Step 4: Run — expect PASS. Lint.** `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov` ; `uv run ruff check app/services/paper_limit_order_service.py tests/services/test_paper_limit_order_service.py && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add app/services/paper_limit_order_service.py tests/services/test_paper_limit_order_service.py && git commit -m "fix(ROB-703): tz-safe reconcile comparison (blocker) + tz-naive regression tests"`

---

## Task 2: atomic per-order fill + failure isolation (BLOCKER #2)

**Files:**
- Modify: `app/services/paper_limit_order_service.py` (`reconcile_pending_orders` fill block)
- Modify: `tests/services/test_paper_limit_order_service.py` (multi-order-with-failure regression test)

**Interfaces:**
- Consumes: `_as_aware_kst` (Task 1), `_latest_trade_id`, `PaperTradingService.execute_order`.
- Behavior contract: for each crossed order, `order.status='filled'` commits **atomically with** the `execute_order` trade (single `execute_order.commit()`); a failure on any one order rolls back only that order and never aborts the batch or leaves a booked trade with `status='pending'`.

- [ ] **Step 1: Write the failing test.** A batch where the 2nd order oversells (execute_order raises); the 1st must fill exactly once and re-reconcile must NOT double-fill it.

```python
@pytest.mark.asyncio
async def test_reconcile_isolates_failure_no_double_fill(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing order in the batch must not abort the batch nor leave an
    already-booked trade re-fillable on the next reconcile (blocker #2)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-iso"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    # Order A: buy that will cross and fill fine.
    await svc.place_limit_order(
        account_id=acct.id, symbol="KRW-BTC", side="buy",
        limit_price=Decimal("90000000"), amount=Decimal("100000"),
    )
    ts = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)
    async def _bars(symbol, market, period, count, end=None):
        return [_candle(Decimal("89000000"), Decimal("91000000"), ts)]
    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    # Directly insert a crossing SELL pending order with no position -> execute_order raises.
    from app.models.paper_trading import PaperPendingOrder
    bad = PaperPendingOrder(
        account_id=acct.id, symbol="KRW-ETH", side="sell", order_type="limit",
        limit_price=Decimal("1000000"), quantity=Decimal("0.1"),
        reserved_krw=Decimal("0"), status="pending", placed_at=now_kst(),
    )
    db_session.add(bad)
    await db_session.commit()

    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 1, res  # A filled; the bad sell did not abort the batch

    # A must now be 'filled' and NOT re-fillable.
    trades1 = await pts.get_trade_history(acct.id, limit=50)
    btc_trades1 = [t for t in trades1 if t["symbol"] == "KRW-BTC"]
    assert len(btc_trades1) == 1, btc_trades1
    res2 = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    trades2 = await pts.get_trade_history(acct.id, limit=50)
    btc_trades2 = [t for t in trades2 if t["symbol"] == "KRW-BTC"]
    assert len(btc_trades2) == 1, f"double-fill: {btc_trades2}"
```
(If `get_trade_history`'s dict key is not `"symbol"`, read `app/services/paper_trading_service.py::get_trade_history` and use its actual keys.)

- [ ] **Step 2: Run — expect FAIL** (batch aborts on the bad sell and/or A double-fills). `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov -k "isolates_failure"`

- [ ] **Step 3: Rewrite the fill block.** Replace the current fill section of `reconcile_pending_orders` (current lines 347-379, from `fill_price = limit_crossed(...)` result handling through `filled += 1`) with:

```python
            fill_price = limit_crossed(order.side, Decimal(order.limit_price), bars)
            if fill_price is None:
                continue

            # Atomic per-order fill, isolated from the rest of the batch.
            # Flip status BEFORE execute_order so its internal commit persists
            # status='filled' + the trade in ONE transaction. A raise before
            # that commit rolls everything back -> order stays pending, no
            # phantom trade. execute_order failures (e.g. oversell) are caught
            # so one bad order never aborts the batch.
            try:
                account = await self.pts.get_account(account_id)
                assert account is not None
                if order.side == "buy":
                    account.cash_krw = quantize_money(
                        Decimal(account.cash_krw) + Decimal(order.reserved_krw)
                    )
                order.status = "filled"
                order.fill_price = fill_price
                order.filled_at = effective_now
                await self.pts.execute_order(
                    account_id=account_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type="limit",
                    price=fill_price,
                    quantity=Decimal(order.quantity),
                    reason=order.thesis or "paper resting-limit fill",
                )
                # trade + status now durably committed by execute_order; link FK best-effort
                order.paper_trade_id = await _latest_trade_id(
                    self.db,
                    account_id=account_id,
                    symbol=order.symbol,
                    side=order.side,
                )
                await self.db.commit()
                filled += 1
            except Exception:
                await self.db.rollback()
                continue

        return {
            "success": True,
            "reconciled": len(orders),
            "filled": filled,
        }
```

Remove the now-dead trailing `await self.db.commit()` / return that followed the loop (the return is now inside — ensure the method ends cleanly with the loop; the pre-loop early return `{"reconciled": 0, "filled": 0}` stays). Verify indentation: the `try/except` is inside `for order in orders:`, and the final `return` is after the `for` loop.

- [ ] **Step 4: Run — expect PASS. Full service suite + lint.** `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov` ; `uv run ruff check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add -A && git commit -m "fix(ROB-703): atomic per-order reconcile fill + failure isolation (blocker)"`

---

## Task 3: sell-side position reservation + min-notional on sell (MAJOR #3 + minor)

**Files:**
- Modify: `app/services/paper_limit_order_service.py` (`place_limit_order` sell branch + a `_pending_sell_qty` helper; align buy min-notional to gross)
- Modify: `tests/services/test_paper_limit_order_service.py` (concurrent-sell + sub-min tests)

**Interfaces:**
- Produces: `PaperLimitOrderService._pending_sell_qty(self, account_id: int, symbol: str) -> Decimal` — sum of `quantity` over `pending` `sell` orders for that (account, symbol).

- [ ] **Step 1: Write the failing tests.**

```python
@pytest.mark.asyncio
async def test_place_sell_reserves_position_across_pending(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two resting sells cannot jointly exceed the held position (major #3)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-sell"), initial_capital_krw=Decimal("100000000")
    )
    # establish a 0.002 BTC position via a market buy
    async def _price(symbol, itype):
        return Decimal("50000000")
    monkeypatch.setattr(pts, "_fetch_current_price", _price)
    await pts.execute_order(account_id=acct.id, symbol="KRW-BTC", side="buy",
                            order_type="market", quantity=Decimal("0.002"))
    svc = PaperLimitOrderService(db_session)
    first = await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC",
        side="sell", limit_price=Decimal("60000000"), quantity=Decimal("0.002"))
    assert first["success"], first
    second = await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC",
        side="sell", limit_price=Decimal("61000000"), quantity=Decimal("0.002"))
    assert not second["success"]
    assert "sellable" in second["error"].lower() or "insufficient" in second["error"].lower()

@pytest.mark.asyncio
async def test_place_sell_below_min_notional_rejected(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-min"), initial_capital_krw=Decimal("100000000")
    )
    async def _price(symbol, itype):
        return Decimal("50000000")
    monkeypatch.setattr(pts, "_fetch_current_price", _price)
    await pts.execute_order(account_id=acct.id, symbol="KRW-BTC", side="buy",
                            order_type="market", quantity=Decimal("0.01"))
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(account_id=acct.id, symbol="KRW-BTC",
        side="sell", limit_price=Decimal("50000000"), quantity=Decimal("0.00001"))  # 500 KRW
    assert not out["success"]
    assert "minimum" in out["error"].lower()
```

- [ ] **Step 2: Run — expect FAIL** (second sell currently succeeds; sub-min sell currently succeeds). `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py -v --no-cov -k "reserves_position or below_min_notional"`

- [ ] **Step 3: Add the helper + fix the sell branch.** Add `from sqlalchemy import func` to the imports (alongside `desc, select`). Add the helper on the class:

```python
    async def _pending_sell_qty(self, account_id: int, symbol: str) -> Decimal:
        stmt = select(func.coalesce(func.sum(PaperPendingOrder.quantity), 0)).where(
            PaperPendingOrder.account_id == account_id,
            PaperPendingOrder.symbol == symbol,
            PaperPendingOrder.side == "sell",
            PaperPendingOrder.status == "pending",
        )
        return Decimal(str((await self.db.execute(stmt)).scalar() or 0))
```

Replace the sell branch of `place_limit_order` (current lines 199-215) with:

```python
        else:
            reserved_krw_value = Decimal("0")
            gross = quantize_money(qty * snapped_price)
            if gross < _MIN_CRYPTO_KRW:
                return {
                    "success": False,
                    "error": (
                        f"Order notional {gross} KRW is below the "
                        f"Upbit minimum {_MIN_CRYPTO_KRW} KRW"
                    ),
                }
            position = await self.pts._get_position(account_id, resolved_symbol)
            if position is None:
                return {
                    "success": False,
                    "error": f"No position to sell for {resolved_symbol}",
                }
            pending_sell = await self._pending_sell_qty(account_id, resolved_symbol)
            available = Decimal(position.quantity) - pending_sell
            if available < qty:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient sellable quantity: position "
                        f"{position.quantity}, already-pending sells {pending_sell}, "
                        f"need {qty}"
                    ),
                }
```

Also align the BUY min-notional to `gross` (not `gross+fee`) for consistency — in the buy branch, after computing `gross`, change the guard to test `gross < _MIN_CRYPTO_KRW` (keep computing `reserved_krw = gross + fee` for the reservation). This is a one-line change to the existing `if reserved_krw < _MIN_CRYPTO_KRW:` → `if gross < _MIN_CRYPTO_KRW:`.

- [ ] **Step 4: Run — expect PASS. Full suite + lint + ty.** `uv run --all-groups pytest tests/services/test_paper_limit_order_service.py tests/services/test_paper_fills.py tests/test_mcp_paper_limit_order.py tests/test_mcp_profiles.py -v --no-cov` ; `uv run ruff check app/ tests/ && uv run ruff format --check app/ tests/ && uv run ty check app/ --error-on-warning`

- [ ] **Step 5: Commit.** `git add -A && git commit -m "fix(ROB-703): reserve position across pending sells + min-notional on sell (major)"`

---

## Self-Review
- Blocker #1 (tz crash) → Task 1 (`_as_aware_kst` + tz-naive/pre-placement tests). ✓
- Blocker #2 (non-atomic double-fill) → Task 2 (status flip before `execute_order` commit + per-order try/except/rollback + double-fill regression). ✓
- Major #3 (sell oversell) → Task 3 (`_pending_sell_qty` reservation + concurrent-sell test); also folds in sell min-notional (minor C) + buy min-notional alignment. ✓
- Minor D (`c_ts is None` inclusion): retained as conservative include-when-unknown; real Upbit candles always carry a timestamp post-Task-1, so this only affects synthetic candles. No task (documented).
- `execute_order` NOT modified (constraint honored). All fixes local to one service file + its tests.
- Type consistency: `_as_aware_kst`, `_pending_sell_qty` signatures used exactly as defined; `KST`/`func` imports added.
- Regression coverage closes the false-green: canned candles now carry real tz-naive timestamps; batch-with-failure and concurrent-sell paths are exercised.
```
