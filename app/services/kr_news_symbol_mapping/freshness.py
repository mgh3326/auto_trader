"""뉴스-종목 매핑 freshness 파생 (ROB-398 Slice 1, 순수 함수).

가장 신선한 매핑 기사 as_of 가 TTL 내면 fresh, 초과면 stale, 0건이면
unavailable. reason 을 동봉해 숨김 없이 노출한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.services.kr_news_symbol_mapping.contract import (
    FRESHNESS_TTL_HOURS,
    Freshness,
)


def derive_freshness(
    as_ofs: Sequence[datetime],
    *,
    now: datetime,
    ttl_hours: int = FRESHNESS_TTL_HOURS,
) -> Freshness:
    if not as_ofs:
        return Freshness(
            overall="unavailable", latest_as_of=None, stale_reason="no_mapped_news"
        )
    latest = max(as_ofs)
    if now - latest <= timedelta(hours=ttl_hours):
        return Freshness(overall="fresh", latest_as_of=latest, stale_reason=None)
    return Freshness(
        overall="stale", latest_as_of=latest, stale_reason="older_than_ttl"
    )
