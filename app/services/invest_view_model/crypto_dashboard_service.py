"""Read-only crypto dashboard view model for ROB-226."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_order import PendingOrder
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.schemas.invest_crypto import (
    CryptoCandidateInsight,
    CryptoCandidateReasonKind,
    CryptoDashboardMeta,
    CryptoDashboardResponse,
    CryptoHoldingSummary,
    CryptoInsightsSummary,
    CryptoMarketCard,
    CryptoPendingOrderItem,
    CryptoPendingOrdersSummary,
    CryptoRiskBadge,
    CryptoRiskLevel,
    CryptoRiskSummary,
    CryptoSourceState,
)
from app.services.invest_view_model.relation_resolver import RelationResolver

TickerProvider = Callable[[list[str]], Awaitable[Any] | Any]
OrderbookSpreadProvider = Callable[[list[str]], Awaitable[Any] | Any]

# Deterministic dashboard heuristics only; these thresholds never trigger orders,
# watch writes, candidate persistence, or provider-side mutations.
HIGH_VOLATILITY_ABS_CHANGE = 0.07
ELEVATED_MOMENTUM_ABS_CHANGE = 0.04
THIN_ORDERBOOK_SPREAD_PCT = 0.5
LOW_LIQUIDITY_TRADE_PRICE_KRW = 500_000_000
CANDIDATE_MAX_ITEMS = 5


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _base_symbol(symbol: str) -> str:
    normalized = str(symbol or "").upper()
    if normalized.startswith("KRW-"):
        return normalized.split("-", 1)[1]
    if normalized.endswith("-KRW"):
        return normalized.rsplit("-", 1)[0]
    return normalized


def _ticker_map(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        market = str(row.get("market") or "").upper()
        if market:
            mapped[market] = row
    return mapped


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pending_symbol_variants(symbols: Sequence[str]) -> set[str]:
    variants: set[str] = set()
    for symbol in symbols:
        upper = str(symbol).upper()
        base = _base_symbol(upper)
        variants.update({upper, base, f"KRW-{base}", f"{base}-KRW"})
    return variants


async def _default_ticker_provider(markets: list[str]):
    from app.services.upbit_public_read_model import get_default_read_model

    read_model = await get_default_read_model()
    return await read_model.get_tickers(markets)


async def _default_orderbook_spread_provider(markets: list[str]):
    from app.services.upbit_public_read_model import get_default_read_model

    read_model = await get_default_read_model()
    return await read_model.get_orderbooks(markets)


def _normalize_ticker_provider_result(
    result: Any,
) -> tuple[dict[str, dict[str, Any]], Any | None]:
    if hasattr(result, "tickers") and hasattr(result, "meta"):
        return dict(result.tickers), result.meta
    return _ticker_map(result or []), None


def _normalize_orderbook_provider_result(
    result: Any,
) -> tuple[dict[str, float | None], Any | None]:
    if hasattr(result, "spreadsPct") and hasattr(result, "meta"):
        return {
            str(k).upper(): v for k, v in dict(result.spreadsPct).items()
        }, result.meta
    return {str(k).upper(): v for k, v in dict(result or {}).items()}, None


def _crypto_source_from_upbit_meta(
    meta: Any, *, fallback_source: str, fallback_label: str, fetched_at: datetime
) -> CryptoSourceState:
    if meta is not None:
        try:
            from app.services.upbit_public_read_model import to_crypto_source_state

            return to_crypto_source_state(meta)
        except Exception:  # noqa: BLE001 - fallback keeps dashboard renderable
            pass
    return CryptoSourceState(
        source=fallback_source,
        state="supported",
        label=fallback_label,
        fetchedAt=fetched_at,
    )


async def _load_active_krw_markets(
    db: AsyncSession, *, limit: int | None = None
) -> list[UpbitSymbolUniverse]:
    stmt = (
        select(UpbitSymbolUniverse)
        .where(
            UpbitSymbolUniverse.quote_currency == "KRW",
            UpbitSymbolUniverse.is_active.is_(True),
        )
        .order_by(UpbitSymbolUniverse.market.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _load_pending_orders(
    db: AsyncSession,
    *,
    user_id: int,
    symbols: Sequence[str],
    limit: int = 20,
) -> list[PendingOrder]:
    variants = _pending_symbol_variants(symbols)
    if not variants:
        return []
    stmt = (
        select(PendingOrder)
        .where(
            PendingOrder.user_id == user_id,
            PendingOrder.market == "crypto",
            PendingOrder.venue == "upbit",
            PendingOrder.symbol.in_(sorted(variants)),
            or_(PendingOrder.status == "open", PendingOrder.status == "partial_fill"),
        )
        .order_by(PendingOrder.ordered_at.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _build_pending_summary(rows: Sequence[PendingOrder]) -> CryptoPendingOrdersSummary:
    items = [
        CryptoPendingOrderItem(
            orderId=row.broker_order_id,
            symbol=str(row.symbol).upper(),
            baseSymbol=_base_symbol(str(row.symbol)),
            side=row.side,
            orderType=row.order_type,
            price=_float_or_none(row.price),
            quantity=float(row.quantity or 0),
            filledQuantity=float(row.filled_quantity or 0),
            status=row.status,
            orderedAt=row.ordered_at,
            updatedAt=row.updated_at,
        )
        for row in rows
    ]
    return CryptoPendingOrdersSummary(
        items=items,
        emptyState=None if items else "no_pending_orders",
    )


def _risk_level(score: int, *, ticker_available: bool) -> CryptoRiskLevel:
    if not ticker_available:
        return "unknown"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _risk_score(
    *,
    change_rate: float | None,
    acc_trade_price_24h: float | None,
    spread: float | None,
    ticker_available: bool,
    has_pending_order: bool,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if not ticker_available:
        score += 40
        reasons.append("시세 데이터 없음")
    if spread is not None and spread > THIN_ORDERBOOK_SPREAD_PCT:
        score += 25
        reasons.append("호가 스프레드 확대")
    if change_rate is not None and abs(change_rate) >= HIGH_VOLATILITY_ABS_CHANGE:
        score += 25
        reasons.append("24시간 변동성 확대")
    if (
        ticker_available
        and acc_trade_price_24h is not None
        and acc_trade_price_24h < LOW_LIQUIDITY_TRADE_PRICE_KRW
    ):
        score += 15
        reasons.append("24시간 거래대금 낮음")
    if has_pending_order:
        score += 10
        reasons.append("미체결 상태 존재")
    return min(score, 100), reasons


def _candidate_score(
    card: CryptoMarketCard, *, has_pending_order: bool
) -> tuple[int, list[CryptoCandidateReasonKind], str]:
    score = 0
    reasons: list[CryptoCandidateReasonKind] = []
    if card.isWatched:
        score += 35
        reasons.append("watched")
    if (
        card.changeRate24h is not None
        and abs(card.changeRate24h) >= ELEVATED_MOMENTUM_ABS_CHANGE
    ):
        score += 25
        reasons.append("momentum")
    if (
        card.accTradePrice24h is not None
        and card.accTradePrice24h >= LOW_LIQUIDITY_TRADE_PRICE_KRW
    ):
        score += 20
        reasons.append("liquidity")
    if (
        card.orderbookSpreadPct is not None
        and card.orderbookSpreadPct <= THIN_ORDERBOOK_SPREAD_PCT
    ):
        score += 10
        reasons.append("spread")
    if card.isHeld:
        reasons.append("held")
    if has_pending_order:
        score -= 30
        reasons.append("pending_order")
    if card.risk and card.risk.level == "high":
        score -= 20
    if card.risk and card.risk.level in {"low", "medium"}:
        reasons.append("data_quality")
    score = max(0, min(score, 100))
    summary_parts: list[str] = []
    if "watched" in reasons:
        summary_parts.append("기존 검토 목록과 일치")
    if "momentum" in reasons:
        summary_parts.append("24시간 변화가 큼")
    if "liquidity" in reasons:
        summary_parts.append("거래대금 양호")
    if "spread" in reasons:
        summary_parts.append("호가 간격 안정")
    if "pending_order" in reasons:
        summary_parts.append("미체결 상태로 감점")
    summary = " · ".join(summary_parts) or "참고용 검토 후보"
    return score, reasons, summary


def _build_candidate_insights(
    cards: Sequence[CryptoMarketCard],
    pending_by_base: set[str],
    *,
    limit: int = CANDIDATE_MAX_ITEMS,
) -> list[CryptoCandidateInsight]:
    ranked: list[
        tuple[int, float, str, CryptoMarketCard, list[CryptoCandidateReasonKind], str]
    ] = []
    for card in cards:
        risk = card.risk
        if risk is None or risk.level == "unknown":
            continue
        if any(badge.kind == "data_unavailable" for badge in card.badges):
            continue
        has_pending_order = card.baseSymbol in pending_by_base
        score, reasons, summary = _candidate_score(
            card, has_pending_order=has_pending_order
        )
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                card.accTradePrice24h or 0,
                card.symbol,
                card,
                reasons,
                summary,
            )
        )
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    candidates: list[CryptoCandidateInsight] = []
    for rank, (score, _trade_amount, _symbol, card, reasons, summary) in enumerate(
        ranked[: max(0, limit)], start=1
    ):
        risk = card.risk
        if risk is None:
            continue
        candidates.append(
            CryptoCandidateInsight(
                symbol=card.symbol,
                baseSymbol=card.baseSymbol,
                displayName=card.displayName,
                rank=rank,
                score=score,
                reasons=reasons,
                summary=summary,
                isHeld=card.isHeld,
                isWatched=card.isWatched,
                hasPendingOrder=card.baseSymbol in pending_by_base,
                riskLevel=risk.level,
            )
        )
    return candidates


async def build_crypto_dashboard(
    *,
    db: AsyncSession,
    user_id: int,
    resolver: RelationResolver | None = None,
    ticker_provider: TickerProvider | None = None,
    orderbook_spread_provider: OrderbookSpreadProvider | None = None,
    limit: int = 20,
    orderbook_limit: int = 5,
) -> CryptoDashboardResponse:
    """Build a read-only crypto dashboard without broker mutations or syncs."""
    now = datetime.now(UTC)
    warnings: list[str] = []
    sources: list[CryptoSourceState] = []
    limit = max(1, min(limit, 50))
    orderbook_limit = max(0, min(orderbook_limit, limit))

    # Load the active KRW universe before ticker ranking. The page is a
    # top-movers/volume view, not an alphabetical slice of active markets.
    universe = await _load_active_krw_markets(db)
    all_markets = [row.market.upper() for row in universe]

    tickers: dict[str, dict[str, Any]] = {}
    if all_markets:
        provider = ticker_provider or _default_ticker_provider
        try:
            result = await _maybe_await(provider(all_markets))
            tickers, ticker_meta = _normalize_ticker_provider_result(result)
            sources.append(
                _crypto_source_from_upbit_meta(
                    ticker_meta,
                    fallback_source="upbit_ticker",
                    fallback_label="Upbit ticker",
                    fetched_at=now,
                )
            )
        except Exception:
            warnings.append("crypto_ticker_unavailable")
            sources.append(
                CryptoSourceState(
                    source="upbit_ticker", state="unavailable", label="Upbit ticker"
                )
            )

    def _market_rank(row: UpbitSymbolUniverse) -> tuple[float, float, str]:
        ticker = tickers.get(row.market.upper(), {})
        change = abs(_float_or_none(ticker.get("signed_change_rate")) or 0)
        trade_amount = _float_or_none(ticker.get("acc_trade_price_24h")) or 0
        return (-change, -trade_amount, row.market.upper())

    ranked_universe = sorted(universe, key=_market_rank)[:limit]
    markets = [row.market.upper() for row in ranked_universe]

    spreads: dict[str, float | None] = {}
    if markets and orderbook_limit > 0:
        spread_provider = (
            orderbook_spread_provider or _default_orderbook_spread_provider
        )
        try:
            raw_result = await _maybe_await(spread_provider(markets[:orderbook_limit]))
            spreads, orderbook_meta = _normalize_orderbook_provider_result(raw_result)
            sources.append(
                _crypto_source_from_upbit_meta(
                    orderbook_meta,
                    fallback_source="upbit_orderbook",
                    fallback_label="Upbit orderbook",
                    fetched_at=now,
                )
            )
        except Exception:
            warnings.append("crypto_orderbook_unavailable")
            sources.append(
                CryptoSourceState(
                    source="upbit_orderbook",
                    state="unavailable",
                    label="Upbit orderbook",
                )
            )

    pending_rows = await _load_pending_orders(db, user_id=user_id, symbols=markets)
    pending_summary = _build_pending_summary(pending_rows)
    sources.append(
        CryptoSourceState(
            source="pending_orders",
            state="supported",
            label="Pending orders read model",
            fetchedAt=now,
        )
    )
    pending_by_base = {
        item.baseSymbol for item in pending_summary.items if item.baseSymbol
    }

    cards: list[CryptoMarketCard] = []
    held_symbols: list[str] = []
    for row in ranked_universe:
        symbol = row.market.upper()
        base = row.base_currency.upper()
        ticker = tickers.get(symbol, {})
        direct_keys = {("crypto", symbol), ("crypto", base), ("crypto", f"{base}-KRW")}
        is_held = bool(
            resolver
            and (
                resolver.is_held("crypto", symbol)
                or resolver.is_held("crypto", base)
                or resolver.is_held("crypto", f"{base}-KRW")
                or bool(direct_keys & resolver.held)
            )
        )
        is_watched = bool(
            resolver
            and (
                resolver.is_watched("crypto", symbol)
                or resolver.is_watched("crypto", base)
                or resolver.is_watched("crypto", f"{base}-KRW")
                or bool(direct_keys & resolver.watch)
            )
        )
        if is_held:
            held_symbols.append(symbol)
        badges: list[CryptoRiskBadge] = []
        has_pending_order = base in pending_by_base
        change_rate = _float_or_none(ticker.get("signed_change_rate"))
        acc_trade_price = _float_or_none(ticker.get("acc_trade_price_24h"))
        ticker_available = symbol in tickers
        if is_held:
            badges.append(CryptoRiskBadge(kind="held", label="보유", severity="info"))
        if has_pending_order:
            badges.append(
                CryptoRiskBadge(
                    kind="pending_order", label="미체결", severity="warning"
                )
            )
        spread = spreads.get(symbol)
        if spread is not None and spread > THIN_ORDERBOOK_SPREAD_PCT:
            badges.append(
                CryptoRiskBadge(
                    kind="thin_orderbook",
                    label="호가 스프레드 주의",
                    severity="warning",
                )
            )
        if change_rate is not None and abs(change_rate) >= HIGH_VOLATILITY_ABS_CHANGE:
            badges.append(
                CryptoRiskBadge(
                    kind="high_volatility", label="변동성 주의", severity="warning"
                )
            )
        if (
            ticker_available
            and acc_trade_price is not None
            and acc_trade_price < LOW_LIQUIDITY_TRADE_PRICE_KRW
        ):
            badges.append(
                CryptoRiskBadge(
                    kind="low_liquidity", label="거래대금 낮음", severity="warning"
                )
            )
        if not ticker_available:
            badges.append(
                CryptoRiskBadge(
                    kind="data_unavailable", label="시세 없음", severity="warning"
                )
            )
        risk_score, risk_reasons = _risk_score(
            change_rate=change_rate,
            acc_trade_price_24h=acc_trade_price,
            spread=spread,
            ticker_available=ticker_available,
            has_pending_order=has_pending_order,
        )
        cards.append(
            CryptoMarketCard(
                symbol=symbol,
                baseSymbol=base,
                displayName=row.korean_name or row.english_name or base,
                priceKrw=_float_or_none(ticker.get("trade_price")),
                changeRate24h=change_rate,
                changeAmount24h=_float_or_none(ticker.get("signed_change_price")),
                accTradePrice24h=acc_trade_price,
                volume24h=_float_or_none(ticker.get("acc_trade_volume_24h")),
                orderbookSpreadPct=spread,
                isHeld=is_held,
                isWatched=is_watched,
                badges=badges,
                risk=CryptoRiskSummary(
                    level=_risk_level(risk_score, ticker_available=ticker_available),
                    score=risk_score,
                    reasons=risk_reasons,
                ),
            )
        )

    candidates = _build_candidate_insights(cards, pending_by_base)
    sources.extend(
        [
            CryptoSourceState(
                source="mcp_risk_reference",
                state="reference_only",
                label="MCP risk reference",
                fetchedAt=now,
            ),
            CryptoSourceState(
                source="mcp_candidate_reference",
                state="reference_only",
                label="MCP candidate reference",
                fetchedAt=now,
            ),
        ]
    )
    candidate_symbols = {candidate.symbol for candidate in candidates}
    for card in cards:
        if card.symbol not in candidate_symbols:
            continue
        if card.isWatched:
            card.badges.append(
                CryptoRiskBadge(
                    kind="candidate_watch", label="관심 후보", severity="info"
                )
            )
        elif (
            card.changeRate24h is not None
            and abs(card.changeRate24h) >= ELEVATED_MOMENTUM_ABS_CHANGE
        ):
            card.badges.append(
                CryptoRiskBadge(
                    kind="momentum_candidate", label="모멘텀 후보", severity="info"
                )
            )

    insights = CryptoInsightsSummary(
        badges=[
            badge
            for card in cards
            for badge in card.badges
            if badge.kind
            in {
                "thin_orderbook",
                "data_unavailable",
                "high_volatility",
                "low_liquidity",
            }
        ][:5],
        notes=[
            "읽기 전용 대시보드입니다. 후보 인사이트는 참고용이며 상태 변경을 실행하지 않습니다."
        ],
        candidates=candidates,
    )

    return CryptoDashboardResponse(
        asOf=now,
        cards=cards,
        holdings=CryptoHoldingSummary(
            heldCount=len(held_symbols), symbols=held_symbols
        ),
        pendingOrders=pending_summary,
        insights=insights,
        meta=CryptoDashboardMeta(warnings=warnings, sources=sources),
    )
