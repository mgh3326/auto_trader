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
        payload = await _get_indicators_impl(
            symbol,
            ["rsi", "stoch_rsi", "macd", "bollinger", "ema", "sma"],
            market=market_type,
        )
        return {
            "price": payload.get("price"),
            "indicators": payload.get("indicators") or {},
            "summary_cards": self._build_indicator_summary_cards(payload),
        }

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
        consensus = payload.get("consensus") or {}
        return {
            "supported": True,
            "message": payload.get("message"),
            "consensus": consensus.get("consensus"),
            "avg_target_price": consensus.get("avg_target_price"),
            "upside_pct": consensus.get("upside_pct"),
            "buy_count": consensus.get("buy_count"),
            "hold_count": consensus.get("hold_count"),
            "sell_count": consensus.get("sell_count"),
            "opinions": payload.get("opinions") or [],
        }

    def _build_indicator_summary_cards(
        self, payload: dict[str, Any]
    ) -> list[dict[str, str]]:
        indicators = payload.get("indicators") or {}
        price = payload.get("price")
        cards: list[dict[str, str]] = []

        rsi = (indicators.get("rsi") or {}).get("14")
        if isinstance(rsi, (int, float)):
            if rsi < 30:
                tone = "oversold"
                meaning = "과매도"
            elif rsi > 70:
                tone = "overbought"
                meaning = "과매수"
            else:
                tone = "neutral"
                meaning = "중립"
            cards.append(
                {
                    "label": "RSI(14)",
                    "value": f"{rsi:.1f}",
                    "tone": tone,
                    "description": meaning,
                }
            )

        stoch = indicators.get("stoch_rsi") or {}
        k_value = stoch.get("k")
        d_value = stoch.get("d")
        if isinstance(k_value, (int, float)) and isinstance(d_value, (int, float)):
            if k_value < 20 and d_value < 20:
                description = "과매도 구간"
                tone = "oversold"
            elif k_value > 80 and d_value > 80:
                description = "과매수 구간"
                tone = "overbought"
            else:
                description = "중립 구간"
                tone = "neutral"
            cards.append(
                {
                    "label": "Stoch RSI",
                    "value": f"K {k_value:.1f} / D {d_value:.1f}",
                    "tone": tone,
                    "description": description,
                }
            )

        macd = indicators.get("macd") or {}
        macd_value = macd.get("macd")
        signal_value = macd.get("signal")
        histogram = macd.get("histogram")
        if isinstance(macd_value, (int, float)) and isinstance(
            signal_value, (int, float)
        ):
            bullish = macd_value >= signal_value
            cards.append(
                {
                    "label": "MACD",
                    "value": "Bullish" if bullish else "Bearish",
                    "tone": "bullish" if bullish else "bearish",
                    "description": (
                        f"MACD {macd_value:.2f} / Signal {signal_value:.2f}"
                        + (
                            f" / Hist {histogram:.2f}"
                            if isinstance(histogram, (int, float))
                            else ""
                        )
                    ),
                }
            )

        bollinger = indicators.get("bollinger") or {}
        upper = bollinger.get("upper")
        middle = bollinger.get("middle")
        lower = bollinger.get("lower")
        if (
            isinstance(price, (int, float))
            and isinstance(upper, (int, float))
            and isinstance(middle, (int, float))
            and isinstance(lower, (int, float))
        ):
            if abs(price - lower) <= abs(price - upper) and abs(price - lower) <= abs(
                price - middle
            ):
                description = "하단 근처"
                tone = "oversold"
            elif abs(price - upper) < abs(price - middle):
                description = "상단 근처"
                tone = "overbought"
            else:
                description = "중단 근처"
                tone = "neutral"
            cards.append(
                {
                    "label": "Bollinger",
                    "value": description,
                    "tone": tone,
                    "description": f"상단 {upper:.2f} / 중단 {middle:.2f} / 하단 {lower:.2f}",
                }
            )

        ema = indicators.get("ema") or {}
        ema20 = ema.get("20")
        ema60 = ema.get("60")
        ema200 = ema.get("200")
        if isinstance(price, (int, float)) and isinstance(ema20, (int, float)):
            if (
                isinstance(ema60, (int, float))
                and isinstance(ema200, (int, float))
                and price > ema20 > ema60 > ema200
            ):
                tone = "bullish"
                description = "상방 정렬"
            elif (
                isinstance(ema60, (int, float))
                and isinstance(ema200, (int, float))
                and price < ema20 < ema60 < ema200
            ):
                tone = "bearish"
                description = "하방 정렬"
            else:
                tone = "neutral"
                description = "혼조"
            cards.append(
                {
                    "label": "EMA",
                    "value": description,
                    "tone": tone,
                    "description": (
                        f"20 {ema20:.2f}"
                        + (
                            f" / 60 {ema60:.2f}"
                            if isinstance(ema60, (int, float))
                            else ""
                        )
                        + (
                            f" / 200 {ema200:.2f}"
                            if isinstance(ema200, (int, float))
                            else ""
                        )
                    ),
                }
            )

        sma = indicators.get("sma") or {}
        sma20 = sma.get("20")
        sma60 = sma.get("60")
        sma200 = sma.get("200")
        if isinstance(price, (int, float)) and isinstance(sma20, (int, float)):
            if (
                isinstance(sma60, (int, float))
                and isinstance(sma200, (int, float))
                and price > sma20 > sma60 > sma200
            ):
                tone = "bullish"
                description = "상방 정렬"
            elif (
                isinstance(sma60, (int, float))
                and isinstance(sma200, (int, float))
                and price < sma20 < sma60 < sma200
            ):
                tone = "bearish"
                description = "하방 정렬"
            else:
                tone = "neutral"
                description = "혼조"
            cards.append(
                {
                    "label": "SMA",
                    "value": description,
                    "tone": tone,
                    "description": (
                        f"20 {sma20:.2f}"
                        + (
                            f" / 60 {sma60:.2f}"
                            if isinstance(sma60, (int, float))
                            else ""
                        )
                        + (
                            f" / 200 {sma200:.2f}"
                            if isinstance(sma200, (int, float))
                            else ""
                        )
                    ),
                }
            )

        return cards
