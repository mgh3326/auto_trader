from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_order import PendingOrder
from app.schemas.invest_crypto import (
    CryptoPendingOrderItem,
    CryptoPendingOrdersSummary,
    CryptoSourceState,
)
from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    CryptoDetail,
    CryptoDetailProfile,
    CryptoRecentTradeItem,
    CryptoRecentTrades,
    StockDetailDiscussionSignal,
    StockDetailFxScenario,
    StockDetailFxSensitivity,
    StockDetailHolding,
    StockDetailInvestorFlow,
    StockDetailInvestorFlowBuyerDecomposition,
    StockDetailInvestorFlowDailyRow,
    StockDetailInvestorFlowPeriodSummary,
    StockDetailLatestAnalysis,
    StockDetailMeta,
    StockDetailNaverEnrichment,
    StockDetailOrderbook,
    StockDetailQuote,
    StockDetailResponse,
    default_capabilities_for_market,
    orderbook_support_for_market,
)
from app.services.exchange_rate_service import get_usd_krw_quote
from app.services.invest_view_model.crypto_preorder_check import (
    build_crypto_preorder_checklist,
)
from app.services.invest_view_model.investor_flow_service import (
    latest_items_for_symbols as _latest_investor_flow_items,
)
from app.services.invest_view_model.naver_discussion_signal_poc import (
    build_naver_discussion_signal_poc,
)
from app.services.invest_view_model.naver_stock_detail_poc import (
    build_naver_stock_detail_poc,
)
from app.services.invest_view_model.stock_detail_providers import (
    stock_detail_latest_analysis_provider,
    stock_detail_orderbook_provider,
    stock_detail_quote_provider,
    stock_detail_valuation_provider,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    ResolvedSymbol,
    resolve_symbol,
)
from app.services.investor_flow_snapshots.repository import (
    InvestorFlowSnapshotsRepository,
)

logger = logging.getLogger(__name__)

_DEFAULT_OPTIONAL_BLOCK_TIMEOUT_SECONDS = 3.0
_HOLDING_PROVIDER_TIMEOUT_SECONDS = 8.0

Resolver = Callable[[NewsMarket, str, AsyncSession], Awaitable[ResolvedSymbol]]
Provider = Callable[..., Awaitable[Any]]


async def _none_provider(*args: Any, **kwargs: Any) -> None:
    return None


def _base_crypto_symbol(symbol: str) -> str:
    normalized = str(symbol or "").upper()
    if normalized.startswith("KRW-"):
        return normalized.split("-", 1)[1]
    if normalized.endswith("-KRW"):
        return normalized.rsplit("-", 1)[0]
    return normalized


def _pending_symbol_variants(symbol: str) -> set[str]:
    upper = str(symbol or "").upper()
    base = _base_crypto_symbol(upper)
    return {upper, base, f"KRW-{base}", f"{base}-KRW"}


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_upbit_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


async def _default_quote_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailQuote | None:
    _ = db
    if market != "crypto":
        return None
    from app.services.brokers.upbit.client import fetch_multiple_tickers

    rows = await fetch_multiple_tickers([symbol])
    if not rows:
        return None
    row = rows[0]
    price = _float_or_none(row.get("trade_price"))
    change_amount = _float_or_none(row.get("signed_change_price"))
    change_rate = _float_or_none(row.get("signed_change_rate"))
    if change_rate is not None:
        change_rate *= 100
    previous_close = (
        price - change_amount
        if price is not None and change_amount is not None
        else None
    )
    return StockDetailQuote(
        price=price,
        previousClose=previous_close,
        changeAmount=change_amount,
        changeRate=change_rate,
        asOf=datetime.now(UTC),
        priceState="live" if price is not None else "missing",
    )


async def _default_orderbook_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailOrderbook | None:
    _ = db
    if market != "crypto":
        return None
    from app.services.upbit_orderbook import fetch_orderbook

    book = await fetch_orderbook(symbol)
    units = list(book.get("orderbook_units") or [])
    if not units:
        return None
    asks = []
    bids = []
    for unit in units[:10]:
        ask_price = _float_or_none(unit.get("ask_price"))
        ask_size = _float_or_none(unit.get("ask_size"))
        bid_price = _float_or_none(unit.get("bid_price"))
        bid_size = _float_or_none(unit.get("bid_size"))
        if ask_price is not None and ask_size is not None:
            asks.append({"price": ask_price, "quantity": ask_size})
        if bid_price is not None and bid_size is not None:
            bids.append({"price": bid_price, "quantity": bid_size})
    if not asks and not bids:
        return None
    return StockDetailOrderbook(asOf=datetime.now(UTC), asks=asks, bids=bids)


async def _default_recent_trades_provider(
    market: NewsMarket, symbol: str, db: Any
) -> CryptoRecentTrades | None:
    _ = db
    if market != "crypto":
        return None
    import httpx

    url = "https://api.upbit.com/v1/trades/ticks"
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(url, params={"market": symbol, "count": 15})
        response.raise_for_status()
        rows = response.json()
    items = []
    for row in rows or []:
        price = _float_or_none(row.get("trade_price"))
        volume = _float_or_none(row.get("trade_volume"))
        if price is None or volume is None:
            continue
        items.append(
            CryptoRecentTradeItem(
                tradeTime=_parse_upbit_datetime(
                    row.get("trade_timestamp") or row.get("trade_date_utc")
                ),
                priceKrw=price,
                volume=volume,
                side=row.get("ask_bid"),
                sequentialId=row.get("sequential_id"),
            )
        )
    return CryptoRecentTrades(
        items=items,
        emptyState=None if items else "no_recent_trades",
        state="supported" if items else "empty",
        asOf=datetime.now(UTC),
    )


async def _default_pending_orders_provider(
    user_id: int | str, market: NewsMarket, symbol: str, db: Any
) -> CryptoPendingOrdersSummary | None:
    if market != "crypto":
        return None
    if not hasattr(db, "execute"):
        return CryptoPendingOrdersSummary(items=[], emptyState="no_pending_orders")
    variants = _pending_symbol_variants(symbol)
    stmt = (
        select(PendingOrder)
        .where(
            PendingOrder.user_id == int(user_id),
            PendingOrder.market == "crypto",
            PendingOrder.venue == "upbit",
            PendingOrder.symbol.in_(sorted(variants)),
            or_(PendingOrder.status == "open", PendingOrder.status == "partial_fill"),
        )
        .order_by(PendingOrder.ordered_at.desc().nullslast())
        .limit(20)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    items = [
        CryptoPendingOrderItem(
            orderId=row.broker_order_id,
            symbol=str(row.symbol).upper(),
            baseSymbol=_base_crypto_symbol(str(row.symbol)),
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
        items=items, emptyState=None if items else "no_pending_orders"
    )


def _daily_row_from_snapshot(row: Any) -> StockDetailInvestorFlowDailyRow:
    return StockDetailInvestorFlowDailyRow(
        snapshotDate=row.snapshot_date.isoformat(),
        collectedAt=row.collected_at,
        source=row.source,
        foreignNet=row.foreign_net,
        institutionNet=row.institution_net,
        individualNet=row.individual_net,
        doubleBuy=row.double_buy,
        doubleSell=row.double_sell,
    )


async def _recent_investor_flow_rows(
    *, db: Any, symbol: str, limit: int = 20
) -> list[StockDetailInvestorFlowDailyRow]:
    repo = InvestorFlowSnapshotsRepository(db)
    rows = await repo.recent_by_symbol(market="kr", symbol=symbol, limit=limit)
    return [_daily_row_from_snapshot(row) for row in rows]


def _sum_known(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known)


def _build_period_summary(
    daily_rows: list[StockDetailInvestorFlowDailyRow],
) -> StockDetailInvestorFlowPeriodSummary | None:
    if not daily_rows:
        return None
    foreign_values = [row.foreignNet for row in daily_rows]
    volume_values = [row.volume for row in daily_rows if row.volume is not None]
    foreign_total = _sum_known(foreign_values)
    volume_total = sum(volume_values) if volume_values else None
    unavailable = [
        "종가/등락률/거래량은 investor_flow_snapshots 저장소에 아직 없어 일별 표에서 준비중으로 표시됩니다.",
        "외국인 보유주수/보유율은 investor_flow_snapshots 저장소에 아직 없어 변화율을 계산하지 않습니다.",
    ]
    return StockDetailInvestorFlowPeriodSummary(
        windowDays=len(daily_rows),
        rowCount=len(daily_rows),
        foreignNetTotal=foreign_total,
        institutionNetTotal=_sum_known([row.institutionNet for row in daily_rows]),
        individualNetTotal=_sum_known([row.individualNet for row in daily_rows]),
        foreignBuyDays=sum(
            1 for value in foreign_values if value is not None and value > 0
        ),
        foreignSellDays=sum(
            1 for value in foreign_values if value is not None and value < 0
        ),
        foreignFlatDays=sum(1 for value in foreign_values if value == 0),
        foreignNetToVolumeRatio=(foreign_total / volume_total)
        if foreign_total is not None and volume_total
        else None,
        foreignHoldingSharesChange=None,
        foreignHoldingRateChange=None,
        unavailableLabels=unavailable,
    )


def _build_buyer_decomposition(
    daily_rows: list[StockDetailInvestorFlowDailyRow],
) -> StockDetailInvestorFlowBuyerDecomposition | None:
    if not daily_rows:
        return None
    # Without price/change-rate storage, use the latest available row as the
    # decomposition proxy and label the price leg explicitly unavailable.
    row = daily_rows[0]
    buyers = {
        "foreign": row.foreignNet,
        "institution": row.institutionNet,
        "individual": row.individualNet,
    }
    positive = {k: v for k, v in buyers.items() if v is not None and v > 0}
    if not positive:
        leading = "unknown"
    else:
        max_value = max(positive.values())
        leaders = [k for k, v in positive.items() if v == max_value]
        leading = leaders[0] if len(leaders) == 1 else "mixed"
    label_by_leader = {
        "foreign": "외국인 주도",
        "institution": "기관 주도",
        "individual": "개인 주도",
        "mixed": "복합 주도",
        "unknown": "주도 매수자 불명",
    }
    return StockDetailInvestorFlowBuyerDecomposition(
        snapshotDate=row.snapshotDate,
        label=label_by_leader[leading],
        leadingBuyer=leading,
        foreignNet=row.foreignNet,
        institutionNet=row.institutionNet,
        individualNet=row.individualNet,
        note="급등일 여부는 종가/등락률 저장 전까지 판별하지 않고, 최신 수급 행의 매수 주체 분해만 표시합니다.",
    )


_INVESTOR_FLOW_UNAVAILABLE_LABELS = [
    "일별 종가/등락률/거래량: 저장소 미적재",
    "외국인 보유주수/보유율: 저장소 미적재",
    "외국인 순매수/거래량 강도: 거래량 저장 전까지 계산 불가",
]


async def _default_investor_flow_provider(
    market: NewsMarket, symbol: str, db: Any
) -> StockDetailInvestorFlow | None:
    if market != "kr":
        return None
    items = await _latest_investor_flow_items(db=db, symbols=[symbol], market="kr")
    item = items.get(symbol)
    daily_rows = await _recent_investor_flow_rows(db=db, symbol=symbol)
    period_summary = _build_period_summary(daily_rows)
    buyer_decomposition = _build_buyer_decomposition(daily_rows)
    if item is None:
        return StockDetailInvestorFlow(
            symbol=symbol,
            dataState="missing",
            dailyRows=daily_rows,
            periodSummary=period_summary,
            buyerDecomposition=buyer_decomposition,
            unavailableLabels=_INVESTOR_FLOW_UNAVAILABLE_LABELS,
        )
    return StockDetailInvestorFlow(
        symbol=item.symbol,
        dataState=item.dataState,
        snapshotDate=item.snapshotDate.isoformat() if item.snapshotDate else None,
        collectedAt=item.collectedAt,
        snapshotSource=item.source,
        foreignNet=item.foreignNet,
        institutionNet=item.institutionNet,
        individualNet=item.individualNet,
        foreignNetBuyRank=item.foreignNetBuyRank,
        foreignNetSellRank=item.foreignNetSellRank,
        institutionNetBuyRank=item.institutionNetBuyRank,
        institutionNetSellRank=item.institutionNetSellRank,
        doubleBuy=item.doubleBuy,
        doubleSell=item.doubleSell,
        foreignConsecutiveBuyDays=item.foreignConsecutiveBuyDays,
        foreignConsecutiveSellDays=item.foreignConsecutiveSellDays,
        institutionConsecutiveBuyDays=item.institutionConsecutiveBuyDays,
        institutionConsecutiveSellDays=item.institutionConsecutiveSellDays,
        individualConsecutiveBuyDays=item.individualConsecutiveBuyDays,
        individualConsecutiveSellDays=item.individualConsecutiveSellDays,
        dailyRows=daily_rows,
        periodSummary=period_summary,
        buyerDecomposition=buyer_decomposition,
        unavailableLabels=_INVESTOR_FLOW_UNAVAILABLE_LABELS,
    )


async def _run_optional_block(
    name: str,
    coro: Awaitable[Any],
    warnings: list[str],
    *,
    timeout: float = _DEFAULT_OPTIONAL_BLOCK_TIMEOUT_SECONDS,
) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        warnings.append(f"{name}_timeout")
    except Exception as exc:  # pragma: no cover - exercised by callers with stubs
        logger.warning("stock-detail %s block unavailable: %s", name, exc)
        warnings.append(f"{name}_unavailable")
    return None


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


DEFAULT_STOCK_DETAIL_PROVIDERS = StockDetailProviders()


async def build_stock_detail(
    *,
    user_id: int | str,
    market: NewsMarket,
    symbol: str,
    db: Any,
    providers: StockDetailProviders = DEFAULT_STOCK_DETAIL_PROVIDERS,
) -> StockDetailResponse:
    """Build the read-only above-the-fold stock-detail view-model.

    Optional provider failures are isolated into response metadata warnings so
    the shell can still render quote/profile/guardrail data. The default Naver
    enrichment provider is a deterministic, fixture-backed PoC map only; it does
    not perform request-time external fetches or writes.
    """

    resolved = await providers.resolver(market, symbol, db)
    warnings: list[str] = []

    quote_task = _run_optional_block(
        "quote", providers.quote(market, resolved.symbol_db, db), warnings
    )
    screener_task = _run_optional_block(
        "screener_snapshot",
        providers.screener(market, resolved.symbol_db, db),
        warnings,
    )
    valuation_task = _run_optional_block(
        "valuation", providers.valuation(market, resolved.symbol_db, db), warnings
    )
    holding_task = _run_optional_block(
        "holding",
        providers.holding(user_id, market, resolved.symbol_db, db),
        warnings,
        timeout=_HOLDING_PROVIDER_TIMEOUT_SECONDS,
    )
    latest_analysis_task = _run_optional_block(
        "latest_analysis",
        providers.latest_analysis(market, resolved.symbol_db, db),
        warnings,
    )
    naver_enrichment_task = _run_optional_block(
        "naver_enrichment",
        providers.naver_enrichment(market, resolved.symbol_db, db),
        warnings,
    )
    discussion_signal_task = _run_optional_block(
        "discussion_signal",
        providers.discussion_signal(market, resolved.symbol_db, db),
        warnings,
    )
    if market in {"kr", "crypto"}:
        orderbook_task = _run_optional_block(
            "orderbook", providers.orderbook(market, resolved.symbol_db, db), warnings
        )
    else:
        orderbook_task = _none_provider()
    if market == "kr":
        investor_flow_task = _run_optional_block(
            "investor_flow",
            providers.investor_flow(market, resolved.symbol_db, db),
            warnings,
        )
    else:
        investor_flow_task = _none_provider()
    if market == "crypto":
        recent_trades_task = _run_optional_block(
            "crypto_recent_trades",
            providers.recent_trades(market, resolved.symbol_db, db),
            warnings,
        )
        pending_orders_task = _run_optional_block(
            "crypto_pending_orders",
            providers.pending_orders(user_id, market, resolved.symbol_db, db),
            warnings,
        )
    else:
        recent_trades_task = _none_provider()
        pending_orders_task = _none_provider()

    (
        quote,
        screener_snapshot,
        valuation,
        holding,
        latest_analysis,
        naver_enrichment,
        discussion_signal,
        orderbook,
        investor_flow,
        recent_trades,
        pending_orders,
    ) = await asyncio.gather(
        quote_task,
        screener_task,
        valuation_task,
        holding_task,
        latest_analysis_task,
        naver_enrichment_task,
        discussion_signal_task,
        orderbook_task,
        investor_flow_task,
        recent_trades_task,
        pending_orders_task,
    )

    capabilities = default_capabilities_for_market(market)
    orderbook_support = orderbook_support_for_market(market)
    if market == "kr" and orderbook is None:
        orderbook_support = orderbook_support.model_copy(
            update={"supported": False, "reason": "kr_unavailable"}
        )
        capabilities = capabilities.model_copy(
            update={
                "orderbook": capabilities.orderbook.model_copy(
                    update={"supported": False, "reason": "kr_unavailable"}
                )
            }
        )

    if quote is not None and not isinstance(quote, StockDetailQuote):
        quote = StockDetailQuote.model_validate(quote)
    if holding is not None and not isinstance(holding, StockDetailHolding):
        holding = StockDetailHolding.model_validate(holding)
    if latest_analysis is not None and not isinstance(
        latest_analysis, StockDetailLatestAnalysis
    ):
        latest_analysis = StockDetailLatestAnalysis.model_validate(latest_analysis)
    if naver_enrichment is not None and not isinstance(
        naver_enrichment, StockDetailNaverEnrichment
    ):
        naver_enrichment = StockDetailNaverEnrichment.model_validate(naver_enrichment)
    if discussion_signal is not None and not isinstance(
        discussion_signal, StockDetailDiscussionSignal
    ):
        discussion_signal = StockDetailDiscussionSignal.model_validate(
            discussion_signal
        )
    if orderbook is not None and not isinstance(orderbook, StockDetailOrderbook):
        orderbook = StockDetailOrderbook.model_validate(orderbook)
    if investor_flow is not None and not isinstance(
        investor_flow, StockDetailInvestorFlow
    ):
        investor_flow = StockDetailInvestorFlow.model_validate(investor_flow)
    if recent_trades is not None and not isinstance(recent_trades, CryptoRecentTrades):
        recent_trades = CryptoRecentTrades.model_validate(recent_trades)
    if pending_orders is not None and not isinstance(
        pending_orders, CryptoPendingOrdersSummary
    ):
        pending_orders = CryptoPendingOrdersSummary.model_validate(pending_orders)

    if market == "crypto" and orderbook is None:
        orderbook_support = orderbook_support.model_copy(
            update={"supported": False, "reason": "provider_unavailable"}
        )
        capabilities = capabilities.model_copy(
            update={
                "orderbook": capabilities.orderbook.model_copy(
                    update={"supported": False, "reason": "provider_unavailable"}
                )
            }
        )

    fx_rate = None
    if _should_fetch_fx_rate(
        market=market, currency=resolved.currency, holding=holding
    ):
        fx_rate = await _run_optional_block(
            "fx_sensitivity", providers.fx_rate(), warnings
        )
    fx_sensitivity = _build_fx_sensitivity(
        market=market,
        currency=resolved.currency,
        holding=holding,
        fx_rate=fx_rate,
    )

    crypto_detail = None
    if market == "crypto":
        if recent_trades is None:
            recent_trades = CryptoRecentTrades(
                items=[],
                emptyState="no_recent_trades",
                state="unavailable",
                warnings=["crypto_recent_trades_unavailable"],
            )
        if pending_orders is None:
            pending_orders = CryptoPendingOrdersSummary(
                items=[], emptyState="no_pending_orders"
            )
        base_symbol = _base_crypto_symbol(resolved.symbol_db)
        crypto_sources = [
            CryptoSourceState(
                source="upbit_ticker",
                state="supported" if quote else "unavailable",
                label="Upbit ticker" if quote else "Upbit ticker unavailable",
                fetchedAt=datetime.now(UTC),
            ),
            CryptoSourceState(
                source="upbit_orderbook",
                state="supported" if orderbook else "unavailable",
                label="Upbit orderbook" if orderbook else "Upbit orderbook unavailable",
                fetchedAt=datetime.now(UTC),
            ),
            CryptoSourceState(
                source="upbit_recent_trades",
                state="supported"
                if recent_trades.state != "unavailable"
                else "unavailable",
                label="Upbit recent trades",
                fetchedAt=recent_trades.asOf,
            ),
            CryptoSourceState(
                source="pending_orders",
                state="supported",
                label="Read-only pending orders",
                fetchedAt=datetime.now(UTC),
            ),
        ]
        crypto_detail = CryptoDetail(
            profile=CryptoDetailProfile(
                symbol=resolved.symbol_db,
                baseSymbol=base_symbol,
                displayNameKo=resolved.display_name,
                displayNameEn=base_symbol,
                asOf=datetime.now(UTC),
            ),
            recentTrades=recent_trades,
            pendingOrders=pending_orders,
            preOrderChecklist=build_crypto_preorder_checklist(
                quote=quote,
                orderbook=orderbook,
                recent_trades=recent_trades,
                holding=holding,
                pending_orders=pending_orders,
                warnings=warnings,
            ),
            sources=crypto_sources,
        )

    return StockDetailResponse(
        symbol=resolved.symbol_db,
        market=market,
        displayName=resolved.display_name,
        exchange=resolved.exchange,
        instrumentType=resolved.instrument_type,
        currency=resolved.currency,
        assetType=resolved.asset_type,
        assetCategory=resolved.asset_category,
        quote=quote,
        screenerSnapshot=screener_snapshot,
        valuation=valuation,
        naverEnrichment=naver_enrichment,
        discussionSignal=discussion_signal,
        investorFlow=investor_flow,
        holding=holding,
        fxSensitivity=fx_sensitivity,
        latestAnalysis=latest_analysis,
        orderbookSupport=orderbook_support,
        orderbook=orderbook,
        capabilities=capabilities,
        cryptoDetail=crypto_detail,
        meta=StockDetailMeta(computedAt=datetime.now(UTC), warnings=warnings),
    )


def _should_fetch_fx_rate(
    *, market: NewsMarket, currency: str, holding: StockDetailHolding | None
) -> bool:
    if market != "us" and currency != "USD":
        return False
    return holding is not None and (holding.valueNative or 0) > 0


def _build_fx_sensitivity(
    *,
    market: NewsMarket,
    currency: str,
    holding: StockDetailHolding | None,
    fx_rate: float | None,
) -> StockDetailFxSensitivity:
    caution = (
        "환율 민감도는 USD/KRW 1% 변동을 보유 평가액에 단순 적용한 가정치이며, "
        "투자 판단을 대신하는 지표가 아닙니다."
    )
    if market != "us" and currency != "USD":
        return StockDetailFxSensitivity(
            status="not_applicable",
            basis="not_applicable",
            caution="KRW 자산은 별도 USD/KRW 환율 민감도 계산을 표시하지 않습니다.",
        )
    if holding is None:
        return StockDetailFxSensitivity(
            status="missing_holding",
            basis="not_applicable",
            caution="보유 수량이 없어 환율 민감도 계산을 표시하지 않습니다.",
        )
    if holding.valueNative is None or holding.valueNative <= 0:
        return StockDetailFxSensitivity(
            status="missing_native_value",
            holdingValueKrw=holding.valueKrw,
            basis="not_applicable",
            caution="USD 보유 평가액이 없어 환율 민감도 계산을 표시하지 않습니다.",
        )
    if fx_rate is None or fx_rate <= 0:
        return StockDetailFxSensitivity(
            status="missing_fx_rate",
            holdingValueNative=holding.valueNative,
            holdingValueKrw=holding.valueKrw,
            basis="not_applicable",
            caution="USD/KRW 환율을 확인하지 못해 환율 민감도 계산을 표시하지 않습니다.",
        )

    scenarios = [
        StockDetailFxScenario(
            rateMovePct=move_pct,
            estimatedKrwImpact=holding.valueNative * fx_rate * (move_pct / 100),
            estimatedValueKrw=holding.valueNative * fx_rate * (1 + move_pct / 100),
            label=f"USD/KRW {move_pct:+.0f}%",
        )
        for move_pct in (-1.0, 1.0)
    ]
    return StockDetailFxSensitivity(
        status="available",
        currencyPair="USD/KRW",
        baseFxRate=fx_rate,
        holdingValueNative=holding.valueNative,
        holdingValueKrw=holding.valueKrw,
        basis="portfolio_value",
        scenarios=scenarios,
        caution=caution,
    )


__all__ = ["StockDetailProviders", "build_stock_detail"]
