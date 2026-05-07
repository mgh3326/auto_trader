from __future__ import annotations

import asyncio
import re
from typing import Any

from app.mcp_server.tooling.fundamentals._news import handle_get_news
from app.mcp_server.tooling.fundamentals._valuation import (
    handle_get_investment_opinions,
)
from app.mcp_server.tooling.market_data_quotes import _get_indicators_impl
from app.mcp_server.tooling.orders_history import get_order_history_impl
from app.services.portfolio_weights import build_weights as _build_portfolio_weights


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
        overview = await self.overview_service.get_overview(
            user_id=user_id,
            market="ALL",
            skip_missing_prices=False,
        )
        positions = overview.get("positions") or []
        base = next(
            (
                row
                for row in positions
                if row["market_type"] == str(market_type).upper()
                and row["symbol"] == symbol
            ),
            None,
        )
        if base is None:
            raise PortfolioPositionDetailNotFoundError(symbol)

        journal = await self.dashboard_service.get_latest_journal_snapshot(
            symbol,
            current_price=base.get("current_price"),
        )

        weights = _build_portfolio_weights(positions, base)
        indicators = await self._fetch_action_inputs(
            market_type=str(market_type).upper(), symbol=symbol
        )
        action_summary = self._build_action_summary(
            summary=base,
            journal=journal,
            weights=weights,
            indicators=indicators,
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
                "profit_loss_krw": base.get("profit_loss_krw"),
                "profit_rate": base.get("profit_rate"),
                "evaluation": base.get("evaluation"),
                "evaluation_krw": base.get("evaluation_krw"),
                "account_count": len(base.get("components") or []),
                "target_distance_pct": (journal or {}).get("target_distance_pct"),
                "stop_distance_pct": (journal or {}).get("stop_distance_pct"),
            },
            "exchange_rate": overview.get("exchange_rate") or {},
            "components": base.get("components") or [],
            "journal": journal,
            "weights": weights,
            "action_summary": action_summary,
        }

    async def _fetch_action_inputs(
        self,
        *,
        market_type: str,
        symbol: str,
    ) -> dict[str, Any]:
        try:
            payload = await _get_indicators_impl(symbol, ["rsi"], market=market_type)
            rsi = ((payload.get("indicators") or {}).get("rsi") or {}).get("14")
        except Exception:
            rsi = None
        return {"rsi": rsi}

    def _build_action_summary(
        self,
        *,
        summary: dict[str, Any],
        journal: dict[str, Any] | None,
        weights: dict[str, float | None],
        indicators: dict[str, Any],
    ) -> dict[str, Any]:
        portfolio_weight_pct = weights.get("portfolio_weight_pct")
        target_distance_pct = (journal or {}).get("target_distance_pct")
        stop_distance_pct = (journal or {}).get("stop_distance_pct")
        profit_rate = summary.get("profit_rate")
        rsi = indicators.get("rsi")

        status = "관망"
        status_tone = "neutral"
        tags: list[str] = []

        if portfolio_weight_pct is not None and portfolio_weight_pct >= 15:
            status = "비중 과다"
            status_tone = "warning"
            tags.append("비중 과다")
        elif stop_distance_pct is not None and stop_distance_pct >= -5:
            status = "손절 주의"
            status_tone = "danger"
            tags.append("손절 주의")
        elif target_distance_pct is not None and target_distance_pct <= 5:
            status = "목표가 근접"
            status_tone = "success"
            tags.append("목표가 근접")
        elif (
            profit_rate is not None
            and profit_rate < 0
            and rsi is not None
            and rsi <= 30
        ):
            status = "추가매수 검토"
            status_tone = "accent"
            tags.append("추가매수 검토")
        elif journal is None:
            status = "저널 없음"
            status_tone = "neutral"
            tags.append("저널 없음")
        elif self._journal_needs_enrichment(journal):
            status = "저널 보강 필요"
            status_tone = "warning"
            tags.append("저널 보강 필요")

        status_tags = self._build_tags(
            portfolio_weight_pct=portfolio_weight_pct,
            target_distance_pct=target_distance_pct,
            rsi=rsi,
        )
        tags = tags + status_tags

        reason = self._build_reason(
            weights=weights,
            target_distance_pct=target_distance_pct,
            rsi=rsi,
        )

        return {
            "status": status,
            "status_tone": status_tone,
            "tags": tags,
            "reason": reason,
            "short_reason": reason,
        }

    def _journal_needs_enrichment(self, journal: dict[str, Any]) -> bool:
        tracked_fields = ("target_price", "stop_loss", "thesis", "notes")
        if not any(field in journal for field in tracked_fields):
            return False

        target_price = journal.get("target_price")
        stop_loss = journal.get("stop_loss")
        notes = journal.get("notes")
        thesis = journal.get("thesis")
        if target_price is None and stop_loss is None:
            if not (notes or "") and not (thesis or ""):
                return True
        return False

    def _build_tags(
        self,
        portfolio_weight_pct: float | None,
        target_distance_pct: float | None,
        rsi: Any,
    ) -> list[str]:
        tags: list[str] = []

        if portfolio_weight_pct is not None:
            if portfolio_weight_pct >= 10:
                tags.append("비중 큼")
            else:
                tags.append("비중 보통")

        if target_distance_pct is not None and target_distance_pct > 5:
            tags.append("목표가까지 여유")

        if rsi is not None:
            if rsi <= 30:
                tags.append("RSI 과매도")
            elif rsi >= 70:
                tags.append("RSI 과매수")
            else:
                tags.append("RSI 중립")

        return tags[:3]  # NOSONAR python:S6466 — list slicing never raises IndexError

    def _build_reason(
        self,
        weights: dict[str, float | None],
        target_distance_pct: float | None,
        rsi: Any,
    ) -> str | None:
        reason_parts = []
        portfolio_weight_pct = weights.get("portfolio_weight_pct")
        market_weight_pct = weights.get("market_weight_pct")

        if portfolio_weight_pct is not None:
            reason_parts.append(f"전체 비중 {portfolio_weight_pct}%")
        if market_weight_pct is not None:
            reason_parts.append(f"시장 내 {market_weight_pct}%")
        if rsi is not None:
            rsi_val = float(rsi) if isinstance(rsi, (int, float, str)) else rsi
            if isinstance(rsi_val, (int, float)):
                reason_parts.append(f"RSI {rsi_val:.1f}")
            else:
                reason_parts.append(f"RSI {rsi}")

        return " · ".join(reason_parts) if reason_parts else None

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
        payload = await handle_get_news(symbol=symbol, market=market_type, limit=10)
        raw_items = payload.get("news") or []
        normalized_items = [
            self._normalize_news_item(item, symbol=symbol) for item in raw_items
        ]

        deduped: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        for item in normalized_items:
            dedupe_key = self._news_dedupe_key(item)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            deduped.append(item)

        deduped.sort(
            key=lambda item: (
                item.get("_relevance_score", 0),
                item.get("published_at") or "",
            ),
            reverse=True,
        )

        news = []
        for item in deduped[:10]:
            cleaned = dict(item)
            cleaned.pop("_relevance_score", None)
            news.append(cleaned)

        return {
            "count": len(news),
            "news": news,
        }

    async def get_orders_payload(
        self,
        *,
        market_type: str,
        symbol: str,
    ) -> dict[str, Any]:
        filled_result, pending_result = await asyncio.gather(
            get_order_history_impl(
                symbol=symbol,
                market=market_type,
                status="filled",
                days=30,
                limit=10,
            ),
            get_order_history_impl(
                symbol=symbol,
                market=market_type,
                status="pending",
                limit=10,
            ),
            return_exceptions=True,
        )

        errors: list[dict[str, str]] = []
        recent_fills = self._extract_orders_result(
            result=filled_result,
            stage="filled",
            errors=errors,
        )
        pending_orders = self._extract_orders_result(
            result=pending_result,
            stage="pending",
            errors=errors,
        )

        normalized_fills = [
            self._normalize_position_order_item(order) for order in recent_fills
        ]
        normalized_pending = [
            self._normalize_position_order_item(order) for order in pending_orders
        ]
        last_fill = normalized_fills[0] if normalized_fills else None
        last_fill_summary = None
        if normalized_fills:
            count = len(normalized_fills)
            last_side = "매수" if normalized_fills[0]["side"] == "buy" else "매도"
            last_fill_summary = f"최근 체결 {count}건 · 마지막 {last_side}"

        return {
            "summary": {
                "last_fill": last_fill,
                "last_fill_summary": last_fill_summary,
                "pending_count": len(normalized_pending),
                "fill_count": len(normalized_fills),
            },
            "recent_fills": normalized_fills,
            "pending_orders": normalized_pending,
            "errors": errors,
        }

    async def get_opinions_payload(
        self, *, market_type: str, symbol: str
    ) -> dict[str, Any]:
        if str(market_type).lower() == "crypto":
            return {
                "supported": False,
                "message": "애널리스트 의견이 제공되지 않는 시장입니다.",
                "opinions": [],
                "consensus": None,
                "summary_cards": [],
                "distribution": {},
                "top_opinions": [],
                "overflow_count": 0,
            }
        payload = await handle_get_investment_opinions(
            symbol=symbol,
            market=market_type,
            limit=10,
        )
        consensus = payload.get("consensus") or {}
        normalized_opinions = [
            self._normalize_opinion_item(item)
            for item in (payload.get("opinions") or [])
        ]
        top_opinions = normalized_opinions[:5]
        return {
            "supported": True,
            "message": payload.get("message"),
            "consensus": consensus.get("consensus"),
            "avg_target_price": consensus.get("avg_target_price"),
            "upside_pct": consensus.get("upside_pct"),
            "buy_count": consensus.get("buy_count"),
            "hold_count": consensus.get("hold_count"),
            "sell_count": consensus.get("sell_count"),
            "summary_cards": self._build_opinion_summary_cards(consensus),
            "distribution": self._build_opinion_distribution(consensus),
            "top_opinions": top_opinions,
            "overflow_count": max(len(normalized_opinions) - len(top_opinions), 0),
            "opinions": payload.get("opinions") or [],
        }

    def _build_rsi_card(self, rsi_14: Any) -> dict[str, str] | None:
        if not isinstance(rsi_14, (int, float)):
            return None
        if rsi_14 < 30:
            tone, meaning = "oversold", "과매도"
        elif rsi_14 > 70:
            tone, meaning = "overbought", "과매수"
        else:
            tone, meaning = "neutral", "중립"
        return {
            "label": "RSI(14)",
            "value": f"{rsi_14:.1f}",
            "tone": tone,
            "description": meaning,
        }

    def _build_stoch_rsi_card(self, k: Any, d: Any) -> dict[str, str] | None:
        if not isinstance(k, (int, float)) or not isinstance(d, (int, float)):
            return None
        if k < 20 and d < 20:
            description, tone = "과매도 구간", "oversold"
        elif k > 80 and d > 80:
            description, tone = "과매수 구간", "overbought"
        else:
            description, tone = "중립 구간", "neutral"
        return {
            "label": "Stoch RSI",
            "value": f"K {k:.1f} / D {d:.1f}",
            "tone": tone,
            "description": description,
        }

    def _build_macd_card(
        self, macd: Any, signal: Any, histogram: Any
    ) -> dict[str, str] | None:
        if not isinstance(macd, (int, float)) or not isinstance(signal, (int, float)):
            return None
        bullish = macd >= signal
        return {
            "label": "MACD",
            "value": "Bullish" if bullish else "Bearish",
            "tone": "bullish" if bullish else "bearish",
            "description": (
                f"MACD {macd:.2f} / Signal {signal:.2f}"
                + (
                    f" / Hist {histogram:.2f}"
                    if isinstance(histogram, (int, float))
                    else ""
                )
            ),
        }

    def _build_bollinger_card(
        self, price: Any, upper: Any, middle: Any, lower: Any
    ) -> dict[str, str] | None:
        if not all(isinstance(v, (int, float)) for v in (price, upper, middle, lower)):
            return None
        if abs(price - lower) <= abs(price - upper) and abs(price - lower) <= abs(
            price - middle
        ):
            description, tone = "하단 근처", "oversold"
        elif abs(price - upper) < abs(price - middle):
            description, tone = "상단 근처", "overbought"
        else:
            description, tone = "중단 근처", "neutral"
        return {
            "label": "Bollinger",
            "value": description,
            "tone": tone,
            "description": f"상단 {upper:.2f} / 중단 {middle:.2f} / 하단 {lower:.2f}",
        }

    def _build_ema_card(
        self, price: Any, ema20: Any, ema60: Any, ema200: Any
    ) -> dict[str, str] | None:
        if not isinstance(price, (int, float)) or not isinstance(ema20, (int, float)):
            return None
        if (
            isinstance(ema60, (int, float))
            and isinstance(ema200, (int, float))
            and price > ema20 > ema60 > ema200
        ):
            tone, description = "bullish", "상방 정렬"
        elif (
            isinstance(ema60, (int, float))
            and isinstance(ema200, (int, float))
            and price < ema20 < ema60 < ema200
        ):
            tone, description = "bearish", "하방 정렬"
        else:
            tone, description = "neutral", "혼조"
        return {
            "label": "EMA",
            "value": description,
            "tone": tone,
            "description": (
                f"20 {ema20:.2f}"
                + (f" / 60 {ema60:.2f}" if isinstance(ema60, (int, float)) else "")
                + (f" / 200 {ema200:.2f}" if isinstance(ema200, (int, float)) else "")
            ),
        }

    def _build_sma_card(
        self, price: Any, sma20: Any, sma60: Any, sma200: Any
    ) -> dict[str, str] | None:
        if not isinstance(price, (int, float)) or not isinstance(sma20, (int, float)):
            return None
        if (
            isinstance(sma60, (int, float))
            and isinstance(sma200, (int, float))
            and price > sma20 > sma60 > sma200
        ):
            tone, description = "bullish", "상방 정렬"
        elif (
            isinstance(sma60, (int, float))
            and isinstance(sma200, (int, float))
            and price < sma20 < sma60 < sma200
        ):
            tone, description = "bearish", "하방 정렬"
        else:
            tone, description = "neutral", "혼조"
        return {
            "label": "SMA",
            "value": description,
            "tone": tone,
            "description": (
                f"20 {sma20:.2f}"
                + (f" / 60 {sma60:.2f}" if isinstance(sma60, (int, float)) else "")
                + (f" / 200 {sma200:.2f}" if isinstance(sma200, (int, float)) else "")
            ),
        }

    def _build_indicator_summary_cards(
        self, payload: dict[str, Any]
    ) -> list[dict[str, str]]:
        indicators = payload.get("indicators") or {}
        price = payload.get("price")
        cards: list[dict[str, str]] = []

        rsi_card = self._build_rsi_card(
            rsi_14=(indicators.get("rsi") or {}).get("14"),
        )
        if rsi_card:
            cards.append(rsi_card)

        stoch = indicators.get("stoch_rsi") or {}
        stoch_card = self._build_stoch_rsi_card(k=stoch.get("k"), d=stoch.get("d"))
        if stoch_card:
            cards.append(stoch_card)

        macd = indicators.get("macd") or {}
        macd_card = self._build_macd_card(
            macd=macd.get("macd"),
            signal=macd.get("signal"),
            histogram=macd.get("histogram"),
        )
        if macd_card:
            cards.append(macd_card)

        bollinger = indicators.get("bollinger") or {}
        bollinger_card = self._build_bollinger_card(
            price=price,
            upper=bollinger.get("upper"),
            middle=bollinger.get("middle"),
            lower=bollinger.get("lower"),
        )
        if bollinger_card:
            cards.append(bollinger_card)

        ema = indicators.get("ema") or {}
        ema_card = self._build_ema_card(
            price=price,
            ema20=ema.get("20"),
            ema60=ema.get("60"),
            ema200=ema.get("200"),
        )
        if ema_card:
            cards.append(ema_card)

        sma = indicators.get("sma") or {}
        sma_card = self._build_sma_card(
            price=price,
            sma20=sma.get("20"),
            sma60=sma.get("60"),
            sma200=sma.get("200"),
        )
        if sma_card:
            cards.append(sma_card)

        return cards

    def _normalize_news_timestamp(self, item: dict[str, Any]) -> str | None:
        for key in ("published_at", "datetime", "published", "date"):
            value = item.get(key)
            if value:
                return str(value)
        return None

    def _normalize_news_title(self, value: str | None) -> str:
        normalized = (value or "").strip().lower()
        return re.sub(r"\s+", " ", normalized)

    def _news_dedupe_key(self, item: dict[str, Any]) -> tuple[str, ...]:
        normalized_title = self._normalize_news_title(item.get("title"))
        if normalized_title:
            return ("title", normalized_title)

        url = str(item.get("url") or "").strip()
        if url:
            return ("url", url)

        return (
            "fallback",
            str(item.get("source") or ""),
            str(item.get("published_at") or ""),
            str(item.get("title") or ""),
        )

    def _score_news_relevance(self, item: dict[str, Any], symbol: str) -> int:
        haystacks = [
            str(item.get("title") or "").lower(),
            str(item.get("summary") or item.get("description") or "").lower(),
            str(item.get("related") or "").lower(),
        ]
        symbol_lower = str(symbol or "").lower()
        score = 0
        if symbol_lower and any(symbol_lower in text for text in haystacks):
            score += 3
        title = haystacks[0]
        if any(
            keyword in title
            for keyword in ("ai", "strategy", "earnings", "실적", "전략")
        ):
            score += 1
        return score

    def _normalize_news_item(
        self,
        item: dict[str, Any],
        *,
        symbol: str,
    ) -> dict[str, Any]:
        summary = item.get("summary")
        fallback_excerpt = item.get("description") or item.get("headline")
        excerpt = None
        if not summary and fallback_excerpt:
            excerpt = str(fallback_excerpt)[:160]

        relevance_score = self._score_news_relevance(item, symbol)
        if relevance_score >= 3:
            relevance = "high"
        elif relevance_score >= 1:
            relevance = "medium"
        else:
            relevance = "low"

        return {
            "title": str(item.get("title") or ""),
            "source": item.get("source"),
            "published_at": self._normalize_news_timestamp(item),
            "url": item.get("url"),
            "summary": summary,
            "excerpt": excerpt,
            "sentiment": item.get("sentiment"),
            "relevance": relevance,
            "_relevance_score": relevance_score,
        }

    def _build_opinion_summary_cards(
        self,
        consensus: dict[str, Any],
    ) -> list[dict[str, str]]:
        cards = [
            {
                "label": "Consensus",
                "value": str(consensus.get("consensus") or "-"),
                "tone": "positive"
                if consensus.get("consensus") == "Buy"
                else "neutral",
            },
            {
                "label": "Avg Target",
                "value": str(consensus.get("avg_target_price") or "-"),
                "tone": "neutral",
            },
            {
                "label": "Upside/Downside",
                "value": self._format_target_gap(consensus.get("upside_pct")),
                "tone": "positive"
                if (consensus.get("upside_pct") or 0) > 0
                else "neutral",
            },
            {
                "label": "Buy / Hold / Sell",
                "value": (
                    f"{consensus.get('buy_count') or 0} / "
                    f"{consensus.get('hold_count') or 0} / "
                    f"{consensus.get('sell_count') or 0}"
                ),
                "tone": "neutral",
            },
        ]
        return cards

    def _format_target_gap(self, upside_pct: float | None) -> str:
        if upside_pct is None:
            return "-"
        return f"{upside_pct:+.1f}%"

    def _build_opinion_distribution(
        self,
        consensus: dict[str, Any],
    ) -> dict[str, int]:
        return {
            "buy": int(consensus.get("buy_count") or 0),
            "hold": int(consensus.get("hold_count") or 0),
            "sell": int(consensus.get("sell_count") or 0),
        }

    def _normalize_opinion_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "firm": item.get("firm") or item.get("source"),
            "rating": item.get("rating") or item.get("action"),
            "target_price": item.get("target_price"),
            "date": item.get("date"),
        }

    def _extract_orders_result(
        self,
        *,
        result: Any,
        stage: str,
        errors: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        if isinstance(result, Exception):
            errors.append({"stage": stage, "error": str(result)})
            return []

        payload = result or {}
        result_errors = payload.get("errors") or []
        for item in result_errors:
            errors.append(
                {
                    "stage": stage,
                    "error": str(item.get("error") or item),
                }
            )
        return payload.get("orders") or []

    def _pick_order_price(self, order: dict[str, Any]) -> float | None:
        for key in ("filled_price", "filled_avg_price", "ordered_price", "price"):
            value = order.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def _pick_order_quantity(self, order: dict[str, Any]) -> float | None:
        for key in ("filled_qty", "executed_qty", "ordered_qty"):
            value = order.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def _build_order_amount(
        self,
        price: float | None,
        quantity: float | None,
    ) -> float | None:
        if price is None or quantity is None:
            return None
        return price * quantity

    def _normalize_position_order_item(
        self,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        price = self._pick_order_price(order)
        quantity = self._pick_order_quantity(order)
        filled_at = order.get("filled_at")
        ordered_at = filled_at or order.get("ordered_at") or order.get("created_at")
        remaining_quantity = order.get("remaining_qty")
        normalized_remaining = (
            float(remaining_quantity)
            if isinstance(remaining_quantity, (int, float))
            else None
        )
        status = str(order.get("status") or "")
        side = str(order.get("side") or "")

        status_label = status
        status_tone = "neutral"
        if status == "filled":
            status_label = "체결"
            status_tone = "filled"
        elif status == "pending":
            status_label = "대기"
            status_tone = "pending"
        elif status == "partially_filled":
            status_label = "부분체결"
            status_tone = "partial"
        elif status == "cancelled":
            status_label = "취소"

        side_label = "매수" if side == "buy" else "매도"

        return {
            "order_id": str(order.get("order_id") or ""),
            "side": side,
            "side_label": side_label,
            "status": status,
            "status_label": status_label,
            "status_tone": status_tone,
            "ordered_at": ordered_at,
            "filled_at": filled_at,
            "price": price,
            "quantity": quantity,
            "remaining_quantity": normalized_remaining,
            "amount": self._build_order_amount(price, quantity),
            "currency": order.get("currency"),
        }
