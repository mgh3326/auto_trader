from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals._news import handle_get_news
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.mcp_server.tooling.market_data_quotes import _get_indicators_impl


class PortfolioPositionDetailNotFoundError(Exception):
    pass


class PortfolioPositionDetailService:
    def __init__(self, *, overview_service, dashboard_service) -> None:
        self.overview_service = overview_service
        self.dashboard_service = dashboard_service

    async def get_page_payload(
        self,
        *,
        user_id: int,
        market_type: str,
        symbol: str,
    ) -> dict[str, Any]:
        base = await self.overview_service.get_position_detail_base(
            user_id=user_id,
            market_type=market_type,
            symbol=symbol,
        )
        if base is None:
            raise PortfolioPositionDetailNotFoundError(symbol)

        journal = await self.dashboard_service.get_latest_journal_snapshot(
            symbol,
            current_price=base.get("current_price"),
        )
        return {
            "summary": {
                "market_type": base["market_type"],
                "symbol": base["symbol"],
                "name": base["name"],
                "current_price": base.get("current_price"),
                "quantity": base["quantity"],
                "avg_price": base["avg_price"],
                "profit_loss": base.get("profit_loss"),
                "profit_rate": base.get("profit_rate"),
                "evaluation": base.get("evaluation"),
                "account_count": len(base.get("components") or []),
                "target_distance_pct": (journal or {}).get("target_distance_pct"),
                "stop_distance_pct": (journal or {}).get("stop_distance_pct"),
            },
            "components": base.get("components") or [],
            "journal": journal,
        }

    async def get_indicators_payload(
        self, *, market_type: str, symbol: str
    ) -> dict[str, Any]:
        return await _get_indicators_impl(
            symbol,
            ["rsi", "stoch_rsi", "macd", "bollinger", "ema", "sma"],
            market=market_type,
        )

    async def get_news_payload(
        self, *, market_type: str, symbol: str
    ) -> dict[str, Any]:
        return await handle_get_news(symbol=symbol, market=market_type, limit=10)

    async def get_opinions_payload(
        self, *, market_type: str, symbol: str
    ) -> dict[str, Any]:
        if str(market_type).lower() == "crypto":
            return {
                "supported": False,
                "message": "애널리스트 의견이 제공되지 않는 시장입니다.",
                "opinions": [],
                "consensus": None,
            }
        payload = await handle_get_investment_opinions(
            symbol=symbol,
            market=market_type,
            limit=10,
        )
        payload["supported"] = True
        return payload
