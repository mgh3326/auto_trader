# ROB-573 Stock Detail Provider Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `/invest/api/stock-detail/*` detail data by wiring real read-only providers for quote, candles, KR orderbook, account-panel-parity holdings, valuation, and latest analysis.

**Architecture:** Keep `build_stock_detail()` as the view-model orchestrator and add a focused provider adapter module under `app/services/invest_view_model/`. The router passes a request-scoped `StockDetailProviders` instance so the holding provider can reuse the existing `InvestHomeService` account-panel path with `include_paper=False`.

**Tech Stack:** FastAPI, SQLAlchemy async session, Pydantic schemas, existing `market_data.service`, `MarketValuationSnapshotsRepository`, `StockAnalysisService`, React/Vite stock-detail page tests.

---

## Locked Decisions

- Holding scope is account-panel parity: KIS live + Upbit + Toss API + manual holdings.
- Paper/mock/demo sources stay excluded from stock detail by using `include_paper=False`.
- v1 shows merged holding metrics plus source chips and tradeable/reference quantity labels; per-source expanded breakdown is outside this implementation.
- No database migration and no order mutation path.
- Model-lane tag: `keep_on_gpt54`. This is read-only provider wiring, not live order approval, strategy policy, auth, DB migration, or deployment automation.

## File Structure

- Create `app/services/invest_view_model/stock_detail_providers.py`
  - Market-data adapters for quote, candles, and orderbook.
  - DB/read-model adapters for valuation and latest analysis.
  - Account-panel holding provider factory that captures `InvestHomeService`.
- Modify `app/services/invest_view_model/stock_detail_service.py`
  - Import and use real non-holding defaults for quote/orderbook/valuation/latest analysis.
  - Extend `StockDetailProviders` only where needed; keep `_run_optional_block` fail-open behavior.
- Modify `app/services/invest_view_model/stock_detail_candles_service.py`
  - Keep the injectable provider contract and period guardrails.
  - Route-level injection supplies the real provider so unit tests can still pass `_empty_provider`.
- Modify `app/schemas/invest_stock_detail.py`
  - Extend `StockDetailHolding` with `tradeableQuantity`, `sellableQuantity`, `pendingSellQuantity`, and `referenceQuantity`.
- Modify `app/routers/invest_api.py`
  - Inject `InvestHomeService` into `get_stock_detail()`.
  - Pass a request-scoped provider bundle with account-panel holding provider.
  - Pass the real candle provider in `get_stock_detail_candles()`.
- Modify `frontend/invest/src/types/stockDetail.ts`
  - Mirror the new holding quantity fields.
- Modify `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx`
  - Render source chips and tradeable/reference quantity labels in the holding card.
- Test files:
  - Create `tests/test_stock_detail_providers.py`
  - Modify `tests/test_stock_detail_service.py`
  - Modify or add router coverage in `tests/test_invest_api_router_includepaper_sweep.py` or a new focused `tests/test_invest_stock_detail_router.py`
  - Modify `frontend/invest/src/__tests__/StockDetailPage.test.tsx`

---

### Task 1: Backend Provider Adapter Module

**Files:**
- Create: `app/services/invest_view_model/stock_detail_providers.py`
- Test: `tests/test_stock_detail_providers.py`

- [ ] **Step 1: Write failing provider tests**

Add `tests/test_stock_detail_providers.py`:

```python
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.schemas.invest_stock_detail import StockDetailHolding
from app.services.market_data.contracts import Candle, OrderbookLevel, OrderbookSnapshot, Quote


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_candle_provider_maps_period_and_rows(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    calls = []

    async def fake_get_ohlcv(symbol: str, market: str, period: str, count: int):
        calls.append((symbol, market, period, count))
        return [
            Candle(
                symbol=symbol,
                market="equity_kr",
                source="kis",
                period=period,
                timestamp=dt.datetime(2026, 6, 15, tzinfo=dt.UTC),
                open=100,
                high=110,
                low=90,
                close=105,
                volume=1234,
            )
        ]

    monkeypatch.setattr(providers.market_data, "get_ohlcv", fake_get_ohlcv)

    rows = await providers.stock_detail_candle_provider("kr", "000270", "1d")

    assert calls == [("000270", "kr", "day", 200)]
    assert rows == [
        {
            "ts": dt.datetime(2026, 6, 15, tzinfo=dt.UTC),
            "open": 100,
            "high": 110,
            "low": 90,
            "close": 105,
            "volume": 1234,
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_quote_provider_maps_quote(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_quote(symbol: str, market: str):
        return Quote(
            symbol=symbol,
            market="equity_us",
            price=211.34,
            source="yahoo",
            previous_close=209.12,
        )

    monkeypatch.setattr(providers.market_data, "get_quote", fake_get_quote)

    quote = await providers.stock_detail_quote_provider("us", "QQQM", object())

    assert quote is not None
    assert quote.price == pytest.approx(211.34)
    assert quote.previousClose == pytest.approx(209.12)
    assert quote.changeAmount == pytest.approx(2.22)
    assert quote.changeRate == pytest.approx((2.22 / 209.12) * 100)
    assert quote.priceState == "live"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_market_data_orderbook_provider_maps_kr_snapshot(monkeypatch):
    from app.services.invest_view_model import stock_detail_providers as providers

    async def fake_get_orderbook(symbol: str, market: str = "kr", venue: str | None = None):
        return OrderbookSnapshot(
            symbol=symbol,
            instrument_type="equity_kr",
            source="kis",
            asks=[OrderbookLevel(price=71100, quantity=10)],
            bids=[OrderbookLevel(price=71000, quantity=12)],
            total_ask_qty=10,
            total_bid_qty=12,
            bid_ask_ratio=1.2,
        )

    monkeypatch.setattr(providers.market_data, "get_orderbook", fake_get_orderbook)

    orderbook = await providers.stock_detail_orderbook_provider("kr", "005930", object())

    assert orderbook is not None
    assert orderbook.asks[0].price == 71100
    assert orderbook.bids[0].quantity == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holding_provider_uses_account_panel_parity_without_paper():
    from app.services.invest_view_model.stock_detail_providers import (
        make_account_panel_holding_provider,
    )

    class FakeHomeService:
        async def build_account_panel_view(self, *, user_id: int, include_paper: bool = False, paper_sources=None):
            assert user_id == 7
            assert include_paper is False
            assert paper_sources is None
            return SimpleNamespace(
                groupedHoldings=[
                    SimpleNamespace(
                        symbol="000270",
                        market="KR",
                        totalQuantity=3,
                        tradeableQuantity=2,
                        sellableQuantity=1,
                        pendingSellQuantity=1,
                        referenceQuantity=1,
                        averageCost=80000,
                        costBasis=240000,
                        valueNative=255000,
                        valueKrw=255000,
                        pnlKrw=15000,
                        pnlRate=0.0625,
                        includedSources=["kis", "toss_manual"],
                        priceState="live",
                    )
                ]
            )

    provider = make_account_panel_holding_provider(FakeHomeService())

    holding = await provider(7, "kr", "000270", object())

    assert isinstance(holding, StockDetailHolding)
    assert holding.totalQuantity == 3
    assert holding.tradeableQuantity == 2
    assert holding.referenceQuantity == 1
    assert holding.includedSources == ["kis", "toss_manual"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run pytest tests/test_stock_detail_providers.py -q
```

Expected: import failure for `stock_detail_providers` or missing provider functions.

- [ ] **Step 3: Implement provider module**

Create `app/services/invest_view_model/stock_detail_providers.py`:

```python
from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    StockDetailHolding,
    StockDetailLatestAnalysis,
    StockDetailOrderbook,
    StockDetailQuote,
    StockDetailValuation,
)
from app.services.market_data import service as market_data
from app.services.market_valuation_snapshots import MarketValuationSnapshotsRepository
from app.services.stock_info_service import StockAnalysisService


def _period_for_market_data(period: str) -> str:
    normalized = str(period or "1d").strip().lower()
    return {
        "1d": "day",
        "d": "day",
        "day": "day",
        "1w": "week",
        "w": "week",
        "week": "week",
        "1mo": "month",
        "mo": "month",
        "month": "month",
    }.get(normalized, normalized)


def _change_amount(price: float | None, previous_close: float | None) -> float | None:
    if price is None or previous_close is None:
        return None
    return price - previous_close


def _change_rate(price: float | None, previous_close: float | None) -> float | None:
    if price is None or previous_close in (None, 0):
        return None
    return ((price - previous_close) / previous_close) * 100


async def stock_detail_candle_provider(
    market: NewsMarket, symbol: str, period: str
) -> list[dict[str, Any]]:
    rows = await market_data.get_ohlcv(
        symbol=symbol,
        market=market,
        period=_period_for_market_data(period),
        count=200,
    )
    return [
        {
            "ts": row.timestamp,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for row in rows
    ]


async def stock_detail_quote_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailQuote | None:
    _ = db
    quote = await market_data.get_quote(symbol=symbol, market=market)
    amount = _change_amount(quote.price, quote.previous_close)
    return StockDetailQuote(
        price=quote.price,
        previousClose=quote.previous_close,
        changeAmount=amount,
        changeRate=_change_rate(quote.price, quote.previous_close),
        asOf=dt.datetime.now(dt.UTC),
        priceState="live" if quote.price is not None else "missing",
    )


async def stock_detail_orderbook_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailOrderbook | None:
    _ = db
    if market == "us":
        return None
    snapshot = await market_data.get_orderbook(symbol=symbol, market=market)
    if not snapshot.asks and not snapshot.bids:
        return None
    return StockDetailOrderbook(
        asOf=dt.datetime.now(dt.UTC),
        asks=[
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.asks[:10]
        ],
        bids=[
            {"price": level.price, "quantity": level.quantity}
            for level in snapshot.bids[:10]
        ],
    )


async def stock_detail_valuation_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailValuation | None:
    if market == "crypto" or not hasattr(db, "execute"):
        return None
    rows = await MarketValuationSnapshotsRepository(db).latest_for_symbols(
        market=market, symbols={symbol}
    )
    row = rows[0] if rows else None
    if row is None:
        return None
    return StockDetailValuation(
        per=float(row.per) if row.per is not None else None,
        pbr=float(row.pbr) if row.pbr is not None else None,
        roe=float(row.roe) if row.roe is not None else None,
        dividendYield=(
            float(row.dividend_yield) if row.dividend_yield is not None else None
        ),
        high52w=float(row.high_52w) if row.high_52w is not None else None,
        low52w=float(row.low_52w) if row.low_52w is not None else None,
        marketCap=float(row.market_cap) if row.market_cap is not None else None,
        source=row.source,
        asOf=row.computed_at,
        freshness="ok",
    )


def _reasons_top3(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item][:3]
    if isinstance(value, dict):
        for key in ("reasons", "top3", "summary"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [str(item) for item in nested if item][:3]
    return []


async def stock_detail_latest_analysis_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailLatestAnalysis | None:
    _ = market
    if not hasattr(db, "execute"):
        return None
    analysis = await StockAnalysisService(db).get_latest_analysis_by_symbol(symbol)
    if analysis is None:
        return None
    return StockDetailLatestAnalysis(
        id=analysis.id,
        modelName=analysis.model_name,
        decision=analysis.decision,
        confidence=(
            float(analysis.confidence) / 100.0
            if analysis.confidence is not None
            else None
        ),
        appropriateBuyRange=(
            analysis.appropriate_buy_min,
            analysis.appropriate_buy_max,
        ),
        appropriateSellRange=(
            analysis.appropriate_sell_min,
            analysis.appropriate_sell_max,
        ),
        reasonsTop3=_reasons_top3(analysis.reasons),
        createdAt=analysis.created_at,
    )


HoldingProvider = Callable[[int | str, NewsMarket, str, Any], Awaitable[StockDetailHolding | None]]


def make_account_panel_holding_provider(home_service: Any) -> HoldingProvider:
    async def _provider(
        user_id: int | str, market: NewsMarket, symbol: str, db: Any
    ) -> StockDetailHolding | None:
        _ = db
        view = await home_service.build_account_panel_view(
            user_id=int(user_id), include_paper=False, paper_sources=None
        )
        target_market = {"kr": "KR", "us": "US", "crypto": "CRYPTO"}[market]
        for holding in view.groupedHoldings:
            if holding.market == target_market and str(holding.symbol).upper() == symbol.upper():
                return StockDetailHolding(
                    totalQuantity=holding.totalQuantity,
                    tradeableQuantity=holding.tradeableQuantity,
                    sellableQuantity=holding.sellableQuantity,
                    pendingSellQuantity=holding.pendingSellQuantity,
                    referenceQuantity=holding.referenceQuantity,
                    averageCost=holding.averageCost,
                    costBasis=holding.costBasis,
                    valueNative=holding.valueNative,
                    valueKrw=holding.valueKrw,
                    pnlKrw=holding.pnlKrw,
                    pnlRate=holding.pnlRate,
                    includedSources=holding.includedSources,
                    priceState=holding.priceState,
                )
        return None

    return _provider


__all__ = [
    "make_account_panel_holding_provider",
    "stock_detail_candle_provider",
    "stock_detail_latest_analysis_provider",
    "stock_detail_orderbook_provider",
    "stock_detail_quote_provider",
    "stock_detail_valuation_provider",
]
```

- [ ] **Step 4: Run provider tests**

Run:

```bash
uv run pytest tests/test_stock_detail_providers.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/stock_detail_providers.py tests/test_stock_detail_providers.py
git commit -m "test: cover stock detail provider adapters"
```

---

### Task 2: Schema Extension for Holding Provenance

**Files:**
- Modify: `app/schemas/invest_stock_detail.py`
- Modify: `frontend/invest/src/types/stockDetail.ts`
- Test: `tests/test_invest_stock_detail_schemas.py`

- [ ] **Step 1: Write schema test**

Add to `tests/test_invest_stock_detail_schemas.py`:

```python
def test_stock_detail_holding_exposes_tradeable_and_reference_quantities():
    from app.schemas.invest_stock_detail import StockDetailHolding

    holding = StockDetailHolding(
        totalQuantity=5,
        tradeableQuantity=3,
        sellableQuantity=2,
        pendingSellQuantity=1,
        referenceQuantity=2,
        averageCost=100,
        costBasis=500,
        valueNative=550,
        valueKrw=550,
        pnlKrw=50,
        pnlRate=0.1,
        includedSources=["kis", "toss_manual"],
        priceState="live",
    )

    assert holding.tradeableQuantity == 3
    assert holding.referenceQuantity == 2
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
uv run pytest tests/test_invest_stock_detail_schemas.py::test_stock_detail_holding_exposes_tradeable_and_reference_quantities -q
```

Expected: validation failure because the fields are not defined yet.

- [ ] **Step 3: Extend Pydantic schema**

In `app/schemas/invest_stock_detail.py`, update `StockDetailHolding`:

```python
class StockDetailHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalQuantity: float
    tradeableQuantity: float = 0.0
    sellableQuantity: float = 0.0
    pendingSellQuantity: float = 0.0
    referenceQuantity: float = 0.0
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    includedSources: list[AccountSourceLiteral]
    priceState: PriceStateLiteral = "live"
```

- [ ] **Step 4: Extend frontend type**

In `frontend/invest/src/types/stockDetail.ts`, update `StockDetailHolding`:

```ts
export interface StockDetailHolding {
  totalQuantity: number;
  tradeableQuantity: number;
  sellableQuantity: number;
  pendingSellQuantity: number;
  referenceQuantity: number;
  averageCost: number | null;
  costBasis: number | null;
  valueNative: number | null;
  valueKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  includedSources: AccountSource[];
  priceState: PriceState;
}
```

- [ ] **Step 5: Run schema and frontend type checks**

Run:

```bash
uv run pytest tests/test_invest_stock_detail_schemas.py -q
cd frontend/invest && npm run typecheck
```

Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/invest_stock_detail.py frontend/invest/src/types/stockDetail.ts tests/test_invest_stock_detail_schemas.py
git commit -m "feat: expose stock detail holding provenance quantities"
```

---

### Task 3: Wire Providers into Stock Detail Service and Router

**Files:**
- Modify: `app/services/invest_view_model/stock_detail_service.py`
- Modify: `app/routers/invest_api.py`
- Test: `tests/test_stock_detail_service.py`
- Test: `tests/test_invest_stock_detail_router.py`

- [ ] **Step 1: Add service-level regression test for real default providers**

Add to `tests/test_stock_detail_service.py`:

```python
@pytest.mark.asyncio
async def test_default_stock_detail_providers_are_not_noop_for_core_blocks():
    from app.services.invest_view_model.stock_detail_providers import (
        stock_detail_latest_analysis_provider,
        stock_detail_orderbook_provider,
        stock_detail_quote_provider,
        stock_detail_valuation_provider,
    )
    from app.services.invest_view_model.stock_detail_service import (
        DEFAULT_STOCK_DETAIL_PROVIDERS,
    )

    assert DEFAULT_STOCK_DETAIL_PROVIDERS.quote is stock_detail_quote_provider
    assert DEFAULT_STOCK_DETAIL_PROVIDERS.valuation is stock_detail_valuation_provider
    assert (
        DEFAULT_STOCK_DETAIL_PROVIDERS.latest_analysis
        is stock_detail_latest_analysis_provider
    )
    assert DEFAULT_STOCK_DETAIL_PROVIDERS.orderbook is stock_detail_orderbook_provider
```

- [ ] **Step 2: Add router regression test for account-panel holding injection**

Create `tests/test_invest_stock_detail_router.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stock_detail_route_passes_account_panel_holding_provider(monkeypatch):
    from app.routers import invest_api
    from app.schemas.invest_stock_detail import StockDetailResponse
    from app.services.invest_view_model.stock_detail_symbol_resolver import ResolvedSymbol

    async def fake_build_stock_detail(*, user_id, market, symbol, db, providers):
        holding = await providers.holding(user_id, market, symbol, db)
        return StockDetailResponse(
            symbol=symbol,
            market=market,
            displayName="기아",
            exchange="KOSPI",
            instrumentType="equity_kr",
            currency="KRW",
            assetType="equity",
            assetCategory="kr_stock",
            quote=None,
            holding=holding,
            orderbookSupport={"supported": True, "reason": None},
            capabilities={},
            meta={"computedAt": "2026-06-15T00:00:00Z", "warnings": []},
        )

    class FakeHomeService:
        async def build_account_panel_view(self, *, user_id, include_paper=False, paper_sources=None):
            assert include_paper is False
            return SimpleNamespace(
                groupedHoldings=[
                    SimpleNamespace(
                        symbol="000270",
                        market="KR",
                        totalQuantity=4,
                        tradeableQuantity=4,
                        sellableQuantity=4,
                        pendingSellQuantity=0,
                        referenceQuantity=0,
                        averageCost=70000,
                        costBasis=280000,
                        valueNative=300000,
                        valueKrw=300000,
                        pnlKrw=20000,
                        pnlRate=0.0714,
                        includedSources=["kis"],
                        priceState="live",
                    )
                ]
            )

    monkeypatch.setattr(invest_api, "build_stock_detail", fake_build_stock_detail)

    response = await invest_api.get_stock_detail(
        market="kr",
        symbol="000270",
        user=SimpleNamespace(id=7),
        db=SimpleNamespace(),
        service=FakeHomeService(),
    )

    assert response.holding is not None
    assert response.holding.totalQuantity == 4
    assert response.holding.includedSources == ["kis"]
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
uv run pytest tests/test_stock_detail_service.py tests/test_invest_stock_detail_router.py -q
```

Expected: router signature or provider wiring assertions fail.

- [ ] **Step 4: Wire default non-holding providers**

In `app/services/invest_view_model/stock_detail_service.py`, import the new providers:

```python
from app.services.invest_view_model.stock_detail_providers import (
    stock_detail_latest_analysis_provider,
    stock_detail_orderbook_provider,
    stock_detail_quote_provider,
    stock_detail_valuation_provider,
)
```

Update `StockDetailProviders` defaults:

```python
@dataclass(frozen=True, slots=True)
class StockDetailProviders:
    resolver: Resolver = resolve_symbol
    quote: Provider = stock_detail_quote_provider
    screener: Provider = _none_provider
    valuation: Provider = stock_detail_valuation_provider
    holding: Provider = _none_provider
    latest_analysis: Provider = stock_detail_latest_analysis_provider
    orderbook: Provider = stock_detail_orderbook_provider
    fx_rate: Provider = get_usd_krw_quote
    naver_enrichment: Provider = build_naver_stock_detail_poc
    discussion_signal: Provider = build_naver_discussion_signal_poc
    investor_flow: Provider = _default_investor_flow_provider
    recent_trades: Provider = _default_recent_trades_provider
    pending_orders: Provider = _default_pending_orders_provider
```

Keep `holding` out of the default bundle because it must be scoped to the request's `InvestHomeService` dependency.

- [ ] **Step 5: Wire router holding and candle providers**

In `app/routers/invest_api.py`, import:

```python
from app.services.invest_view_model.stock_detail_providers import (
    make_account_panel_holding_provider,
    stock_detail_candle_provider,
)
```

Update `get_stock_detail()` signature and call:

```python
@router.get("/stock-detail/{market}/{symbol}")
async def get_stock_detail(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
) -> StockDetailResponse:
    try:
        return await build_stock_detail(
            user_id=user.id,
            market=market,
            symbol=symbol,
            db=db,
            providers=StockDetailProviders(
                holding=make_account_panel_holding_provider(service)
            ),
        )
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail="symbol_not_found") from exc
```

Update `get_stock_detail_candles()`:

```python
return await build_stock_detail_candles(
    market=market,
    symbol=symbol,
    period=period,
    provider=stock_detail_candle_provider,
)
```

- [ ] **Step 6: Run backend tests**

Run:

```bash
uv run pytest tests/test_stock_detail_service.py tests/test_stock_detail_providers.py tests/test_invest_stock_detail_router.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/invest_view_model/stock_detail_service.py app/routers/invest_api.py tests/test_stock_detail_service.py tests/test_invest_stock_detail_router.py
git commit -m "feat: wire stock detail read providers"
```

---

### Task 4: Frontend Holding Card Provenance

**Files:**
- Modify: `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx`
- Modify: `frontend/invest/src/__tests__/StockDetailPage.test.tsx`

- [ ] **Step 1: Update fixture and failing assertions**

In `frontend/invest/src/__tests__/StockDetailPage.test.tsx`, extend `aboveFold.holding`:

```ts
holding: {
  totalQuantity: 2,
  tradeableQuantity: 1,
  sellableQuantity: 1,
  pendingSellQuantity: 0,
  referenceQuantity: 1,
  averageCost: 200,
  costBasis: 400,
  valueNative: 422.68,
  valueKrw: 575000,
  pnlKrw: 30000,
  pnlRate: 0.055,
  includedSources: ["kis", "toss_manual"],
  priceState: "live",
},
```

Add assertions:

```ts
expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("KIS");
expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("Toss");
expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("매매가능 1주");
expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("참고 1주");
```

- [ ] **Step 2: Run frontend test and verify failure**

Run:

```bash
cd frontend/invest && npm test -- StockDetailPage.test.tsx
```

Expected: missing source and quantity labels.

- [ ] **Step 3: Render source chips and provenance labels**

In `frontend/invest/src/pages/stock-detail/StockDetailPage.tsx`, import source metadata:

```ts
import { accountSourceMeta } from "../../desktop/AccountSourceMeta";
import type { StockDetailHolding } from "../../types/stockDetail";
```

Add helpers near existing format helpers:

```ts
function sourceChips(sources: StockDetailHolding["includedSources"] | undefined) {
  return (sources ?? []).map((source) => {
    const meta = accountSourceMeta(source);
    return (
      <Pill key={source} tone={meta.tone} size="sm">
        {meta.shortLabel}
      </Pill>
    );
  });
}
```

Update the holding card body:

```tsx
{holding ? (
  <div style={{ display: "grid", gap: 12 }}>
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {sourceChips(holding.includedSources)}
    </div>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
      <Metric label="수량" value={fmtQty(holding.totalQuantity)} />
      <Metric label="평단" value={data.currency === "USD" ? `$${holding.averageCost?.toFixed(2) ?? "−"}` : `₩${holding.averageCost?.toLocaleString("ko-KR") ?? "−"}`} />
      <Metric label="평가금액" value={holding.valueKrw == null ? "−" : `₩${Math.round(holding.valueKrw).toLocaleString("ko-KR")}`} />
      <div>
        <div style={{ color: "var(--fg-3)", fontSize: 12 }}>손익</div>
        <PL value={holding.pnlKrw ?? 0} pct={(holding.pnlRate ?? 0) * 100} />
      </div>
    </div>
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", color: "var(--fg-3)", fontSize: 12 }}>
      <span>매매가능 {fmtQty(holding.tradeableQuantity)}</span>
      <span>매도가능 {fmtQty(holding.sellableQuantity)}</span>
      {holding.referenceQuantity > 0 ? <span>참고 {fmtQty(holding.referenceQuantity)}</span> : null}
    </div>
  </div>
) : (
  <p style={{ margin: 0, color: "var(--fg-3)" }}>보유 수량이 없습니다.</p>
)}
```

- [ ] **Step 4: Run frontend tests**

Run:

```bash
cd frontend/invest && npm test -- StockDetailPage.test.tsx
cd frontend/invest && npm run typecheck
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/pages/stock-detail/StockDetailPage.tsx frontend/invest/src/__tests__/StockDetailPage.test.tsx
git commit -m "feat: show stock detail holding provenance"
```

---

### Task 5: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
uv run pytest \
  tests/test_stock_detail_providers.py \
  tests/test_stock_detail_service.py \
  tests/test_invest_stock_detail_router.py \
  tests/test_invest_stock_detail_schemas.py \
  -q
```

Expected: all pass.

- [ ] **Step 2: Run focused frontend tests**

Run:

```bash
cd frontend/invest && npm test -- StockDetailPage.test.tsx
cd frontend/invest && npm run typecheck
```

Expected: all pass.

- [ ] **Step 3: Run broader safety checks**

Run:

```bash
make lint
make test-unit
```

Expected: both pass. If `make test-unit` is too slow in the local environment, run the focused backend tests above plus `uv run pytest tests/test_invest_api_router_safety.py -q` and record that broader unit coverage was deferred.

- [ ] **Step 4: Manual smoke target**

Start the API/frontend through the repo's normal dev path:

```bash
make dev
```

Open:

```text
/invest/stocks/kr/000270
/invest/stocks/kr/011200
```

Expected:

- Header quote is populated when `market_data.get_quote` succeeds.
- Chart shows non-zero candles for KR daily period when KIS returns rows.
- KR orderbook renders bid/ask rows instead of `kr_unavailable`.
- Holding card uses account-panel parity, excludes paper/mock, and shows source chips.
- Valuation and latest analysis render when DB rows exist.
- Provider failures show warnings in `meta.warnings` and do not break the page shell.

---

## Self-Review

- Spec coverage: The plan covers candles, KR orderbook, holding, valuation, latest analysis, and the related equity quote gap found during code inspection.
- Scope control: No migration, no order mutation, no live order approval change, no heavy router logic.
- Placeholder scan: No implementation step relies on unspecified behavior; each changed code path has a concrete test and command.
- Type consistency: Backend `StockDetailHolding` fields match frontend `StockDetailHolding` fields and account-panel `GroupedHolding` names. `pnlRate` remains a ratio from the account-panel read model; the stock-detail UI multiplies by 100 only when rendering the percentage string.
- Residual risk: `StockAnalysisResult.reasons` may contain shapes beyond list/dict. The provider intentionally falls back to an empty `reasonsTop3` list rather than failing the page.
