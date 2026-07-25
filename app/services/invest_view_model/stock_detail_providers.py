from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    StockDetailDecisionHistory,
    StockDetailHolding,
    StockDetailOrderbook,
    StockDetailQuote,
    StockDetailValuation,
)
from app.services.decision_history import build_decision_context


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


def _ratio_to_percent(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) * 100


async def stock_detail_candle_provider(
    market: NewsMarket, symbol: str, period: str
) -> list[dict[str, Any]]:
    from app.services.market_data import service as market_data

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
    from app.services.market_data import service as market_data

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
    from app.services.market_data import service as market_data

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
    from app.services.market_valuation_snapshots import (
        MarketValuationSnapshotsRepository,
    )

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
        dividendYield=_ratio_to_percent(row.dividend_yield),
        high52w=float(row.high_52w) if row.high_52w is not None else None,
        low52w=float(row.low_52w) if row.low_52w is not None else None,
        marketCap=float(row.market_cap) if row.market_cap is not None else None,
        source=row.source,
        asOf=row.computed_at,
        freshness="ok",
    )


def _brier(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "n": raw.get("n", 0),
        "meanBrier": raw.get("mean_brier"),
        "flag": raw.get("flag", "insufficient_sample"),
    }


async def stock_detail_decision_history_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailDecisionHistory | None:
    if not hasattr(db, "execute"):
        return None
    ctx = await build_decision_context(db, symbol, market)
    if ctx is None:
        return None
    return StockDetailDecisionHistory(
        symbol=ctx.get("symbol", symbol),
        market=ctx.get("market", market),
        linkQuality=ctx.get("link_quality", "symbol_window"),
        priorDecisions=[
            {
                "date": d.get("date"),
                "intent": d.get("intent"),
                "side": d.get("side"),
                "decisionBucket": d.get("decision_bucket"),
                "confidence": d.get("confidence"),
                "rationale": d.get("rationale"),
            }
            for d in ctx.get("prior_decisions", [])
        ],
        priorLessons=list(ctx.get("prior_lessons", [])),
        realizedOutcomes=[
            {
                "date": o.get("date"),
                "side": o.get("side"),
                "outcome": o.get("outcome"),
                "triggerType": o.get("trigger_type"),
                "pnlPct": o.get("pnl_pct"),
                "realizedPnl": o.get("realized_pnl"),
            }
            for o in ctx.get("realized_outcomes", [])
        ],
        openClaims=[
            {
                "probability": c.get("probability"),
                "horizon": c.get("horizon"),
                "reviewDate": c.get("review_date"),
                "direction": c.get("direction"),
                "targetPrice": c.get("target_price"),
            }
            for c in ctx.get("open_claims", [])
        ],
        runningBrierSymbol=_brier(ctx.get("running_brier_symbol")),
        runningBrierGlobal=_brier(ctx.get("running_brier_global")),
    )


HoldingProvider = Callable[
    [int | str, NewsMarket, str, Any], Awaitable[StockDetailHolding | None]
]


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
            if (
                holding.market == target_market
                and str(holding.symbol).upper() == symbol.upper()
            ):
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
    "stock_detail_decision_history_provider",
    "stock_detail_orderbook_provider",
    "stock_detail_quote_provider",
    "stock_detail_valuation_provider",
]
