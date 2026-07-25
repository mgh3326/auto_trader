# ROB-744 Mirror Pairing And Cohort Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ROB-734 mirror counterfactual data accumulate usable paired samples by closing mock mirror cohorts even when the exit sell is an ordinary unstamped `kis_mock` sell, and by surfacing pairability health when live orders are missing the report-item key.

**Architecture:** Keep the existing write schema unchanged. Fix the read model in `app/services/trade_journal/aggregates.py` by projecting unstamped KIS mock sells onto open mirror lots with a conservative FIFO ownership rule. Then add counterfactual pairing diagnostics and a health threshold to the delta scoreboard so `paired_count == 0` cannot look like a valid neutral result after enough closed samples.

**Tech Stack:** Python 3.13, SQLAlchemy async, existing `KISMockOrderLedger` read model, FastMCP tool handlers, pytest async. Reuses `load_fills`, `pair_fills_fifo`, `build_counterfactual_delta_scoreboard`, `get_trading_scoreboard`, and `get_operating_briefing`.

## Global Constraints

- No schema change for ROB-744; `ux_kis_mock_mirror_report_item_once` means an exit sell must not reuse the entry `report_item_uuid`.
- Do not stamp ordinary mock sell rows in place during scoreboard reads; all ownership assignment is a read-time projection.
- Explicit `mirror_cohort='mock_counterfactual'` rows are always mirror-owned.
- Unstamped `kis_mock` sells are mirror-owned only for the quantity that consumes an already-open mirror buy lot under account/symbol FIFO.
- Older non-mirror mock lots keep FIFO priority over later mirror lots for unstamped sells.
- Live pairing natural key is `report_item_uuid`; live `correlation_id` is place-time and account-scoped, so it is not expected to equal `mirror:{item_uuid}`.
- Public response changes must be backward compatible: add fields, do not rename or remove existing `paired_count`, `overall_delta`, `live_gated`, or `mock_counterfactual`.
- MCP docs must state that live orders originating from an investment report item must pass that item's `item_uuid` as `report_item_uuid`.

---

## File Structure

- **Modify** `app/services/trade_journal/aggregates.py` - KIS mock mirror cohort projection, pairability diagnostics, and `min_pair_threshold` health.
- **Modify** `app/mcp_server/tooling/trading_scoreboard_tools.py` - forward `min_pair_threshold` into the delta builder.
- **Modify** `app/mcp_server/tooling/trading_scoreboard_registration.py` - document the new optional threshold in the tool registration description if descriptions are declared there.
- **Modify** `app/mcp_server/tooling/operating_briefing.py` - rely on the delta builder default and preserve fail-open behavior.
- **Modify** `app/mcp_server/README.md` - update `get_trading_scoreboard`, `get_operating_briefing`, and order linkage notes.
- **Test** `tests/services/test_trade_journal_mirror_aggregates.py` - cohort closure and FIFO ownership tests.
- **Test** `tests/services/test_trade_journal_aggregates_scoreboard.py` - pairing diagnostics and threshold health tests.
- **Test** `tests/test_mcp_trading_scoreboard.py` - MCP forwarding of `min_pair_threshold`.
- **Test** `tests/mcp_server/test_operating_briefing_tools.py` - default delta response remains accepted by operating briefing.

---

### Task 1: Read-Time Mirror Cohort Closure

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_mirror_aggregates.py`

**Interfaces:**
- Create internal helper `_load_mock_counterfactual_fills(db, *, market, date_from, date_to) -> list[Fill]`.
- Create internal helper `_mock_fill_from_row(row, *, qty, fee, side, cohort, source_bucket, item_uuid, correlation_id) -> Fill | None`.
- Keep public `load_fills(...) -> list[Fill]` unchanged.
- Ownership rule for unstamped sells:
  - Track all KIS mock filled buys by `(market, account, symbol)` FIFO.
  - Mirror buys are lots where `mirror_cohort == "mock_counterfactual"`.
  - Non-mirror buys are lots where `mirror_cohort is None`.
  - An unstamped sell emits a mirror sell `Fill` only for quantity consumed from mirror lots.
  - An explicit mirror sell emits a mirror sell `Fill` and consumes mirror lots first.

- [ ] **Step 1: Add regression tests for unstamped sell closure**

Append these tests to `tests/services/test_trade_journal_mirror_aggregates.py`:

```python
@pytest.mark.asyncio
async def test_unstamped_mock_sell_closes_open_mirror_buy_for_counterfactual(
    db_session, monkeypatch
):
    async def no_excursions(trade):
        return None, None, False

    monkeypatch.setattr(agg, "compute_excursions", no_excursions)
    item_uuid = uuid4()
    buy_ts = datetime(2026, 7, 6, 1, tzinfo=UTC)
    sell_ts = datetime(2026, 7, 7, 1, tzinfo=UTC)
    db_session.add_all(
        [
            KISMockOrderLedger(
                trade_date=buy_ts,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("2"),
                price=Decimal("100"),
                amount=Decimal("200"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MIRROR-BUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "2"},
                report_item_uuid=item_uuid,
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id=f"mirror:{item_uuid}",
            ),
            KISMockOrderLedger(
                trade_date=sell_ts,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("2"),
                price=Decimal("110"),
                amount=Decimal("220"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"UNSTAMPED-SELL-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "2"},
                correlation_id="manual-mock-exit",
            ),
        ]
    )
    await db_session.flush()

    fills = await agg.load_fills(db_session, market="kr", cohort="mock_counterfactual")
    assert [(f.side, f.qty, f.cohort) for f in fills if f.symbol == "005930"] == [
        ("buy", pytest.approx(2.0), "mock_counterfactual"),
        ("sell", pytest.approx(2.0), "mock_counterfactual"),
    ]
    trades = agg.pair_fills_fifo(fills)
    assert len([t for t in trades if t.symbol == "005930"]) == 1
    trade = [t for t in trades if t.symbol == "005930"][0]
    assert trade.entry_item_uuids == (str(item_uuid),)
    assert trade.pnl_pct == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_unstamped_mock_sell_respects_older_non_mirror_fifo_lot(db_session):
    item_uuid = uuid4()
    ts0 = datetime(2026, 7, 6, 1, tzinfo=UTC)
    ts1 = datetime(2026, 7, 6, 2, tzinfo=UTC)
    ts2 = datetime(2026, 7, 6, 3, tzinfo=UTC)
    db_session.add_all(
        [
            KISMockOrderLedger(
                trade_date=ts0,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("90"),
                amount=Decimal("90"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"PRACTICE-BUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                correlation_id="practice-entry",
            ),
            KISMockOrderLedger(
                trade_date=ts1,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MIRROR-BUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                report_item_uuid=item_uuid,
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id=f"mirror:{item_uuid}",
            ),
            KISMockOrderLedger(
                trade_date=ts2,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("110"),
                amount=Decimal("110"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"UNSTAMPED-SELL-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                correlation_id="manual-practice-exit",
            ),
        ]
    )
    await db_session.flush()

    fills = await agg.load_fills(db_session, market="kr", cohort="mock_counterfactual")
    assert [(f.side, f.qty) for f in fills if f.symbol == "005930"] == [
        ("buy", pytest.approx(1.0))
    ]
    assert agg.pair_fills_fifo(fills) == []
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
uv run pytest tests/services/test_trade_journal_mirror_aggregates.py -k "unstamped_mock_sell" -v
```

Expected: FAIL because current `load_fills` only loads `KISMockOrderLedger.mirror_cohort == "mock_counterfactual"` rows.

- [ ] **Step 3: Add internal projection dataclass and helpers**

In `app/services/trade_journal/aggregates.py`, add near `_Lot`:

```python
@dataclass
class _MockAttributionLot:
    qty: float
    orig_qty: float
    is_mirror: bool
    source_bucket: str | None
    item_uuid: str | None
    correlation_id: str | None
```

Add this helper below `_coerce_uuid`:

```python
def _mock_market_for(row: KISMockOrderLedger) -> str:
    return "kr" if row.instrument_type == InstrumentType.equity_kr else "us"


def _mock_fill_from_row(
    row: KISMockOrderLedger,
    *,
    qty: float,
    fee: float,
    side: str,
    cohort: str,
    source_bucket: str | None,
    item_uuid: str | None,
    correlation_id: str | None,
) -> Fill | None:
    if qty <= _EPS or row.trade_date is None or float(row.price) <= _EPS:
        return None
    return Fill(
        market=_mock_market_for(row),
        symbol=to_db_symbol(row.symbol),
        account="kis_mock",
        side=side,
        qty=qty,
        price=float(row.price),
        fee=fee,
        ts=row.trade_date,
        item_uuid=item_uuid,
        correlation_id=correlation_id,
        source="kis_mock",
        cohort=cohort,
        source_bucket=source_bucket,
    )
```

- [ ] **Step 4: Implement `_load_mock_counterfactual_fills`**

Still in `aggregates.py`, add:

```python
async def _load_mock_counterfactual_fills(
    db: AsyncSession,
    *,
    market: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Fill]:
    from app.mcp_server.tooling.kis_mock_ledger import _derive_shadow_fill

    stmt = (
        select(KISMockOrderLedger)
        .where(KISMockOrderLedger.lifecycle_state == "fill")
        .order_by(KISMockOrderLedger.trade_date.asc(), KISMockOrderLedger.id.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    open_lots: dict[tuple[str, str, str], deque[_MockAttributionLot]] = defaultdict(
        deque
    )
    fills: list[Fill] = []

    def in_requested_window(row: KISMockOrderLedger) -> bool:
        if row.trade_date is None:
            return False
        d = row.trade_date.date()
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    for row in rows:
        row_market = _mock_market_for(row)
        if market and row_market != market:
            continue

        filled_qty, _remaining, status = _derive_shadow_fill(row, float(row.quantity))
        if status not in {"filled", "partial"} or filled_qty <= _EPS:
            continue

        key = (row_market, "kis_mock", to_db_symbol(row.symbol))
        is_mirror = row.mirror_cohort == "mock_counterfactual"
        item_uuid = str(row.report_item_uuid) if row.report_item_uuid else None
        row_fee = float(row.fee or 0)

        if row.side == "buy":
            open_lots[key].append(
                _MockAttributionLot(
                    qty=filled_qty,
                    orig_qty=filled_qty,
                    is_mirror=is_mirror,
                    source_bucket=row.mirror_source_bucket if is_mirror else None,
                    item_uuid=item_uuid if is_mirror else None,
                    correlation_id=row.correlation_id if is_mirror else None,
                )
            )
            if is_mirror and in_requested_window(row):
                fill = _mock_fill_from_row(
                    row,
                    qty=filled_qty,
                    fee=row_fee,
                    side="buy",
                    cohort="mock_counterfactual",
                    source_bucket=row.mirror_source_bucket,
                    item_uuid=item_uuid,
                    correlation_id=row.correlation_id,
                )
                if fill is not None:
                    fills.append(fill)
            continue

        if row.side != "sell":
            continue

        remaining = filled_qty
        attributed_qty = 0.0
        attributed_fee = 0.0

        if is_mirror:
            mirror_lots = open_lots[key]
            for lot in list(mirror_lots):
                if remaining <= _EPS:
                    break
                if not lot.is_mirror:
                    continue
                take = min(remaining, lot.qty)
                lot.qty -= take
                remaining -= take
                attributed_qty += take
                attributed_fee += row_fee * (take / filled_qty)
            while mirror_lots and mirror_lots[0].qty <= _EPS:
                mirror_lots.popleft()
        else:
            lots = open_lots[key]
            while remaining > _EPS and lots:
                lot = lots[0]
                take = min(remaining, lot.qty)
                lot.qty -= take
                remaining -= take
                if lot.is_mirror:
                    attributed_qty += take
                    attributed_fee += row_fee * (take / filled_qty)
                if lot.qty <= _EPS:
                    lots.popleft()

        if attributed_qty > _EPS and in_requested_window(row):
            fill = _mock_fill_from_row(
                row,
                qty=attributed_qty,
                fee=attributed_fee,
                side="sell",
                cohort="mock_counterfactual",
                source_bucket=row.mirror_source_bucket if is_mirror else None,
                item_uuid=item_uuid if is_mirror else None,
                correlation_id=row.correlation_id,
            )
            if fill is not None:
                fills.append(fill)

    return fills
```

- [ ] **Step 5: Route `load_fills` through the projection**

Replace the current mock block in `load_fills` with:

```python
    if cohort in ("mock_counterfactual", "all") and account_mode in (None, "kis_mock"):
        fills.extend(
            await _load_mock_counterfactual_fills(
                db,
                market=market,
                date_from=date_from,
                date_to=date_to,
            )
        )
```

Keep the final filter:

```python
return [f for f in fills if f.price > 0 and f.ts is not None]
```

- [ ] **Step 6: Verify Task 1**

Run:

```bash
uv run pytest tests/services/test_trade_journal_mirror_aggregates.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_mirror_aggregates.py
git commit -m "fix(ROB-744): close mirror cohort from unstamped mock exits"
```

---

### Task 2: Pairability Diagnostics And Health Threshold

**Files:**
- Modify: `app/services/trade_journal/aggregates.py`
- Test: `tests/services/test_trade_journal_aggregates_scoreboard.py`

**Interfaces:**
- `build_counterfactual_delta_scoreboard(..., min_pair_threshold: int = 20) -> dict[str, Any]`
- Add response key:
  - `pairing_diagnostics`: counts for live/mock closed trades, pairable keys, unpaired counts, and missing live `report_item_uuid`.
  - `pairing_health`: `ok`, `warming_up`, or `needs_design_review`.
- Do not change `paired_count` or `overall_delta`.

- [ ] **Step 1: Add scoreboard regression tests**

Append to `tests/services/test_trade_journal_aggregates_scoreboard.py`:

```python
@pytest.mark.asyncio
async def test_delta_scoreboard_pairs_when_mock_exit_is_unstamped(
    db_session, monkeypatch
):
    from decimal import Decimal
    from uuid import uuid4

    from app.models.review import KISLiveOrderLedger, KISMockOrderLedger
    from app.models.trading import InstrumentType

    async def no_excursions(trade):
        return None, None, False

    monkeypatch.setattr(agg, "compute_excursions", no_excursions)
    item_uuid = uuid4()
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=ts1,
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("100"),
                account_mode="kis_live",
                broker="kis",
                correlation_id="live-entry",
                report_item_uuid=item_uuid,
            ),
            KISLiveOrderLedger(
                trade_date=ts2,
                symbol="005930",
                instrument_type="equity_kr",
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("105"),
                amount=Decimal("105"),
                status="filled",
                lifecycle_state="fill",
                filled_qty=Decimal("1"),
                avg_fill_price=Decimal("105"),
                account_mode="kis_live",
                broker="kis",
                correlation_id="live-exit",
            ),
            KISMockOrderLedger(
                trade_date=ts1,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="buy",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("100"),
                amount=Decimal("100"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MBUY-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                mirror_cohort="mock_counterfactual",
                mirror_source_bucket="place_original",
                correlation_id=f"mirror:{item_uuid}",
                report_item_uuid=item_uuid,
            ),
            KISMockOrderLedger(
                trade_date=ts2,
                symbol="005930",
                instrument_type=InstrumentType.equity_kr,
                side="sell",
                order_type="limit",
                quantity=Decimal("1"),
                price=Decimal("110"),
                amount=Decimal("110"),
                fee=Decimal("0"),
                currency="KRW",
                order_no=f"MSELL-{uuid4().hex[:8]}",
                account_mode="kis_mock",
                broker="kis",
                status="accepted",
                lifecycle_state="fill",
                last_reconcile_detail={"attributed_fill_qty": "1"},
                correlation_id="manual-mock-exit",
            ),
        ]
    )
    await db_session.flush()

    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        include_excursions=False,
        use_cache=False,
        min_pair_threshold=1,
    )
    assert result["paired_count"] == 1
    assert result["overall_delta"]["paired_n"] == 1
    assert result["pairing_health"]["status"] == "ok"


@pytest.mark.asyncio
async def test_delta_scoreboard_health_flags_closed_but_unpaired_samples(
    db_session, monkeypatch
):
    ts1 = datetime(2026, 7, 1, tzinfo=UTC)
    ts2 = datetime(2026, 7, 2, tzinfo=UTC)

    async def fake_load_fills(db, **kw):
        return [
            agg.Fill("kr", "005930", "kis_live", "buy", 1, 100, 0, ts1, None, "live-a", "kis", "live_gated"),
            agg.Fill("kr", "005930", "kis_live", "sell", 1, 105, 0, ts2, None, "live-b", "kis", "live_gated"),
            agg.Fill("kr", "005930", "kis_mock", "buy", 1, 100, 0, ts1, "item-1", "mirror:item-1", "kis_mock", "mock_counterfactual"),
            agg.Fill("kr", "005930", "kis_mock", "sell", 1, 110, 0, ts2, None, "manual-exit", "kis_mock", "mock_counterfactual"),
        ]

    monkeypatch.setattr(agg, "load_fills", fake_load_fills)
    result = await agg.build_counterfactual_delta_scoreboard(
        db_session,
        market="kr",
        include_excursions=False,
        use_cache=False,
        min_pair_threshold=1,
    )
    assert result["paired_count"] == 0
    assert result["pairing_diagnostics"]["live_trades_without_report_item_uuid"] == 1
    assert result["pairing_diagnostics"]["unpaired_mock_count"] == 1
    assert result["pairing_health"]["status"] == "needs_design_review"
    assert any("report_item_uuid" in caveat for caveat in result["caveats"])
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py -k "unstamped or health_flags" -v
```

Expected: FAIL because diagnostics and `min_pair_threshold` do not exist.

- [ ] **Step 3: Implement diagnostics helpers**

Add below `_pair_by_entry_provenance`:

```python
def _key_set(trade: ClosedTrade) -> set[tuple[str, str]]:
    return set(_entry_pair_keys(trade))


def _pairing_diagnostics(
    live_trades: list[ClosedTrade],
    mock_trades: list[ClosedTrade],
    paired: list[tuple[ClosedTrade, ClosedTrade]],
) -> dict[str, Any]:
    paired_live_ids = {id(live) for live, _ in paired}
    paired_mock_ids = {id(mock) for _, mock in paired}
    live_item_keys = {
        key[1]
        for trade in live_trades
        for key in _entry_pair_keys(trade)
        if key[0] == "report_item_uuid"
    }
    mock_item_keys = {
        key[1]
        for trade in mock_trades
        for key in _entry_pair_keys(trade)
        if key[0] == "report_item_uuid"
    }
    unpaired_mock = [trade for trade in mock_trades if id(trade) not in paired_mock_ids]
    unpaired_live = [trade for trade in live_trades if id(trade) not in paired_live_ids]
    return {
        "live_closed_count": len(live_trades),
        "mock_closed_count": len(mock_trades),
        "live_pair_key_count": sum(len(_key_set(t)) for t in live_trades),
        "mock_pair_key_count": sum(len(_key_set(t)) for t in mock_trades),
        "paired_count": len(paired),
        "unpaired_live_count": len(unpaired_live),
        "unpaired_mock_count": len(unpaired_mock),
        "live_trades_without_report_item_uuid": sum(
            1 for trade in live_trades if not trade.entry_item_uuids
        ),
        "mock_report_item_keys_without_live_match": sorted(
            mock_item_keys - live_item_keys
        )[:20],
    }


def _pairing_health(
    diagnostics: dict[str, Any],
    *,
    min_pair_threshold: int,
) -> dict[str, Any]:
    observed = min(
        int(diagnostics["live_closed_count"]),
        int(diagnostics["mock_closed_count"]),
    )
    paired_count = int(diagnostics["paired_count"])
    if paired_count >= min_pair_threshold:
        status = "ok"
        reason = None
    elif observed >= min_pair_threshold:
        status = "needs_design_review"
        reason = "closed_samples_available_but_pairing_below_threshold"
    else:
        status = "warming_up"
        reason = "below_min_pair_threshold"
    return {
        "status": status,
        "min_pair_threshold": min_pair_threshold,
        "observed_closed_trade_floor": observed,
        "paired_count": paired_count,
        "reason": reason,
    }
```

- [ ] **Step 4: Wire diagnostics into `build_counterfactual_delta_scoreboard`**

Change the signature:

```python
async def build_counterfactual_delta_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
    min_pair_threshold: int = 20,
    include_excursions: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
```

After pair filtering, add:

```python
    diagnostics = _pairing_diagnostics(live_trades, mock_trades, paired)
    health = _pairing_health(
        diagnostics, min_pair_threshold=max(1, int(min_pair_threshold))
    )
    caveats = [
        "KIS mock fills do not model queue priority, liquidity, slippage, or market impact; mock performance is upward biased."
    ]
    if health["status"] == "needs_design_review":
        caveats.append(
            "Counterfactual closed samples exist but paired_count is below min_pair_threshold; verify live report_item_uuid tagging from investment report items."
        )
```

Add fields to the return dict:

```python
        "pairing_diagnostics": diagnostics,
        "pairing_health": health,
```

Add the threshold to `filters`:

```python
            "min_pair_threshold": max(1, int(min_pair_threshold)),
```

Return `caveats` instead of the previous hard-coded list.

- [ ] **Step 5: Verify Task 2**

Run:

```bash
uv run pytest tests/services/test_trade_journal_aggregates_scoreboard.py tests/services/test_trade_journal_mirror_aggregates.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/trade_journal/aggregates.py tests/services/test_trade_journal_aggregates_scoreboard.py
git commit -m "feat(ROB-744): surface counterfactual pairing health"
```

---

### Task 3: MCP Parameter And Documentation Sync

**Files:**
- Modify: `app/mcp_server/tooling/trading_scoreboard_tools.py`
- Modify: `app/mcp_server/tooling/trading_scoreboard_registration.py`
- Modify: `app/mcp_server/tooling/operating_briefing.py`
- Modify: `app/mcp_server/README.md`
- Test: `tests/test_mcp_trading_scoreboard.py`
- Test: `tests/mcp_server/test_operating_briefing_tools.py`

**Interfaces:**
- `get_trading_scoreboard(..., min_pair_threshold: int = 20, include_counterfactual_delta: bool = False)` forwards the threshold only when `include_counterfactual_delta=True`.
- `get_operating_briefing(..., include_counterfactual_delta=True)` keeps using the builder default `min_pair_threshold=20`.
- README explicitly states that report-originated live orders must pass `report_item_uuid`.

- [ ] **Step 1: Add MCP forwarding test**

Update `tests/test_mcp_trading_scoreboard.py::test_scoreboard_tool_calls_counterfactual_delta` so the fake captures `min_pair_threshold`:

```python
@pytest.mark.asyncio
async def test_scoreboard_tool_calls_counterfactual_delta(monkeypatch):
    calls = {}

    async def fake_delta(db, **kwargs):
        calls.update(kwargs)
        return {
            "paired_count": 5,
            "overall_delta": {},
            "pairing_health": {"status": "ok"},
            "pairing_diagnostics": {},
            "caveats": [],
        }

    monkeypatch.setattr(tool, "build_counterfactual_delta_scoreboard", fake_delta)
    result = await tool.get_trading_scoreboard(
        market="kr",
        setup_tag="breakout",
        min_sample=2,
        min_pair_threshold=7,
        include_counterfactual_delta=True,
    )

    assert result["paired_count"] == 5
    assert calls["market"] == "kr"
    assert calls["setup_tag"] == "breakout"
    assert calls["min_sample"] == 2
    assert calls["min_pair_threshold"] == 7
```

- [ ] **Step 2: Run MCP test and confirm failure**

Run:

```bash
uv run pytest tests/test_mcp_trading_scoreboard.py::test_scoreboard_tool_calls_counterfactual_delta -v
```

Expected: FAIL because `get_trading_scoreboard` does not accept or forward `min_pair_threshold`.

- [ ] **Step 3: Update the tool signature**

In `app/mcp_server/tooling/trading_scoreboard_tools.py`, change the signature:

```python
async def get_trading_scoreboard(
    market: str | None = None,
    account_mode: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
    cohort: str = "live_gated",
    min_pair_threshold: int = 20,
    include_counterfactual_delta: bool = False,
) -> dict:
```

Pass it into the delta builder:

```python
                    min_pair_threshold=max(1, int(min_pair_threshold)),
```

Do not pass it to `build_trading_scoreboard` when `include_counterfactual_delta=False`.

- [ ] **Step 4: Update registration text if present**

If `app/mcp_server/tooling/trading_scoreboard_registration.py` describes parameters, include:

```python
"min_pair_threshold: optional counterfactual pairing health threshold; default 20. "
```

If the file only registers the callable, leave it unchanged.

- [ ] **Step 5: Update README contract**

In `app/mcp_server/README.md`, replace the `get_trading_scoreboard` counterfactual line with:

```markdown
- `include_counterfactual_delta`: default `false`. When `true`, returns aggregates delta scoreboard comparing `live_gated` and `mock_counterfactual` paired by shared `report_item_uuid` where available. `correlation_id` is still considered for legacy rows, but live place-time IDs are account-scoped and should not be expected to equal `mirror:{item_uuid}`.
- `min_pair_threshold`: default `20`. Only affects `pairing_health`; it does not filter rows.
```

Add this to the order linkage note:

```markdown
For report-originated live orders, passing `report_item_uuid` is required for counterfactual pairing; without it, `paired_count` can remain zero even when both live and mock cohorts have closed trades.
```

Add this to response fields:

```markdown
- `pairing_diagnostics`: closed-trade and key-coverage counts used to explain why pairs did or did not form.
- `pairing_health`: `ok`, `warming_up`, or `needs_design_review` based on `paired_count`, closed sample availability, and `min_pair_threshold`.
```

- [ ] **Step 6: Verify Task 3**

Run:

```bash
uv run pytest tests/test_mcp_trading_scoreboard.py tests/mcp_server/test_operating_briefing_tools.py -v
rg -n "min_pair_threshold|pairing_health|report_item_uuid.*counterfactual" app/mcp_server/README.md app/mcp_server/tooling/trading_scoreboard_tools.py
```

Expected: PASS for tests; `rg` finds the updated contract text.

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/trading_scoreboard_tools.py app/mcp_server/tooling/trading_scoreboard_registration.py app/mcp_server/tooling/operating_briefing.py app/mcp_server/README.md tests/test_mcp_trading_scoreboard.py tests/mcp_server/test_operating_briefing_tools.py
git commit -m "docs(ROB-744): expose counterfactual pairability contract"
```

---

### Task 4: Final Verification

**Files:**
- No new files.

**Interfaces:**
- All ROB-734 and ROB-744 focused tests pass.
- Lint passes for touched Python files.

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
uv run pytest \
  tests/services/test_trade_journal_mirror_aggregates.py \
  tests/services/test_trade_journal_aggregates_scoreboard.py \
  tests/test_mcp_trading_scoreboard.py \
  tests/mcp_server/test_operating_briefing_tools.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run adjacent mirror execution tests**

Run:

```bash
uv run pytest \
  tests/services/test_mirror_counterfactual_execution.py \
  tests/services/test_mirror_counterfactual_plans.py \
  tests/mcp_server/test_mirror_counterfactual_tool.py \
  -v
```

Expected: PASS.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
uv run ruff check \
  app/services/trade_journal/aggregates.py \
  app/mcp_server/tooling/trading_scoreboard_tools.py \
  app/mcp_server/tooling/trading_scoreboard_registration.py \
  app/mcp_server/tooling/operating_briefing.py \
  tests/services/test_trade_journal_mirror_aggregates.py \
  tests/services/test_trade_journal_aggregates_scoreboard.py \
  tests/test_mcp_trading_scoreboard.py \
  tests/mcp_server/test_operating_briefing_tools.py
```

Expected: PASS.

- [ ] **Step 4: Optional full unit sweep**

Run:

```bash
make test-unit
```

Expected: PASS. If this is too slow for the branch, record the focused test commands above in the final handoff.

- [ ] **Step 5: Commit final verification note if code changed after Task 3**

```bash
git status --short
git add -A
git commit -m "test(ROB-744): verify mirror counterfactual pairing"
```

Only create this commit if there are actual verification-driven source or test changes after the previous task commits.

---

## Self-Review

- Spec coverage:
  - Pairing key weakness is addressed by documenting `report_item_uuid` as the live natural key and by adding diagnostics when that key is absent.
  - Mock cohort closure is addressed by read-time FIFO projection of unstamped sell rows onto open mirror lots.
  - Measurement lower bound is addressed by `min_pair_threshold` and `pairing_health`.
- No schema mutation is planned, because the existing unique index allows exactly one mirror row per `report_item_uuid`.
- No live order execution behavior changes are planned; the contract is surfaced through docs and diagnostics.
- The read projection is conservative: it does not claim unstamped sells when an older non-mirror lot consumes the sell first.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-06-rob-744-mirror-pairing-cohort-closure.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
