from __future__ import annotations

from typing import Any, Literal

from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.portfolio_cash import get_available_capital_impl
from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl
from app.mcp_server.tooling.user_settings_tools import get_user_setting
from app.services.account_routing import (
    AccountRoutingInput,
    suggest_account_from_snapshot,
)
from app.services.exchange_rate_service import get_usd_krw_rate
from app.core.timezone import now_kst
from app.services.brokers.toss.market_calendar import get_kr_toss_session_from_toss
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.nxt_preflight import evaluate_nxt_preflight


async def _resolve_price(
    symbol: str, market: Literal["kr", "us"], price: float | None
) -> tuple[float, str]:
    if price is not None:
        value = float(price)
        if value <= 0:
            raise ValueError("price must be positive")
        return value, "input"
    if market == "kr":
        quote = await _fetch_quote_equity_kr(symbol)
    else:
        quote = await _fetch_quote_equity_us(symbol)
    resolved = float(quote["price"])
    if resolved <= 0:
        raise ValueError("resolved quote price must be positive")
    return resolved, str(quote.get("source") or "quote")


def _normalize_market(market: str | None, symbol: str) -> Literal["kr", "us"]:
    raw = (market or "").strip().lower()
    if raw in {"kr", "equity_kr"}:
        return "kr"
    if raw in {"us", "equity_us"}:
        return "us"
    if raw:
        raise ValueError("suggest_order_account supports market='kr' or market='us'")
    stripped = symbol.strip()
    return "kr" if stripped.isdigit() and len(stripped) == 6 else "us"


async def suggest_order_account_impl(
    *,
    symbol: str,
    market: str | None = None,
    side: str = "buy",
    quantity: float,
    price: float | None = None,
    usd_krw: float | None = None,
) -> dict[str, Any]:
    if side.lower() != "buy":
        raise ValueError("suggest_order_account supports buy side only")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    normalized_market = _normalize_market(market, symbol)
    resolved_price, price_source = await _resolve_price(
        symbol, normalized_market, price
    )
    resolved_usd_krw = usd_krw
    if normalized_market == "us" and resolved_usd_krw is None:
        resolved_usd_krw = await get_usd_krw_rate()
    account_costs = await get_user_setting("account_costs")
    capital = await get_available_capital_impl(include_manual=False)
    holdings = await _get_holdings_impl(
        market=normalized_market,
        include_current_price=False,
        minimum_value=0,
    )
    result = suggest_account_from_snapshot(
        AccountRoutingInput(
            symbol=symbol,
            market=normalized_market,
            side=side,
            quantity=float(quantity),
            price=resolved_price,
            usd_krw=resolved_usd_krw,
            account_costs=account_costs,
            capital_snapshot=capital,
            holdings_snapshot=holdings,
        )
    )
    result["price_source"] = price_source
    if normalized_market == "kr":
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            result.update(tradability.public_fields())
            session = await get_kr_toss_session_from_toss(now_kst())
            result["nxt_preflight"] = evaluate_nxt_preflight(
                session, tradability
            ).to_dict()
    return result


__all__ = ["suggest_order_account_impl"]
