# ROB-559 — Per-symbol order history on StockDetailPage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]`.

**Spec:** Linear **ROB-559** (decisions locked there). **Goal:** show per-symbol order lifecycle (status + rationale + fill rollup) on `/invest/stocks/:market/:symbol`, reusing ROB-554's `LinkedOrderView` + `project_*` by **symbol** instead of `report_item_uuid`.

**Decisions:** source = 3 live order ledgers (live-only) · markets = all (KR/US/crypto) · rationale shown · v1 = order rollup (no per-fill drill-down) · **migration 0**.

**⚠️ Crypto symbol reality:** `review.live_order_ledger.symbol = 'KRW-BTC'` (full pair, upper). URL carries `KRW-BTC` → direct match. (execution_ledger uses `BTC`/`raw_symbol` — that's why order-ledger source is cleaner.) KR=6-digit, US=dot-upper, consistent.

**Base:** `origin/main` (worktree `/Users/mgh3326/work/auto_trader.rob-559`, branch `rob-559`).

---

## S1 — Backend `list_live_orders_for_symbol`
**Files:** modify `app/services/investment_reports/linked_orders.py`; test `tests/test_rob559_symbol_order_history.py`.

- Add `from datetime import UTC, datetime, timedelta` to imports.
- New fn (reuses existing `project_live_order`/`project_kis_live_order`/`project_toss_live_order`):
```python
async def list_live_orders_for_symbol(
    db: AsyncSession, market: str, symbol: str, *, days: int = 90, limit: int = 50
) -> list[LinkedOrderView]:
    """Per-symbol live order history across the 3 live ledgers (live-only).

    market routes the ledgers: crypto -> LiveOrderLedger(market='crypto');
    us -> LiveOrderLedger(market='us') + TossLiveOrderLedger(market='us');
    kr -> KISLiveOrderLedger + TossLiveOrderLedger(market='kr').
    Crypto symbols are the full Upbit pair ('KRW-BTC'); match LiveOrderLedger.symbol
    directly. Merged most-recent-first by created_at, capped at limit.
    """
    sym = symbol.strip().upper()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    collected: list[tuple[datetime, LinkedOrderView]] = []

    async def _live(market_value: str):
        rows = (await db.execute(
            select(LiveOrderLedger)
            .where(LiveOrderLedger.symbol == sym,
                   LiveOrderLedger.market == market_value,
                   LiveOrderLedger.created_at >= cutoff)
            .order_by(LiveOrderLedger.id.desc()).limit(limit))).scalars().all()
        for r in rows: collected.append((r.created_at, project_live_order(r)))

    async def _kis():
        rows = (await db.execute(
            select(KISLiveOrderLedger)
            .where(KISLiveOrderLedger.symbol == sym,
                   KISLiveOrderLedger.created_at >= cutoff)
            .order_by(KISLiveOrderLedger.id.desc()).limit(limit))).scalars().all()
        for r in rows: collected.append((r.created_at, project_kis_live_order(r)))

    async def _toss(market_value: str):
        rows = (await db.execute(
            select(TossLiveOrderLedger)
            .where(TossLiveOrderLedger.symbol == sym,
                   TossLiveOrderLedger.market == market_value,
                   TossLiveOrderLedger.created_at >= cutoff)
            .order_by(TossLiveOrderLedger.id.desc()).limit(limit))).scalars().all()
        for r in rows: collected.append((r.created_at, project_toss_live_order(r)))

    if market == "crypto":
        await _live("crypto")
    elif market == "us":
        await _live("us"); await _toss("us")
    elif market == "kr":
        await _kis(); await _toss("kr")
    else:
        return []

    collected.sort(key=lambda t: t[0], reverse=True)
    return [v for _, v in collected[:limit]]
```
- Tests: crypto KRW-BTC matches live ledger (not 'BTC'); us merges live+toss; kr merges kis+toss; days cutoff excludes old; limit caps; empty for unknown symbol/market.

## S2 — Endpoint `GET /stock-detail/{market}/{symbol}/order-ledger`
**Files:** modify `app/schemas/investment_reports.py` (add response model), `app/routers/invest_api.py`; test `tests/test_rob559_symbol_order_history.py` (router-level).

- Schema (next to `LinkedOrderView`):
```python
class StockDetailOrderLedgerResponse(BaseModel):
    """ROB-559 — per-symbol live order history for the stock detail page."""
    count: int
    items: list[LinkedOrderView]
    model_config = ConfigDict(extra="forbid")
```
- Route (after the `/orders` route, ~invest_api.py:523):
```python
@router.get("/stock-detail/{market}/{symbol}/order-ledger")
async def get_stock_detail_order_ledger(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = Query(90, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
) -> StockDetailOrderLedgerResponse:
    _ = user
    items = await list_live_orders_for_symbol(db, market=market, symbol=symbol, days=days, limit=limit)
    return StockDetailOrderLedgerResponse(count=len(items), items=items)
```
- Imports: `list_live_orders_for_symbol` from `app.services.investment_reports.linked_orders`; `StockDetailOrderLedgerResponse` from `app.schemas.investment_reports`.

## S3 — Extract shared LinkedOrder status maps + row (FE refactor, behaviour-preserving)
**Files:** create `frontend/invest/src/components/orders/LinkedOrderRow.tsx`; modify `InvestmentReportBundleContent.tsx`; test stays green.

- New module exports `LINKED_ORDER_STATUS_LABELS`, `LINKED_ORDER_STATUS_TONES` (moved verbatim from `InvestmentReportBundleContent.tsx:80-107`) and `LinkedOrderRow({ order }: { order: LinkedOrder })` rendering the exact ROB-554 Pill row (status pill, side/symbol, filledQty@avgFillPrice, orderTime, order# slice(0,8), exitReason||thesis).
- `InvestmentReportBundleContent.tsx`: delete local maps + inline row JSX; import `LinkedOrderRow`; render `item.linkedOrders.map(o => <LinkedOrderRow key={`${o.broker??''}:${o.market??''}:${o.ledgerId}`} order={o} />)`.
- Existing `InvestmentReportBundleContent.linkedOrders.test.tsx` must stay green (same rendered text).

## S4 — FE client + "주문 기록" card
**Files:** modify `frontend/invest/src/api/investmentReports.ts` (export `normalizeLinkedOrder`), `frontend/invest/src/api/stockDetail.ts`, `frontend/invest/src/types/stockDetail.ts`, `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx`; test `StockDetailPage.orderLedger.test.tsx`.

- Export `normalizeLinkedOrder` from `api/investmentReports.ts` (currently internal).
- `api/stockDetail.ts`: `fetchStockDetailOrderLedger({market,symbol,days?}): Promise<LinkedOrder[]>` → `getJson<{count:number; items:Record<string,unknown>[]}>(`${path}/order-ledger${suffix}`)` then `.items.map(normalizeLinkedOrder)`.
- `StockDetailPage.tsx`: state `orderLedger: LinkedOrder[] | undefined`; fetch in the `[market,symbol]` effect (crypto sends raw pair — symbol already correct); new full-width `OrderLedgerCard` (title "주문 기록") after the orderbook/orders grid (line 462), rendering `LinkedOrderRow` rows + loading/empty states.
- Test: card renders rows for given orders; empty state; crypto param wiring sends KRW-BTC.

## Verify
- Backend: `uv run pytest tests/test_rob559_symbol_order_history.py -q` + adjacent (linked_orders/investment_reports), `ruff check`, `ruff format --check`, `ty check app/`.
- Frontend: `npm test -- --run` (new + existing linkedOrders test green), `npm run typecheck`.
- ⚠️ Reminder: NO literal "binance" string in `app/**` (ROB-285 guard).

## Self-review
- Coverage: S1 query / S2 endpoint / S3 shared maps / S4 card — all decisions covered. Migration 0.
- Type consistency: `LinkedOrderView` (be) ↔ `LinkedOrder` (fe via normalizeLinkedOrder). `list_live_orders_for_symbol` name consistent S1↔S2. crypto symbol = raw pair end-to-end.
