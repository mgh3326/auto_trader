"""ROB-626: shared confirmed-daily investor-flow helpers (Naver-backed).

Pure ownership/derivation helpers + a best-effort confirmed-daily block
builder, shared by ``get_investor_trends`` (daily enrichment) and
``get_intraday_investor_flow`` (confirmed block embed + freshness anchor).
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_investor_trends_naver,
)

# Ownership-rate delta below this magnitude (pp) reads as flat, not up/down.
_OWNERSHIP_FLAT_EPS = 0.01


def derive_individual_net(institutional_net: Any, foreign_net: Any) -> int | None:
    """개인 순매수 = -(기관 + 외인). 한쪽이라도 None이면 None."""
    if institutional_net is None or foreign_net is None:
        return None
    return -(int(institutional_net) + int(foreign_net))


def holding_rate_change(
    rows_newest_first: list[dict[str, Any]],
) -> float | None:
    """ROB-448: 외인 보유율 델타 (newest − oldest, pp). 끝점 결측이면 None."""
    if not rows_newest_first:
        return None
    newest = rows_newest_first[0].get("foreign_holding_rate")
    oldest = rows_newest_first[-1].get("foreign_holding_rate")
    if newest is None or oldest is None:
        return None
    return round(newest - oldest, 2)


def ownership_trend(rate_change: float | None) -> str | None:
    """rate_change(pp) → 'up' | 'down' | 'flat' | None."""
    if rate_change is None:
        return None
    if abs(rate_change) < _OWNERSHIP_FLAT_EPS:
        return "flat"
    return "up" if rate_change > 0 else "down"


def ownership_summary(
    rows_newest_first: list[dict[str, Any]],
) -> dict[str, Any]:
    """{foreign_ownership_pct, foreign_ownership_trend, foreign_ownership_rate_change}."""
    pct = (
        rows_newest_first[0].get("foreign_holding_rate") if rows_newest_first else None
    )
    change = holding_rate_change(rows_newest_first)
    return {
        "foreign_ownership_pct": pct,
        "foreign_ownership_trend": ownership_trend(change),
        "foreign_ownership_rate_change": change,
    }


async def build_confirmed_block(
    symbol: str, days: int = 5
) -> tuple[dict[str, Any], str | None]:
    """Best-effort Naver 확정 일별 블록 + freshness 앵커.

    Returns ``(block, last_confirmed_date)``. Naver 페치 예외를 흡수하여 intraday
    도구 전체 실패를 막는다(열화: error 키 + 빈 history).
    """
    try:
        fetched = await _fetch_investor_trends_naver(symbol, days)
        rows = fetched.get("data") or []
    except Exception as exc:  # noqa: BLE001 — best-effort degrade
        return (
            {
                "source": "naver",
                "error": str(exc),
                "foreign_ownership_pct": None,
                "foreign_ownership_trend": None,
                "foreign_ownership_rate_change": None,
                "history": [],
                "days": 0,
            },
            None,
        )

    history = [
        {
            "date": row.get("date"),
            "foreign_net": row.get("foreign_net"),
            "institutional_net": row.get("institutional_net"),
            "individual_net": derive_individual_net(
                row.get("institutional_net"), row.get("foreign_net")
            ),
            "close": row.get("close"),
        }
        for row in rows
    ]
    block = {
        "source": "naver",
        **ownership_summary(rows),
        "history": history,
        "days": len(history),
    }
    last_confirmed_date = rows[0].get("date") if rows else None
    return block, last_confirmed_date
