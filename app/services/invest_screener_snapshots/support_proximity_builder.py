"""Build persisted KR support-proximity screener snapshots.

Candidate discovery may use the latest ordinary screener partition as a cheap,
zero-network proxy.  The final price, change metrics, support level, and support
distance are then derived from one completed OHLCV frame and persisted together.
Read paths never invoke this module or recalculate a support level.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.market_data_indicators import (
    _calculate_bollinger,
    _fetch_ohlcv_for_indicators,
)
from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_screener_snapshots.builder import (
    _coerce_snapshot_date,
    derive_metrics,
)
from app.services.invest_screener_snapshots.freshness import expected_baseline_date
from app.services.invest_screener_snapshots.repository import SnapshotUpsert
from app.services.invest_screener_snapshots.support_proximity_policy import (
    DEFAULT_CANDIDATE_POOL_LIMIT,
    DEFAULT_CONCURRENCY,
    DEFAULT_MIN_MARKET_CAP_KRW,
    DEFAULT_MIN_TURNOVER_KRW,
    MAX_CANDIDATE_POOL_LIMIT,
)
from app.services.market_valuation_snapshots.normalized_market_cap import (
    NormalizedMarketCap,
    load_normalized_kr_market_caps,
)

logger = logging.getLogger(__name__)

_OHLCV_LOOKBACK = 60


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class SupportProximityCandidate:
    symbol: str
    market_cap: NormalizedMarketCap
    proxy_distance_pct: float


@dataclass(frozen=True)
class SupportProximityBuildBatch:
    source_partition_date: dt.date | None
    candidates: tuple[SupportProximityCandidate, ...]
    payloads: tuple[SnapshotUpsert, ...]

    @property
    def support_count(self) -> int:
        return sum(row.dist_to_support_pct is not None for row in self.payloads)


def snapshot_proxy_distance_pct(
    closes_window: list[Any] | None,
    latest_close: Decimal | float,
) -> float:
    """Bollinger-lower proximity used only to bound the expensive build fan-out."""

    close = float(latest_close)
    if not closes_window or close <= 0:
        return float("inf")
    try:
        series = pd.Series([float(value) for value in closes_window])
        lower = _calculate_bollinger(series).get("lower")
    except (TypeError, ValueError):
        return float("inf")
    if lower is None or float(lower) <= 0:
        return float("inf")
    return abs((close - float(lower)) / close * 100)


async def resolve_support_proximity_candidates(
    session: AsyncSession,
    *,
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT,
    min_market_cap: Decimal = DEFAULT_MIN_MARKET_CAP_KRW,
    min_turnover: Decimal = DEFAULT_MIN_TURNOVER_KRW,
) -> tuple[dt.date | None, list[SupportProximityCandidate]]:
    """Resolve a bounded, active/common-stock KR candidate pool.

    The market-cap gate uses only Naver-normalized ``market_valuation_snapshots``
    values.  The latest ordinary screener partition supplies a cheap proximity
    proxy and pre-build turnover estimate; neither value is copied into the final
    support snapshot.
    """

    if not 1 <= candidate_pool_limit <= MAX_CANDIDATE_POOL_LIMIT:
        raise ValueError(
            f"candidate_pool_limit must be between 1 and {MAX_CANDIDATE_POOL_LIMIT}"
        )

    from app.services.invest_screener_snapshots.partition_health import (
        active_universe_count,
        resolve_healthy_partition,
    )

    universe_count = await active_universe_count(session, market="kr")
    base_partition = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    if base_partition is None:
        return None, []
    partition_date = base_partition.partition_date

    rows_result = await session.execute(
        sa.select(
            InvestScreenerSnapshot.symbol,
            InvestScreenerSnapshot.latest_close,
            InvestScreenerSnapshot.daily_volume,
            InvestScreenerSnapshot.daily_turnover,
            InvestScreenerSnapshot.closes_window,
            KRSymbolUniverse.name,
            KRSymbolUniverse.security_type,
            KRSymbolUniverse.is_common_share,
            KRSymbolUniverse.krx_trading_suspended,
            KRSymbolUniverse.nxt_trading_suspended,
        )
        .join(
            KRSymbolUniverse,
            KRSymbolUniverse.symbol == InvestScreenerSnapshot.symbol,
        )
        .where(
            InvestScreenerSnapshot.market == "kr",
            InvestScreenerSnapshot.snapshot_date == partition_date,
            InvestScreenerSnapshot.latest_close > 0,
            KRSymbolUniverse.is_active.is_(True),
        )
    )
    rows = list(rows_result.mappings().all())
    if not rows:
        return partition_date, []

    from app.services.invest_view_model.screener_service import (
        _is_toss_common_stock_row,
    )

    eligible_rows: list[Any] = []
    for row in rows:
        if not _is_toss_common_stock_row(
            symbol=row["symbol"],
            name=row["name"],
            security_type=row["security_type"],
            is_common_share=row["is_common_share"],
            trading_suspended=(
                row["krx_trading_suspended"] or row["nxt_trading_suspended"]
            ),
        ):
            continue
        eligible_rows.append(row)

    caps = await load_normalized_kr_market_caps(
        session, (row["symbol"] for row in eligible_rows)
    )

    candidates: list[SupportProximityCandidate] = []
    for row in eligible_rows:
        cap = caps.get(row["symbol"])
        if cap is None or cap.value < min_market_cap:
            continue
        turnover = row["daily_turnover"]
        if turnover is None and row["daily_volume"] is not None:
            turnover = Decimal(row["latest_close"]) * Decimal(row["daily_volume"])
        if turnover is None or Decimal(turnover) < min_turnover:
            continue
        candidates.append(
            SupportProximityCandidate(
                symbol=row["symbol"],
                market_cap=cap,
                proxy_distance_pct=snapshot_proxy_distance_pct(
                    row["closes_window"], row["latest_close"]
                ),
            )
        )

    candidates.sort(key=lambda row: (row.proxy_distance_pct, row.symbol))
    return partition_date, candidates[:candidate_pool_limit]


def _completed_ohlcv(
    frame: pd.DataFrame,
    *,
    fallback_date: dt.date,
    now: dt.datetime,
) -> pd.DataFrame:
    result = frame.copy()
    if "date" in result.columns:
        result = result.sort_values("date").reset_index(drop=True)
        completed_through = expected_baseline_date("kr", now=now)
        result = result[
            result["date"].map(
                lambda value: (
                    _coerce_snapshot_date(value, fallback_date) <= completed_through
                )
            )
        ].reset_index(drop=True)
    return result


def _nearest_support(
    supports: list[dict[str, Any]], *, current_price: Decimal
) -> tuple[Decimal, str | None, str | None, Decimal] | None:
    usable: list[tuple[Decimal, dict[str, Any]]] = []
    for level in supports:
        try:
            price = Decimal(str(level.get("price")))
        except Exception:  # noqa: BLE001 - provider-shaped level payload
            continue
        if price <= 0 or price > current_price:
            continue
        usable.append((price, level))
    if not usable:
        return None

    price, level = min(usable, key=lambda item: current_price - item[0])
    distance = (current_price - price) / current_price * Decimal("100")
    sources = level.get("sources") or []
    if isinstance(sources, str):
        kind = sources
    else:
        kind = ",".join(str(source) for source in sources if source)
    kind = kind[:255] or None
    strength = str(level.get("strength") or "").strip()[:20] or None
    return price, kind, strength, distance.quantize(Decimal("0.0001"))


async def build_support_proximity_snapshot_for_candidate(
    candidate: SupportProximityCandidate,
    *,
    now: dt.datetime,
    min_turnover: Decimal = DEFAULT_MIN_TURNOVER_KRW,
    clock: Callable[[], dt.datetime] = _utcnow,
) -> SnapshotUpsert | None:
    """Build one atomic price/support snapshot from a single completed frame."""

    try:
        frame = await _fetch_ohlcv_for_indicators(
            candidate.symbol, "equity_kr", count=_OHLCV_LOOKBACK
        )
    except Exception as exc:  # noqa: BLE001 - bounded per-symbol fail-soft build
        logger.warning(
            "support snapshot OHLCV fetch failed %s: %s", candidate.symbol, exc
        )
        return None
    if frame is None or frame.empty:
        return None

    fallback_date = expected_baseline_date("kr", now=now)
    frame = _completed_ohlcv(frame, fallback_date=fallback_date, now=now)
    required = {"high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns):
        return None

    closes = [
        Decimal(str(value))
        for value in frame["close"].tolist()
        if value is not None and not pd.isna(value)
    ]
    if not closes:
        return None
    metrics = derive_metrics(closes)
    snapshot_date = (
        _coerce_snapshot_date(frame["date"].iloc[-1], fallback_date)
        if "date" in frame.columns
        else fallback_date
    )
    volume_value = frame["volume"].iloc[-1] if "volume" in frame.columns else None
    daily_volume = (
        int(volume_value)
        if volume_value is not None and not pd.isna(volume_value)
        else None
    )
    daily_turnover = (
        metrics.latest_close * Decimal(daily_volume)
        if daily_volume is not None
        else None
    )

    support_price: Decimal | None = None
    support_kind: str | None = None
    support_strength: str | None = None
    distance: Decimal | None = None

    # The same completed frame supplies both current_price and every support
    # input.  Passing preloaded_df is the look-ahead boundary: this call cannot
    # fetch a newer candle behind the builder's back.
    if daily_turnover is not None and daily_turnover >= min_turnover:
        from app.mcp_server.tooling.fundamentals._support_resistance import (
            get_support_resistance_impl,
        )

        try:
            support_result = await get_support_resistance_impl(
                candidate.symbol,
                market="kr",
                preloaded_df=frame,
            )
        except Exception as exc:  # noqa: BLE001 - bounded per-symbol fail-soft build
            logger.warning(
                "support snapshot calculation raised %s: %s", candidate.symbol, exc
            )
            return None
        if support_result.get("error"):
            logger.warning(
                "support snapshot calculation failed %s: %s",
                candidate.symbol,
                support_result.get("message") or "unknown error",
            )
            return None
        nearest = _nearest_support(
            list(support_result.get("supports") or []),
            current_price=metrics.latest_close,
        )
        if nearest is not None:
            support_price, support_kind, support_strength, distance = nearest

    return SnapshotUpsert(
        market="kr",
        symbol=candidate.symbol,
        snapshot_date=snapshot_date,
        latest_close=metrics.latest_close,
        prev_close=metrics.prev_close,
        change_amount=metrics.change_amount,
        change_rate=metrics.change_rate,
        consecutive_up_days=metrics.consecutive_up_days,
        week_change_rate=metrics.week_change_rate,
        closes_window=[float(value) for value in closes[-_OHLCV_LOOKBACK:]],
        daily_volume=daily_volume,
        daily_turnover=daily_turnover,
        market_cap=candidate.market_cap.value,
        market_cap_source=candidate.market_cap.source,
        market_cap_snapshot_date=candidate.market_cap.snapshot_date,
        support_price=support_price,
        support_kind=support_kind,
        support_strength=support_strength,
        dist_to_support_pct=distance,
        # Record completion, not fan-out start. Freshness metadata must not
        # claim a calculation finished before its provider work actually did.
        support_computed_at=clock(),
        source="kis",
    )


async def build_support_proximity_snapshots(
    session: AsyncSession,
    *,
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT,
    concurrency: int = DEFAULT_CONCURRENCY,
    min_market_cap: Decimal = DEFAULT_MIN_MARKET_CAP_KRW,
    min_turnover: Decimal = DEFAULT_MIN_TURNOVER_KRW,
    now: dt.datetime | None = None,
) -> SupportProximityBuildBatch:
    """Build a bounded batch without writing it."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    source_partition_date, candidates = await resolve_support_proximity_candidates(
        session,
        candidate_pool_limit=candidate_pool_limit,
        min_market_cap=min_market_cap,
        min_turnover=min_turnover,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(candidate: SupportProximityCandidate) -> SnapshotUpsert | None:
        async with semaphore:
            return await build_support_proximity_snapshot_for_candidate(
                candidate,
                now=moment,
                min_turnover=min_turnover,
            )

    rows = await asyncio.gather(*(_one(candidate) for candidate in candidates))
    return SupportProximityBuildBatch(
        source_partition_date=source_partition_date,
        candidates=tuple(candidates),
        payloads=tuple(row for row in rows if row is not None),
    )
