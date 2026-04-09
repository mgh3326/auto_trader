import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.portfolio_links import build_position_detail_url
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.brokers.kis.client import KISClient
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

from .kis_market_adapters import (
    DomesticAutomationAdapter,
    OverseasAutomationAdapter,
    StockContext,
    extract_domestic_stock_info,
    extract_overseas_stock_info,
    match_domestic_stock,
    match_overseas_stock,
)

logger = logging.getLogger(__name__)

STATUS_FETCHING_HOLDINGS = "보유 주식 조회 중..."
NO_DOMESTIC_STOCKS_MESSAGE = "보유 중인 국내 주식이 없습니다."
NO_OVERSEAS_STOCKS_MESSAGE = "보유 중인 해외 주식이 없습니다."


# === Handler wrappers (unified signature for adapter + config) ===


async def _domestic_buy(kis, symbol, price, avg, *, exchange_code=None):
    from app.services.kis_trading_service import (
        process_kis_domestic_buy_orders_with_analysis,
    )

    return await process_kis_domestic_buy_orders_with_analysis(kis, symbol, price, avg)


async def _domestic_sell(kis, symbol, price, avg, qty, *, exchange_code=None):
    from app.services.kis_trading_service import (
        process_kis_domestic_sell_orders_with_analysis,
    )

    return await process_kis_domestic_sell_orders_with_analysis(
        kis, symbol, price, avg, qty
    )


async def _overseas_buy(kis, symbol, price, avg, *, exchange_code=None):
    from app.services.kis_trading_service import (
        process_kis_overseas_buy_orders_with_analysis,
    )

    return await process_kis_overseas_buy_orders_with_analysis(
        kis, symbol, price, avg, exchange_code or "NASD"
    )


async def _overseas_sell(kis, symbol, price, avg, qty, *, exchange_code=None):
    from app.services.kis_trading_service import (
        process_kis_overseas_sell_orders_with_analysis,
    )

    return await process_kis_overseas_sell_orders_with_analysis(
        kis, symbol, price, avg, qty, exchange_code or "NASD"
    )


# === Price fetch helpers ===


async def _fetch_domestic_new_price(kis: KISClient, symbol: str) -> float:
    info = await kis.fetch_price(symbol)
    return float(info["output"]["stck_prpr"])


async def _fetch_overseas_new_price(kis: KISClient, symbol: str) -> float:
    df = await kis.inquire_overseas_price(symbol)
    if not df.empty:
        return float(df.iloc[0]["close"])
    return 0.0


# === MarketHoldingsConfig ===


@dataclass(frozen=True, slots=True)
class MarketHoldingsConfig:
    """Market-specific configuration for bulk and single-stock operations."""

    fetch_holdings: Callable[..., Awaitable[list[dict[str, Any]]]]
    extract_info: Callable[[dict[str, Any]], StockContext]
    match_stock: Callable[[list[dict[str, Any]], str], dict[str, Any] | None]
    resolve_exchange: Callable[..., Awaitable[str]] | None
    process_buy_name: str
    process_sell_name: str
    fetch_new_price: Callable[..., Awaitable[float]]
    no_stocks_message: str
    result_symbol_key: str
    market_type_label: str | None  # None = no Telegram notifications


async def _resolve_overseas_exchange_code(
    symbol: str,
    preferred_exchange: str | None,
) -> str:
    normalized_preferred = str(preferred_exchange or "").strip().upper()
    if normalized_preferred:
        return normalized_preferred
    return await get_us_exchange_by_symbol(symbol)


_DOMESTIC_CFG = MarketHoldingsConfig(
    fetch_holdings=lambda kis: kis.fetch_my_stocks(),
    extract_info=extract_domestic_stock_info,
    match_stock=match_domestic_stock,
    resolve_exchange=None,
    process_buy_name="_domestic_buy",
    process_sell_name="_domestic_sell",
    fetch_new_price=_fetch_domestic_new_price,
    no_stocks_message=NO_DOMESTIC_STOCKS_MESSAGE,
    result_symbol_key="code",
    market_type_label=None,
)

_OVERSEAS_CFG = MarketHoldingsConfig(
    fetch_holdings=lambda kis: kis.fetch_my_overseas_stocks(),
    extract_info=extract_overseas_stock_info,
    match_stock=match_overseas_stock,
    resolve_exchange=_resolve_overseas_exchange_code,
    process_buy_name="_overseas_buy",
    process_sell_name="_overseas_sell",
    fetch_new_price=_fetch_overseas_new_price,
    no_stocks_message=NO_OVERSEAS_STOCKS_MESSAGE,
    result_symbol_key="symbol",
    market_type_label="해외주식",
)


async def _send_toss_recommendation_async(
    code: str,
    name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None = None,
    kis_avg_price: float | None = None,
    market_type: str = "kr",
    currency: str = "원",
) -> None:
    """수동 잔고(토스) 종목에 대해 AI 분석 결과와 가격 제안 알림 발송.

    AI 결정(buy/hold/sell)과 무관하게 항상 가격 제안을 포함하여 알림을 발송합니다.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    notifier = get_trade_notifier()
    if not notifier._enabled:
        logger.debug(f"[토스추천] {name}({code}) - 알림 비활성화됨")
        return

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(code)

        if not analysis:
            logger.warning(f"[토스추천] {name}({code}) - 분석 결과 없음, 알림 스킵")
            return

        decision = analysis.decision.lower() if analysis.decision else "hold"
        confidence = analysis.confidence if analysis.confidence else 0
        raw_reasons = analysis.reasons
        if isinstance(raw_reasons, list):
            reasons = [str(r) for r in raw_reasons]
        elif isinstance(raw_reasons, str):
            try:
                parsed = json.loads(raw_reasons)
                reasons = (
                    [str(r) for r in parsed]
                    if isinstance(parsed, list)
                    else [str(parsed)]
                )
            except Exception as parse_error:
                logger.debug(
                    "Failed to parse analysis reasons for %s(%s): %s",
                    name,
                    code,
                    parse_error,
                )
                reasons = [raw_reasons]
        else:
            reasons = []

        # AI 결정과 무관하게 항상 가격 제안 알림 발송
        detail_url = build_position_detail_url(code, market_type)
        await notifier.notify_toss_price_recommendation(
            symbol=code,
            korean_name=name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            appropriate_buy_min=analysis.appropriate_buy_min,
            appropriate_buy_max=analysis.appropriate_buy_max,
            appropriate_sell_min=analysis.appropriate_sell_min,
            appropriate_sell_max=analysis.appropriate_sell_max,
            buy_hope_min=analysis.buy_hope_min,
            buy_hope_max=analysis.buy_hope_max,
            sell_target_min=analysis.sell_target_min,
            sell_target_max=analysis.sell_target_max,
            currency=currency,
            detail_url=detail_url,
        )
        logger.info(
            f"[토스추천] {name}({code}) - 가격 제안 알림 발송 (AI 판단: {decision}, 신뢰도: {confidence}%)"
        )


# === Unified Bulk and Task Implementations ===


async def _execute_bulk_buy_orders(cfg: MarketHoldingsConfig) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        if not my_stocks:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": cfg.no_stocks_message,
                "results": [],
            }

        results = []
        for stock in my_stocks:
            ctx = cfg.extract_info(stock)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )

            try:
                process_buy = globals()[cfg.process_buy_name]
                res = await process_buy(
                    kis,
                    ctx.symbol,
                    ctx.current_price,
                    ctx.avg_price,
                    exchange_code=ctx.exchange_code,
                )
                results.append(
                    {
                        "name": ctx.name,
                        cfg.result_symbol_key: ctx.symbol,
                        "success": res["success"],
                        "message": res["message"],
                    }
                )
                if (
                    cfg.market_type_label
                    and res.get("success")
                    and res.get("orders_placed", 0) > 0
                ):
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_buy_order(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            order_count=res.get("orders_placed", 0),
                            total_amount=res.get("total_amount", 0.0),
                            prices=res.get("prices", []),
                            volumes=res.get("quantities", []),
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
            except Exception as e:
                results.append(
                    {
                        "name": ctx.name,
                        cfg.result_symbol_key: ctx.symbol,
                        "success": False,
                        "error": str(e),
                    }
                )
                if cfg.market_type_label:
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            reason=f"매수 주문 실패: {e}",
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": len(my_stocks),
            "message": f"{success_count}/{len(my_stocks)}개 종목 매수 주문 완료",
            "results": results,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _execute_bulk_sell_orders(cfg: MarketHoldingsConfig) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        if not my_stocks:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": cfg.no_stocks_message,
                "results": [],
            }

        results = []
        for stock in my_stocks:
            ctx = cfg.extract_info(stock)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )

            try:
                process_sell = globals()[cfg.process_sell_name]
                res = await process_sell(
                    kis,
                    ctx.symbol,
                    ctx.current_price,
                    ctx.avg_price,
                    ctx.qty,
                    exchange_code=ctx.exchange_code,
                )
                results.append(
                    {
                        "name": ctx.name,
                        cfg.result_symbol_key: ctx.symbol,
                        "success": res["success"],
                        "message": res["message"],
                    }
                )
                if (
                    cfg.market_type_label
                    and res.get("success")
                    and res.get("orders_placed", 0) > 0
                ):
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_sell_order(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            order_count=res.get("orders_placed", 0),
                            total_volume=res.get("total_volume", 0),
                            prices=res.get("prices", []),
                            volumes=res.get("quantities", []),
                            expected_amount=res.get("expected_amount", 0.0),
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
            except Exception as e:
                results.append(
                    {
                        "name": ctx.name,
                        cfg.result_symbol_key: ctx.symbol,
                        "success": False,
                        "error": str(e),
                    }
                )
                if cfg.market_type_label:
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            reason=f"매도 주문 실패: {e}",
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": len(my_stocks),
            "message": f"{success_count}/{len(my_stocks)}개 종목 매도 주문 완료",
            "results": results,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _execute_single_buy_task(cfg: MarketHoldingsConfig, symbol: str) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        target = cfg.match_stock(my_stocks, symbol)

        if target:
            ctx = cfg.extract_info(target)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )
        else:
            try:
                current_price = await cfg.fetch_new_price(kis, symbol)
            except Exception as price_error:
                return {"success": False, "message": f"현재가 조회 실패: {price_error}"}
            exchange_code = (
                await cfg.resolve_exchange(symbol, None)
                if cfg.resolve_exchange
                else None
            )
            ctx = StockContext(
                symbol=symbol,
                name="",
                avg_price=0.0,
                current_price=current_price,
                qty=0,
                is_manual=False,
                exchange_code=exchange_code,
            )

        process_buy = globals()[cfg.process_buy_name]
        return await process_buy(
            kis,
            ctx.symbol,
            ctx.current_price,
            ctx.avg_price,
            exchange_code=ctx.exchange_code,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _execute_single_sell_task(cfg: MarketHoldingsConfig, symbol: str) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        target = cfg.match_stock(my_stocks, symbol)

        if not target:
            return {"success": False, "message": "보유 중인 주식이 아닙니다."}

        ctx = cfg.extract_info(target)
        if cfg.resolve_exchange:
            ctx.exchange_code = await cfg.resolve_exchange(
                ctx.symbol, ctx.exchange_code
            )

        process_sell = globals()[cfg.process_sell_name]
        return await process_sell(
            kis,
            ctx.symbol,
            ctx.current_price,
            ctx.avg_price,
            ctx.qty,
            exchange_code=ctx.exchange_code,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


# === Analysis (dead code) ===


def _analyze_stock_ignored(symbol: str) -> dict:
    if not symbol:
        return {"status": "failed", "error": "종목 코드/심볼이 필요합니다."}
    return {
        "status": "ignored",
        "symbol": symbol,
        "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.",
    }


async def run_analysis_for_my_domestic_stocks() -> dict:
    return {
        "status": "ignored",
        "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.",
        "results": [],
    }


async def run_analysis_for_my_overseas_stocks() -> dict:
    return {
        "status": "ignored",
        "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.",
        "results": [],
    }


async def analyze_domestic_stock_task(symbol: str) -> dict:
    return _analyze_stock_ignored(symbol)


async def analyze_overseas_stock_task(symbol: str) -> dict:
    return _analyze_stock_ignored(symbol)


# === Bulk orders ===


async def execute_domestic_buy_orders() -> dict:
    return await _execute_bulk_buy_orders(_DOMESTIC_CFG)


async def execute_overseas_buy_orders() -> dict:
    return await _execute_bulk_buy_orders(_OVERSEAS_CFG)


async def execute_domestic_sell_orders() -> dict:
    return await _execute_bulk_sell_orders(_DOMESTIC_CFG)


async def execute_overseas_sell_orders() -> dict:
    return await _execute_bulk_sell_orders(_OVERSEAS_CFG)


# === Single stock tasks ===


async def execute_domestic_buy_order_task(symbol: str) -> dict:
    return await _execute_single_buy_task(_DOMESTIC_CFG, symbol)


async def execute_overseas_buy_order_task(symbol: str) -> dict:
    return await _execute_single_buy_task(_OVERSEAS_CFG, symbol)


async def execute_domestic_sell_order_task(symbol: str) -> dict:
    return await _execute_single_sell_task(_DOMESTIC_CFG, symbol)


async def execute_overseas_sell_order_task(symbol: str) -> dict:
    return await _execute_single_sell_task(_OVERSEAS_CFG, symbol)


# === Per-stock automation ===


async def run_per_domestic_stock_automation() -> dict:
    """국내 주식 종목별 자동 실행 (미체결취소 -> 분석 -> 매수 -> 매도)"""
    from app.core.db import AsyncSessionLocal
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    from . import kis_automation_runner

    adapter = DomesticAutomationAdapter(
        kis_client_factory=KISClient,
        async_session_factory=AsyncSessionLocal,
        manual_holdings_service_factory=ManualHoldingsService,
        manual_market_type=MarketType.KR,
        buy_handler=_domestic_buy,
        sell_handler=_domestic_sell,
        send_toss_recommendation=_send_toss_recommendation_async,
        notifier_factory=get_trade_notifier,
        no_stocks_message=NO_DOMESTIC_STOCKS_MESSAGE,
    )
    return await kis_automation_runner.run_market_automation(adapter=adapter)


async def run_per_overseas_stock_automation() -> dict:
    """해외 주식 종목별 자동 실행 (미체결취소 -> 분석 -> 매수 -> 매도)"""
    from app.core.db import AsyncSessionLocal
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    from . import kis_automation_runner

    adapter = OverseasAutomationAdapter(
        kis_client_factory=KISClient,
        async_session_factory=AsyncSessionLocal,
        manual_holdings_service_factory=ManualHoldingsService,
        manual_market_type=MarketType.US,
        buy_handler=_overseas_buy,
        sell_handler=_overseas_sell,
        send_toss_recommendation=_send_toss_recommendation_async,
        notifier_factory=get_trade_notifier,
        no_stocks_message=NO_OVERSEAS_STOCKS_MESSAGE,
    )
    return await kis_automation_runner.run_market_automation(adapter=adapter)
