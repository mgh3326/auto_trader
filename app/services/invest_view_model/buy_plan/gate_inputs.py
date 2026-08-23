"""Discovery-gate metric readers for the 매수 계획 board (§144차).

``config/trading_policy.yaml`` → ``market_rules.crypto.recovery_gate`` names
two metrics by id. This module resolves exactly those two and nothing else:

``alt_breadth_24h`` (``upbit_alt_breadth_24h``)
    Share of KRW-quoted alts outperforming KRW-BTC over 24h, from the official
    Upbit Open API ticker via :func:`app.services.external.upbit_index
    .fetch_upbit_altseason`. That function's own docstring defines breadth in
    exactly the policy's terms, so no re-derivation happens here.

``btc_long_short_ratio``
    Binance ``globalLongShortAccountRatio`` and ``topLongShortPositionRatio``.
    The policy says "both report inputs should remain at or below the
    threshold", so the resolved value is the **maximum** of the two legs —
    comparing that single number with ``lte`` reproduces the two-leg rule. If
    either leg is missing the value is ``None``, never the surviving leg.

Both readers are fail-open to ``None``. The policy's
``missing_or_null_threshold: do_not_infer_or_count_as_met`` then makes a
``None`` an ``unavailable`` condition, which cannot count toward the gate — so
a dead upstream leaves the gate un-passable rather than silently open.

The board is a read surface, so results are cached for
``GATE_CACHE_TTL_SECONDS`` to keep a page refresh from fanning out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

logger = logging.getLogger(__name__)

GATE_CACHE_TTL_SECONDS: Final = 180

ALT_BREADTH_SOURCE: Final = "upbit_open_api_ticker_derived"
LONG_SHORT_SOURCE: Final = "binance_global_account+binance_top_trader_position"


@dataclass(frozen=True, slots=True)
class GateMetricReading:
    """One resolved (or unresolved) gate metric."""

    metric: str
    value: Decimal | None
    source: str
    note: str | None = None


_cache: dict[str, tuple[float, GateMetricReading]] = {}
_cache_lock = asyncio.Lock()


def _reset_cache_for_tests() -> None:
    _cache.clear()


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _cached(
    key: str,
    producer: Callable[[], Awaitable[GateMetricReading]],
    *,
    now: float,
) -> GateMetricReading:
    """Serve a fresh-enough reading, else produce one and store it.

    ``producer`` is a factory rather than an already-created coroutine so a
    cache hit does not leave an un-awaited coroutine behind.
    """

    async with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < GATE_CACHE_TTL_SECONDS:
            return hit[1]
    reading = await producer()
    async with _cache_lock:
        _cache[key] = (now, reading)
    return reading


async def read_alt_breadth_24h(*, now: float | None = None) -> GateMetricReading:
    """Percent of KRW alts outperforming BTC over 24h, or ``None``."""

    return await _cached(
        "alt_breadth_24h", _read_alt_breadth_24h, now=now or time.monotonic()
    )


async def _read_alt_breadth_24h() -> GateMetricReading:
    try:
        from app.services.external.upbit_index import fetch_upbit_altseason

        payload = await fetch_upbit_altseason()
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("buy_plan: alt breadth unavailable: %s", exc)
        return GateMetricReading(
            metric="upbit_alt_breadth_24h",
            value=None,
            source=ALT_BREADTH_SOURCE,
            note=f"조회 실패: {exc}",
        )

    breadth = (payload or {}).get("breadth") if isinstance(payload, dict) else None
    fraction = _to_decimal((breadth or {}).get("alts_beating_btc_pct"))
    if fraction is None:
        return GateMetricReading(
            metric="upbit_alt_breadth_24h",
            value=None,
            source=ALT_BREADTH_SOURCE,
            note="Upbit 티커에서 breadth를 산출하지 못했습니다.",
        )
    total = (breadth or {}).get("alts_total")
    beating = (breadth or {}).get("alts_beating_btc")
    return GateMetricReading(
        metric="upbit_alt_breadth_24h",
        # The upstream reports a 0..1 fraction; the policy threshold is in
        # percent, so convert once here rather than at the comparison site.
        value=fraction * Decimal(100),
        source=ALT_BREADTH_SOURCE,
        note=f"{beating}/{total} alts > KRW-BTC (24h)"
        if total is not None and beating is not None
        else None,
    )


async def read_btc_long_short_ratio(*, now: float | None = None) -> GateMetricReading:
    """The worse (higher) of the two Binance long/short legs, or ``None``."""

    return await _cached(
        "btc_long_short_ratio",
        _read_btc_long_short_ratio,
        now=now or time.monotonic(),
    )


async def _read_btc_long_short_ratio() -> GateMetricReading:
    try:
        from app.mcp_server.tooling.fundamentals._crypto import (
            handle_get_long_short_ratio,
        )

        payload = await handle_get_long_short_ratio("BTC", "1h", 1)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("buy_plan: long/short ratio unavailable: %s", exc)
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note=f"조회 실패: {exc}",
        )

    if not isinstance(payload, dict) or payload.get("error"):
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note="Binance 롱숏 비율 응답을 해석하지 못했습니다.",
        )

    legs = [
        _to_decimal((payload.get(key) or {}).get("ratio"))
        for key in ("global_account", "top_position")
    ]
    if any(leg is None for leg in legs):
        # Deliberately not "use whichever leg answered": the policy asks both
        # reports to sit at or below the threshold, so one leg cannot stand in
        # for the pair without weakening the gate.
        return GateMetricReading(
            metric="btc_long_short_ratio",
            value=None,
            source=LONG_SHORT_SOURCE,
            note="두 리포트 중 하나가 비어 있어 판정 불가(한쪽만으로 대체하지 않음).",
        )

    resolved = max(leg for leg in legs if leg is not None)
    return GateMetricReading(
        metric="btc_long_short_ratio",
        value=resolved,
        source=LONG_SHORT_SOURCE,
        note="global_account / top_position 중 더 높은 값",
    )
