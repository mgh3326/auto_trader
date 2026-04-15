from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.timezone import now_kst
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_bollinger,
    _calculate_rsi,
    _calculate_stoch_rsi,
    _fetch_ohlcv_for_indicators,
)
from app.schemas.n8n.sell_signal import N8nSellCondition
from app.services.brokers.kis.client import KISClient

logger = logging.getLogger(__name__)

REDIS_RSI_PREFIX = "sell_signal:rsi_state"
TRIGGER_THRESHOLD = 2


async def _get_redis() -> aioredis.Redis:
    return aioredis.from_url(
        settings.get_redis_url(),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        decode_responses=True,
    )


async def _fetch_current_price(
    kis: KISClient, symbol: str
) -> tuple[float | None, str | None]:
    try:
        df = await kis.inquire_price(symbol)
        if df.empty:
            return None, None
        row = df.iloc[0]
        return float(row["close"]), None
    except Exception as exc:
        return None, str(exc)


async def _fetch_stock_name(kis: KISClient, symbol: str) -> str:
    try:
        info = await kis.fetch_fundamental_info(symbol)
        return info.get("종목명", symbol)
    except Exception:
        return symbol


async def _check_trailing_stop(
    kis: KISClient, symbol: str, threshold: float
) -> tuple[N8nSellCondition, float | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    price, err = await _fetch_current_price(kis, symbol)
    if err:
        errors.append({"condition": "trailing_stop", "error": err})

    met = price is not None and price <= threshold
    return (
        N8nSellCondition(
            name="trailing_stop",
            met=met,
            value=price,
            threshold=threshold,
            detail=f"현재가 ₩{price:,.0f}" if price else "가격 조회 실패",
        ),
        price,
        errors,
    )


async def _check_stoch_rsi(
    symbol: str, threshold: float
) -> tuple[N8nSellCondition, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    try:
        df = await _fetch_ohlcv_for_indicators(symbol, "equity_kr", count=200)
        if df.empty or len(df) < 30:
            return (
                N8nSellCondition(
                    name="stoch_rsi",
                    met=False,
                    value=None,
                    threshold=threshold,
                    detail="OHLCV 데이터 부족",
                ),
                errors,
            )
        close = df["close"].astype(float)
        stoch = _calculate_stoch_rsi(close)
        k_val = stoch.get("k")
        met = k_val is not None and k_val < threshold
        return (
            N8nSellCondition(
                name="stoch_rsi",
                met=met,
                value=round(k_val, 2) if k_val is not None else None,
                threshold=threshold,
                detail=f"StochRSI K={k_val:.1f}" if k_val is not None else "계산 불가",
            ),
            errors,
        )
    except Exception as exc:
        errors.append({"condition": "stoch_rsi", "error": str(exc)})
        return (
            N8nSellCondition(
                name="stoch_rsi",
                met=False,
                value=None,
                threshold=threshold,
                detail=str(exc),
            ),
            errors,
        )


async def _check_foreign_selling(
    kis: KISClient, symbol: str, consecutive_days: int
) -> tuple[N8nSellCondition, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    try:
        rows = await kis.inquire_investor(symbol)
        if not rows or len(rows) < consecutive_days:
            return (
                N8nSellCondition(
                    name="foreign_selling",
                    met=False,
                    value=None,
                    detail="투자자 데이터 부족",
                ),
                errors,
            )

        net_sells = 0
        for row in rows[:consecutive_days]:
            frgn_ntby = float(row.get("frgn_ntby_qty", 0))
            if frgn_ntby < 0:
                net_sells += 1

        met = net_sells >= consecutive_days
        detail_parts = []
        for i, row in enumerate(rows[:consecutive_days]):
            qty = float(row.get("frgn_ntby_qty", 0))
            label = "순매도" if qty < 0 else "순매수"
            detail_parts.append(f"D-{i}: {label} {abs(qty):,.0f}주")

        return (
            N8nSellCondition(
                name="foreign_selling",
                met=met,
                value=None,
                detail=f"{net_sells}일 연속 순매도" if met else "; ".join(detail_parts),
            ),
            errors,
        )
    except Exception as exc:
        errors.append({"condition": "foreign_selling", "error": str(exc)})
        return (
            N8nSellCondition(
                name="foreign_selling",
                met=False,
                value=None,
                detail=str(exc),
            ),
            errors,
        )


async def _check_rsi_momentum(
    symbol: str, high_mark: float, low_mark: float
) -> tuple[N8nSellCondition, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    redis_key = f"{REDIS_RSI_PREFIX}:{symbol}"

    try:
        df = await _fetch_ohlcv_for_indicators(symbol, "equity_kr", count=200)
        if df.empty or len(df) < 20:
            return (
                N8nSellCondition(
                    name="rsi_momentum",
                    met=False,
                    value=None,
                    detail="OHLCV 데이터 부족",
                ),
                errors,
            )

        close = df["close"].astype(float)
        rsi_result = _calculate_rsi(close, 14)
        current_rsi = rsi_result.get("14")
        if current_rsi is None:
            return (
                N8nSellCondition(
                    name="rsi_momentum",
                    met=False,
                    value=None,
                    detail="RSI 계산 불가",
                ),
                errors,
            )

        r = await _get_redis()
        try:
            raw = await r.get(redis_key)
            prev_state = json.loads(raw) if raw else {}
        except Exception:
            prev_state = {}

        was_above_high = prev_state.get("was_above_high", False)

        if current_rsi >= high_mark:
            was_above_high = True
        met = was_above_high and current_rsi <= low_mark

        new_state = {
            "rsi": round(current_rsi, 2),
            "was_above_high": was_above_high and not met,
            "updated_at": now_kst().isoformat(),
        }
        await r.set(redis_key, json.dumps(new_state), ex=86400 * 7)
        await r.aclose()

        if met:
            detail = f"{high_mark}→{current_rsi:.1f} 하락"
        elif was_above_high:
            detail = f"RSI {current_rsi:.1f} (고점 {high_mark} 돌파 이력 있음)"
        else:
            detail = f"RSI {current_rsi:.1f} (아직 {high_mark} 미돌파)"

        return (
            N8nSellCondition(
                name="rsi_momentum",
                met=met,
                value=round(current_rsi, 2),
                detail=detail,
            ),
            errors,
        )
    except Exception as exc:
        errors.append({"condition": "rsi_momentum", "error": str(exc)})
        return (
            N8nSellCondition(
                name="rsi_momentum",
                met=False,
                value=None,
                detail=str(exc),
            ),
            errors,
        )


async def _check_bollinger_reentry(
    symbol: str, current_price: float | None, bb_upper_ref: float
) -> tuple[N8nSellCondition, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    try:
        df = await _fetch_ohlcv_for_indicators(symbol, "equity_kr", count=200)
        if df.empty or len(df) < 25:
            return (
                N8nSellCondition(
                    name="bollinger_reentry",
                    met=False,
                    value=None,
                    detail="OHLCV 데이터 부족",
                ),
                errors,
            )

        close = df["close"].astype(float)
        bb = _calculate_bollinger(close)
        bb_upper = bb.get("upper")
        if bb_upper is None or current_price is None:
            return (
                N8nSellCondition(
                    name="bollinger_reentry",
                    met=False,
                    value=None,
                    detail="볼린저밴드 계산 불가",
                ),
                errors,
            )

        prices = close.values
        was_above = False
        re_entered = False
        for i in range(max(0, len(prices) - 10), len(prices) - 1):
            if prices[i] > bb_upper_ref:
                was_above = True
            elif was_above and prices[i] <= bb_upper_ref:
                re_entered = True
                break

        met = re_entered and current_price < bb_upper
        if met:
            detail = f"밴드 상단 ₩{bb_upper:,.0f} 아래 재진입 후 반등 실패"
        elif was_above and not re_entered:
            detail = f"밴드 상단 위 (현재가 > ₩{bb_upper_ref:,.0f})"
        else:
            detail = f"밴드 상단 ₩{bb_upper:,.0f}"

        return (
            N8nSellCondition(
                name="bollinger_reentry",
                met=met,
                value=round(bb_upper, 0) if bb_upper else None,
                detail=detail,
            ),
            errors,
        )
    except Exception as exc:
        errors.append({"condition": "bollinger_reentry", "error": str(exc)})
        return (
            N8nSellCondition(
                name="bollinger_reentry",
                met=False,
                value=None,
                detail=str(exc),
            ),
            errors,
        )


async def evaluate_sell_signal(
    symbol: str,
    price_threshold: float = 1_152_000,
    stoch_rsi_threshold: float = 80,
    foreign_consecutive_days: int = 2,
    rsi_high_mark: float = 70,
    rsi_low_mark: float = 65,
    bb_upper_ref: float = 1_142_000,
) -> dict[str, Any]:
    kis = KISClient()
    all_errors: list[dict[str, Any]] = []

    stock_name = await _fetch_stock_name(kis, symbol)

    trailing_cond, current_price, errs = await _check_trailing_stop(
        kis, symbol, price_threshold
    )
    all_errors.extend(errs)

    stoch_cond, errs = await _check_stoch_rsi(symbol, stoch_rsi_threshold)
    all_errors.extend(errs)

    foreign_cond, errs = await _check_foreign_selling(
        kis, symbol, foreign_consecutive_days
    )
    all_errors.extend(errs)

    rsi_cond, errs = await _check_rsi_momentum(symbol, rsi_high_mark, rsi_low_mark)
    all_errors.extend(errs)

    bb_cond, errs = await _check_bollinger_reentry(symbol, current_price, bb_upper_ref)
    all_errors.extend(errs)

    conditions = [trailing_cond, stoch_cond, foreign_cond, rsi_cond, bb_cond]
    conditions_met = sum(1 for c in conditions if c.met)
    triggered = conditions_met >= TRIGGER_THRESHOLD

    met_names = [c.name for c in conditions if c.met]
    if triggered:
        message = f"[매도 검토] {stock_name} {conditions_met}/5 조건 충족 ({', '.join(met_names)})"
    else:
        message = f"[매도 대기] {stock_name} {conditions_met}/5 조건 충족"

    return {
        "symbol": symbol,
        "name": stock_name,
        "triggered": triggered,
        "conditions_met": conditions_met,
        "conditions": conditions,
        "message": message,
        "errors": all_errors,
    }
