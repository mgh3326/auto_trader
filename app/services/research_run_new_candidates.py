"""ROB-918: kr-preopen new-candidate shadow section (advisory-only, 2-week shadow).

Builds a read-only "new candidates" observation block — 전일 consecutive_gainers
상위 + 전일 마감 테마 상위 + 수급 double_buy 요약 — for injection into
``TradingDecisionSession.market_brief``. This module NEVER creates
``trading_decision_proposals`` rows and NEVER touches broker/order/watch state;
it only reads from existing snapshot tables.

Safety-by-construction: the public entrypoint opens its own DB session (never
the caller's) so any read failure here can never poison a caller's write
transaction, and this module structurally has no access to write a proposal
row even by mistake.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_screener_snapshots.partition_health import (
    resolve_healthy_partition,
)
from app.services.invest_view_model.double_buy_screener import (
    load_double_buy_from_snapshots,
)
from app.services.market_valuation_snapshots.repository import metric_rich_filter

logger = logging.getLogger(__name__)

# 필터: 시총≥2,000억·거래대금≥200억 (ROB-918 spec)
_MIN_MARKET_CAP_KRW = Decimal("200000000000")
_MIN_TRADE_VALUE_KRW = Decimal("20000000000")

_CRASH_INDEX_SYMBOL = "069500"  # KODEX200
_CRASH_INDEX_VENUE = "KRX"
_CRASH_GAP_THRESHOLD_PCT = Decimal("-3.0")

_DEFAULT_TOP_N = 10
_THEME_LEADERS_PER_THEME = 3


def _decimal_to_float(value: Any) -> float | None:
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# Crash guard (AC #5): label only, never blocks candidate generation.
# ---------------------------------------------------------------------------


async def _fetch_index_recent_closes(session: AsyncSession) -> list[DailyCandleRow]:
    repo = DailyCandlesRepository(session=session)
    return await repo.fetch_recent(
        market=MarketKey.KR,
        symbol=_CRASH_INDEX_SYMBOL,
        partition=_CRASH_INDEX_VENUE,
        count=2,
    )


async def _resolve_market_state(session: AsyncSession) -> tuple[str, dict[str, Any]]:
    """전일比 지수 갭으로 crash_warning 라벨만 부여한다 (차단 아님)."""
    try:
        rows = await _fetch_index_recent_closes(session)
    except Exception as exc:  # noqa: BLE001 -- must never break session creation
        logger.warning(
            "new_candidates: index candle fetch failed: %s", exc, exc_info=True
        )
        return "unknown", {
            "reason": "index_candles_fetch_failed",
            "symbol": _CRASH_INDEX_SYMBOL,
        }

    if len(rows) < 2:
        return "unknown", {
            "reason": "index_candles_insufficient",
            "symbol": _CRASH_INDEX_SYMBOL,
        }

    latest, prior = rows[0], rows[1]
    if not prior.close:
        return "unknown", {
            "reason": "index_prior_close_zero",
            "symbol": _CRASH_INDEX_SYMBOL,
        }

    gap_pct = (
        (Decimal(str(latest.close)) - Decimal(str(prior.close)))
        / Decimal(str(prior.close))
        * 100
    )
    detail = {
        "symbol": _CRASH_INDEX_SYMBOL,
        "venue": _CRASH_INDEX_VENUE,
        "gap_pct": float(gap_pct),
        "latest_close": latest.close,
        "latest_time": latest.time_utc.isoformat(),
        "prior_close": prior.close,
        "prior_time": prior.time_utc.isoformat(),
    }
    if gap_pct <= _CRASH_GAP_THRESHOLD_PCT:
        return "crash_warning", detail
    return "normal", detail


# ---------------------------------------------------------------------------
# 전일 consecutive_gainers 상위 (시총/거래대금 필터, change_rate 상위 N)
# ---------------------------------------------------------------------------


async def _build_consecutive_gainers_candidates(
    session: AsyncSession, *, top_n: int, omitted: list[dict[str, str]]
) -> list[dict[str, Any]]:
    try:
        hp = await resolve_healthy_partition(
            session,
            model=InvestScreenerSnapshot,
            date_col=InvestScreenerSnapshot.snapshot_date,
            market_col=InvestScreenerSnapshot.market,
            market="kr",
        )
        if hp is None:
            omitted.append(
                {"section": "consecutive_gainers", "reason": "snapshot_missing"}
            )
            return []
        snapshot_date = hp.partition_date

        stmt = (
            sa.select(InvestScreenerSnapshot)
            .where(
                InvestScreenerSnapshot.market == "kr",
                InvestScreenerSnapshot.snapshot_date == snapshot_date,
            )
            .order_by(
                InvestScreenerSnapshot.change_rate.desc().nullslast(),
                InvestScreenerSnapshot.symbol.asc(),
            )
            .limit(max(top_n * 10, top_n + 100))
        )
        snaps = (await session.execute(stmt)).scalars().all()
        if not snaps:
            omitted.append(
                {
                    "section": "consecutive_gainers",
                    "reason": "healthy_partition_no_rows",
                }
            )
            return []

        symbols = [s.symbol for s in snaps]

        val_hp = await resolve_healthy_partition(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
            row_filter=metric_rich_filter(),
        )
        market_cap_map: dict[str, Decimal] = {}
        if val_hp is not None:
            rows = (
                await session.execute(
                    sa.select(
                        MarketValuationSnapshot.symbol,
                        MarketValuationSnapshot.market_cap,
                    ).where(
                        MarketValuationSnapshot.market == "kr",
                        MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                        MarketValuationSnapshot.symbol.in_(symbols),
                    )
                )
            ).all()
            market_cap_map = {
                r.symbol: r.market_cap for r in rows if r.market_cap is not None
            }
        else:
            omitted.append(
                {
                    "section": "consecutive_gainers_market_cap",
                    "reason": "valuation_snapshot_missing",
                }
            )

        name_map: dict[str, str] = {}
        if symbols:
            name_rows = (
                await session.execute(
                    sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                        KRSymbolUniverse.symbol.in_(symbols)
                    )
                )
            ).all()
            name_map = {r.symbol: r.name for r in name_rows}

        candidates: list[dict[str, Any]] = []
        for snap in snaps:
            market_cap = market_cap_map.get(snap.symbol)
            if market_cap is None or market_cap < _MIN_MARKET_CAP_KRW:
                continue
            trade_value_est: Decimal | None = None
            if snap.daily_volume is not None and snap.latest_close is not None:
                trade_value_est = Decimal(snap.daily_volume) * snap.latest_close
            if trade_value_est is None or trade_value_est < _MIN_TRADE_VALUE_KRW:
                continue

            candidates.append(
                {
                    "symbol": snap.symbol,
                    "name": name_map.get(snap.symbol),
                    "reason": "consecutive_gainers",
                    "advisory_only": True,
                    "selection_rationale": (
                        f"전일({snap.snapshot_date.isoformat()}) "
                        f"{_decimal_to_float(snap.change_rate)}% 상승, "
                        f"연속상승 {snap.consecutive_up_days}일, "
                        "시총≥2,000억·거래대금≥200억 필터 통과"
                    ),
                    "metrics": {
                        "change_rate": _decimal_to_float(snap.change_rate),
                        "consecutive_up_days": snap.consecutive_up_days,
                        "week_change_rate": _decimal_to_float(snap.week_change_rate),
                        "market_cap": _decimal_to_float(market_cap),
                        "trade_value_est": _decimal_to_float(trade_value_est),
                    },
                    "baseline_date": snap.snapshot_date.isoformat(),
                    "baseline_close": _decimal_to_float(snap.latest_close),
                    "outcome": {"d1_close_pct": None},
                }
            )
            if len(candidates) >= top_n:
                break
        return candidates
    except Exception as exc:  # noqa: BLE001 -- must never break session creation
        logger.warning(
            "new_candidates: consecutive_gainers build failed: %s", exc, exc_info=True
        )
        omitted.append({"section": "consecutive_gainers", "reason": "query_failed"})
        return []


# ---------------------------------------------------------------------------
# 전일 마감 테마 상위 (leader_symbols 플랫튼)
# ---------------------------------------------------------------------------


async def _build_theme_leader_candidates(
    session: AsyncSession, *, top_n: int, omitted: list[dict[str, str]]
) -> list[dict[str, Any]]:
    try:
        repo = InvestMomentumEventSnapshotsRepository(session)
        rows = await repo.list_theme_events(event_kind="theme", limit=top_n)
        if not rows:
            omitted.append(
                {"section": "theme_leaders", "reason": "no_naver_theme_snapshots"}
            )
            return []

        stocks_by_theme = await repo.list_theme_event_stocks([row.id for row in rows])

        candidates: list[dict[str, Any]] = []
        for row in rows:
            leaders = (row.leader_symbols or [])[:_THEME_LEADERS_PER_THEME]
            stock_price_map = {
                s.symbol: s.price for s in stocks_by_theme.get(row.id, [])
            }
            for leader in leaders:
                symbol = leader.get("symbol") if isinstance(leader, dict) else None
                if not symbol:
                    continue
                baseline_close = stock_price_map.get(symbol)
                candidates.append(
                    {
                        "symbol": symbol,
                        "name": (
                            leader.get("name") if isinstance(leader, dict) else None
                        ),
                        "reason": "theme_leader",
                        "advisory_only": True,
                        "selection_rationale": (
                            f"전일 마감 테마 '{row.name}' 상위(rank={row.rank}) 주도주"
                        ),
                        "metrics": {
                            "theme_name": row.name,
                            "theme_rank": row.rank,
                            "theme_change_rate": _decimal_to_float(row.change_rate),
                            "theme_trade_value": _decimal_to_float(row.trade_value),
                            "theme_market_cap": _decimal_to_float(row.market_cap),
                            "theme_stock_count": row.stock_count,
                        },
                        "baseline_date": row.trading_date.isoformat(),
                        "baseline_close": _decimal_to_float(baseline_close),
                        "outcome": {"d1_close_pct": None},
                    }
                )
        return candidates
    except Exception as exc:  # noqa: BLE001 -- must never break session creation
        logger.warning(
            "new_candidates: theme_leaders build failed: %s", exc, exc_info=True
        )
        omitted.append({"section": "theme_leaders", "reason": "query_failed"})
        return []


# ---------------------------------------------------------------------------
# 수급 double_buy 요약
# ---------------------------------------------------------------------------


async def _build_double_buy_candidates(
    session: AsyncSession, *, top_n: int, omitted: list[dict[str, str]]
) -> list[dict[str, Any]]:
    try:
        result = await load_double_buy_from_snapshots(session, market="kr", limit=top_n)
        if result is None:
            omitted.append(
                {"section": "double_buy", "reason": "investor_flow_snapshot_missing"}
            )
            return []

        candidates: list[dict[str, Any]] = []
        for row in result.rows[:top_n]:
            snapshot_date = row.get("snapshot_date")
            candidates.append(
                {
                    "symbol": row["symbol"],
                    "name": row.get("name"),
                    "reason": "double_buy",
                    "advisory_only": True,
                    "selection_rationale": "외국인·기관 순매수 전일比 동시 증가(쌍끌이)",
                    "metrics": {
                        "change_rate": row.get("change_rate"),
                        "foreign_net": row.get("foreign_net"),
                        "institution_net": row.get("institution_net"),
                        "market_cap": row.get("market_cap"),
                    },
                    "baseline_date": (
                        snapshot_date.isoformat()
                        if isinstance(snapshot_date, dt.date)
                        else None
                    ),
                    "baseline_close": row.get("close"),
                    "outcome": {"d1_close_pct": None},
                }
            )
        return candidates
    except Exception as exc:  # noqa: BLE001 -- must never break session creation
        logger.warning(
            "new_candidates: double_buy build failed: %s", exc, exc_info=True
        )
        omitted.append({"section": "double_buy", "reason": "query_failed"})
        return []


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def build_new_candidate_section(
    *,
    market_scope: str,
    stage: str,
    top_n: int = _DEFAULT_TOP_N,
) -> dict[str, Any] | None:
    """Advisory-only new-candidate observation block for kr-preopen sessions.

    Returns None for any market/stage other than kr-preopen (the shadow
    rollout is scoped there only, per ROB-918). Opens its own DB session so a
    read failure here can never poison a caller's write transaction, and
    never creates ``trading_decision_proposals`` rows.
    """
    if market_scope != "kr" or stage != "preopen":
        return None

    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        omitted: list[dict[str, str]] = []
        market_state, market_state_detail = await _resolve_market_state(session)
        consecutive_gainers = await _build_consecutive_gainers_candidates(
            session, top_n=top_n, omitted=omitted
        )
        theme_leaders = await _build_theme_leader_candidates(
            session, top_n=top_n, omitted=omitted
        )
        double_buy = await _build_double_buy_candidates(
            session, top_n=top_n, omitted=omitted
        )

    return {
        "advisory_only": True,
        "market_state": market_state,
        "market_state_detail": market_state_detail,
        "consecutive_gainers": consecutive_gainers,
        "theme_leaders": theme_leaders,
        "double_buy": double_buy,
        "omitted_sections": omitted,
    }


__all__ = [
    "build_new_candidate_section",
]
