from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.core.db import AsyncSessionLocal
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_screener_snapshots.freshness import (
    classify_momentum_freshness,
    expected_kr_baseline_date,
)

_ET = ZoneInfo("America/New_York")
_US_SOURCES = ("kis_overseas_ranking", "alpaca_screener", "benzinga_pro")
_US_SESSIONS = ("us_pre", "us_regular", "us_post")
_US_SIDES = ("up", "down", "active")


def _candidate_to_dict(candidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "name": candidate.name,
        "score": candidate.score,
        "latest_snapshot_at": candidate.latest_snapshot_at.isoformat(),
        "trading_date": candidate.trading_date.isoformat(),
        "price": float(candidate.price) if candidate.price is not None else None,
        "change_rate": float(candidate.change_rate)
        if candidate.change_rate is not None
        else None,
        "surface_count": candidate.surface_count,
        "venue_count": candidate.venue_count,
        "rank_delta": candidate.rank_delta,
        "signals": [
            {
                **signal,
                "changeRate": float(signal["changeRate"])
                if signal.get("changeRate") is not None
                else None,
                "tradeValue": float(signal["tradeValue"])
                if signal.get("tradeValue") is not None
                else None,
            }
            for signal in candidate.signals
        ],
        "theme_names": candidate.theme_names,
        "reason_codes": candidate.reason_codes,
    }


async def get_momentum_candidates_impl(
    market: str = "kr",
    date: str | None = None,
    limit: int = 20,
    session: Literal["us_pre", "us_regular", "us_post"] | None = None,
    side: Literal["up", "down", "active"] | None = None,
    source: Literal["kis_overseas_ranking", "alpaca_screener", "benzinga_pro"]
    | None = None,
    min_dollar_volume: float | None = None,
) -> dict[str, Any]:
    """Return read-only persisted KR candidates or US intraday observations."""
    limit = max(1, min(int(limit or 20), 50))
    if market == "us":
        return await _get_us_momentum_candidates(
            date=date,
            limit=limit,
            session=session,
            side=side,
            source=source,
            min_dollar_volume=min_dollar_volume,
        )
    if market != "kr":
        return {
            "market": market,
            "data_state": "unsupported",
            "empty_reason": "naver_stock_supports_kr_only",
            "items": [],
        }

    snapshot_date = dt.date.fromisoformat(date) if date else None
    async with AsyncSessionLocal() as session:
        rows = await InvestMomentumEventSnapshotsRepository(
            session
        ).list_candidate_signals(
            trading_date=snapshot_date,
            limit=limit,
        )
    now = dt.datetime.now(dt.UTC)
    if rows:
        latest_trading_date = rows[0].trading_date
        data_state, days_stale = classify_momentum_freshness(
            latest_trading_date=latest_trading_date, now=now
        )
        empty_reason = None
    else:
        latest_trading_date = None
        data_state, days_stale = "missing", 0
        empty_reason = "no_naver_momentum_snapshots"

    return {
        "market": "kr",
        "data_state": data_state,
        "days_stale": days_stale,
        "expected_baseline_date": expected_kr_baseline_date(now).isoformat(),
        "latest_trading_date": (
            latest_trading_date.isoformat() if latest_trading_date else None
        ),
        "empty_reason": empty_reason,
        "items": [_candidate_to_dict(row) for row in rows],
        "scoring_notes": [
            "searchTop/quantTop/up/priceTop 동시 출현을 우대",
            "KRX+NXT 동시 출현과 테마 리더 편입을 보너스로 반영",
            "동일 surface의 직전 스냅샷 대비 순위 개선(rank_delta)을 반영",
            "read-only: 네이버/브로커 요청 없이 저장된 스냅샷만 조회",
        ],
    }


def _us_surface_parts(surface: str) -> tuple[str, str, str] | None:
    parts = surface.split(":")
    if (
        len(parts) == 4
        and parts[0] == "us"
        and parts[1] in _US_SOURCES
        and parts[2] in _US_SESSIONS
        and parts[3] in _US_SIDES
    ):
        return parts[1], parts[2], parts[3]
    return None


def _us_collection_statuses(
    run_rows: list[Any],
    *,
    sessions: tuple[str, ...],
    sides: tuple[str, ...],
    sources: tuple[str, ...],
) -> list[dict[str, Any]]:
    latest_by_surface: dict[str, Any] = {}
    for run in run_rows:  # repository orders newest first
        latest_by_surface.setdefault(run.surface, run)

    statuses = []
    for source_name in sources:
        surfaces = []
        for session_name in sessions:
            for side_name in sides:
                surface = f"us:{source_name}:{session_name}:{side_name}"
                run = latest_by_surface.get(surface)
                surfaces.append(
                    {
                        "session": session_name,
                        "side": side_name,
                        "status": run.status if run else "not_collected",
                        "reason": run.reason if run else None,
                        "latest_run_at": (
                            run.snapshot_at.astimezone(dt.UTC).isoformat()
                            if run
                            else None
                        ),
                    }
                )
        statuses.append(
            {
                "source": source_name,
                "source_label": "trial"
                if source_name == "benzinga_pro"
                else "standard",
                "surfaces": surfaces,
            }
        )
    return statuses


async def _get_us_momentum_candidates(
    *,
    date: str | None,
    limit: int,
    session: str | None,
    side: str | None,
    source: str | None,
    min_dollar_volume: float | None,
) -> dict[str, Any]:
    trading_date = dt.date.fromisoformat(date) if date else dt.datetime.now(_ET).date()
    if min_dollar_volume is not None and min_dollar_volume < 0:
        raise ValueError("min_dollar_volume must be non-negative")
    minimum = Decimal(str(min_dollar_volume)) if min_dollar_volume is not None else None
    sessions = (session,) if session else _US_SESSIONS
    sides = (side,) if side else _US_SIDES
    sources = (source,) if source else _US_SOURCES

    async with AsyncSessionLocal() as db_session:
        repository = InvestMomentumEventSnapshotsRepository(db_session)
        rows = await repository.list_us_momentum_events(trading_date=trading_date)
        run_rows = await repository.list_us_momentum_run_statuses(
            trading_date=trading_date
        )

    grouped: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for row in rows:
        parts = _us_surface_parts(row.surface)
        if parts is None:
            continue
        row_source, row_session, row_side = parts
        if (
            row.source != row_source
            or row_source not in sources
            or row_session not in sessions
            or row_side not in sides
            or row.order_type == "excluded"
        ):
            continue
        key = (row_source, row_session, row_side)
        by_surface = grouped.setdefault(row.symbol, {})
        observation = by_surface.get(key)
        captured_at = row.first_received_at or row.snapshot_at
        if observation is None:
            by_surface[key] = {"best": row, "first_captured_at": captured_at}
            continue
        if captured_at < observation["first_captured_at"]:
            observation["first_captured_at"] = captured_at
        previous = observation["best"]
        if (
            row.rank,
            captured_at,
            row.snapshot_at,
        ) < (
            previous.rank,
            previous.first_received_at or previous.snapshot_at,
            previous.snapshot_at,
        ):
            observation["best"] = row

    candidates = []
    for symbol, observations in grouped.items():
        rankings = []
        for (row_source, row_session, row_side), observation in observations.items():
            row = observation["best"]
            dollar_volume = row.trade_value
            rankings.append(
                {
                    "source": row_source,
                    "source_label": "trial"
                    if row_source == "benzinga_pro"
                    else "standard",
                    "session": row_session,
                    "side": row_side,
                    "best_rank": row.rank,
                    "change_rate": (
                        float(row.change_rate) if row.change_rate is not None else None
                    ),
                    "volume": row.volume,
                    "dollar_volume": (
                        str(dollar_volume) if dollar_volume is not None else None
                    ),
                    "asset_type": row.asset_type,
                    "first_captured_at": (
                        observation["first_captured_at"].astimezone(dt.UTC).isoformat()
                    ),
                }
            )
        if minimum is not None and not any(
            item["dollar_volume"] is not None
            and Decimal(item["dollar_volume"]) >= minimum
            for item in rankings
        ):
            continue
        rankings.sort(
            key=lambda item: (
                item["best_rank"],
                item["source"],
                item["session"],
                item["side"],
            )
        )
        source_names = sorted({item["source"] for item in rankings})
        candidates.append(
            {
                "symbol": symbol,
                "best_rank": min(item["best_rank"] for item in rankings),
                "sources": source_names,
                "rankings": rankings,
            }
        )
    candidates.sort(key=lambda item: (item["best_rank"], item["symbol"]))

    return {
        "market": "us",
        "trading_date": trading_date.isoformat(),
        "items": candidates[:limit],
        "source_statuses": _us_collection_statuses(
            run_rows,
            sessions=sessions,
            sides=sides,
            sources=sources,
        ),
    }
