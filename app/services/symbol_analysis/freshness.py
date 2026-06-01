"""core-aware freshness 파생 (ROB-397).

overall 은 core 필드(price/consensus/technicals)만으로 결정한다. 보조
필드(flow/valuation)는 stale 이어도 overall 을 stale 로 떨어뜨리지 않는다
(ROB-323 anti-pattern 회피). stale_fields[] 에는 모든 stale 카테고리를 나열.
"""

from __future__ import annotations

from datetime import date, datetime

from app.services.symbol_analysis.authority import CORE_CATEGORIES
from app.services.symbol_analysis.contract import FieldBlock, Freshness


def compute_is_stale(
    category: str,
    as_of: datetime | None,
    *,
    trading_date: date,
) -> bool:
    """as_of 가 부재하거나 당일(trading_date)이 아니면 stale.

    price 의 전일종가 폴백은 as_of.date() < trading_date 이므로 정규장
    세션에서 stale 로 표면화된다 (ROB-396 증상2).
    """

    if as_of is None:
        return True
    return as_of.date() != trading_date


def derive_freshness(blocks: dict[str, FieldBlock]) -> Freshness:
    """필드별 is_stale/value 로부터 core-aware overall + stale_fields 파생."""

    stale_fields = tuple(
        cat for cat, b in blocks.items() if b.value is None or b.is_stale
    )

    price = blocks.get("price")
    # 1) 가격 앵커 부재 → unavailable
    if price is None or price.value is None:
        return Freshness(overall="unavailable", stale_fields=stale_fields)

    core_stale = any(
        cat in CORE_CATEGORIES and (blocks[cat].value is None or blocks[cat].is_stale)
        for cat in blocks
    )
    if core_stale:
        return Freshness(overall="stale", stale_fields=stale_fields)

    if stale_fields:
        return Freshness(overall="partial", stale_fields=stale_fields)

    return Freshness(overall="fresh", stale_fields=())
