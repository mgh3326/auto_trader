"""Read-only loader for the 지지선 근접 (support_proximity) preset (ROB-976).

Ranks the quality-filtered KR universe by distance to the nearest support
level. The clustering itself (Fibonacci / volume-profile / Bollinger) is
reused from ``get_support_resistance_impl`` — never reimplemented.

Two-stage funnel (ROB-976 verify R1 fix):

1. **스냅샷 기반 (zero live calls)** — the latest invest_screener_snapshots
   partition supplies price/quality-filter inputs (market cap / turnover /
   common-stock / active-universe membership) AND a cheap proximity proxy
   computed purely from the persisted ``closes_window`` (a Bollinger lower
   band needs only closes — reused from ``_calculate_bollinger``, not
   reimplemented). Every quality-filtered candidate is ranked by this proxy.
2. **상위 후보만 실시간 재검증** — only the top-N candidates by that proxy
   (bounded, ``candidate_pool_limit``) get a LIVE ``get_support_resistance_impl``
   fan-out (fib/volume-profile/Bollinger on fresh OHLCV). For a candidate that
   clears the live check, price/support/distance are ALL taken from that same
   live call — never a mix of a stale snapshot price with a freshly computed
   distance (the mixed-timestamp bug caught in verify R1).

No new table/column is introduced and no write path exists here —
``InvestScreenerSnapshotsRepository.upsert`` remains the sole writer (this
loader never calls it). ``scripts/build_support_proximity_snapshot.py`` is the
bounded, read-only CLI entry point for the "야간 스냅샷 배치" step (there is
no separate persisted artifact to commit — see that script's docstring).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.invest_view_model.screener_service import _SnapshotLoadResult

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

# ROB-976: internal quality-filter floors. These bound the live
# get_support_resistance fan-out to blue-chip-ish names — they are NOT the
# caller-tunable knob (screen_stocks_snapshot's generic min_market_cap /
# min_market_cap_eok params narrow further at the result layer, same as every
# other preset).
DEFAULT_MIN_MARKET_CAP_KRW = 300_000_000_000.0  # 3천억원
DEFAULT_MIN_TURNOVER_KRW = 1_000_000_000.0  # 10억원 (daily_volume * latest_close)
# ROB-976 verify R1: bounded to the *result size* (not a flat oversized pool) —
# stage 1 (snapshot-only Bollinger proxy) picks who is worth the live re-check,
# so the live fan-out no longer needs a large flat cap to find good candidates.
DEFAULT_CANDIDATE_POOL_LIMIT = 30
_LIVE_CHECK_CONCURRENCY = 6
_BOLLINGER_PERIOD = 20


def _proxy_distance_pct(
    closes_window: list[float] | None, latest_close: float
) -> float:
    """Cheap, snapshot-only (zero live calls) proximity proxy: distance from
    the latest close to a Bollinger lower band computed purely from the
    persisted closes_window. Reuses _calculate_bollinger — not reimplemented.
    Returns +inf when there isn't enough history for a period-20 band (ranks
    last, never crashes, never fabricates a level)."""
    from app.mcp_server.tooling.market_data_indicators import _calculate_bollinger

    if not closes_window or latest_close <= 0:
        return float("inf")
    bb = _calculate_bollinger(pd.Series([float(c) for c in closes_window]))
    lower = bb.get("lower")
    if lower is None or lower <= 0:
        return float("inf")
    return abs((latest_close - float(lower)) / latest_close * 100)


async def load_support_proximity_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 30,
    min_market_cap: float | None = None,
    min_turnover: float | None = None,
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT,
    now: Callable[[], datetime] | None = None,
) -> _SnapshotLoadResult | None:
    """Return support_proximity rows ordered by dist_to_support_pct ascending.

    None  -> no snapshot partition exists; caller should report dataState=missing.
    []    -> a partition exists but nothing passed the quality filters, or every
             checked candidate had no support below its current price (fail-closed,
             never fabricated).
    Rows  -> ordered by dist_to_support_pct asc (closest support first). Every
             field on a row (price/support/distance) comes from the SAME live
             get_support_resistance call — never a snapshot price paired with a
             live-computed distance (ROB-976 verify R1).
    """
    if session is None or market != "kr":
        return None

    now_fn = now or (lambda: datetime.now(UTC))

    from app.services.invest_screener_snapshots.partition_health import (
        active_universe_count,
        resolve_healthy_partition,
    )
    from app.services.invest_view_model.screener_service import (
        _is_toss_common_stock_row,
        _partition_degradation,
        _SnapshotLoadResult,
    )

    universe_count = await active_universe_count(session, market="kr")
    price_hp = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    if price_hp is None:
        return None
    price_date = price_hp.partition_date

    floor_market_cap = (
        float(min_market_cap)
        if min_market_cap is not None
        else DEFAULT_MIN_MARKET_CAP_KRW
    )
    floor_turnover = (
        float(min_turnover) if min_turnover is not None else DEFAULT_MIN_TURNOVER_KRW
    )

    try:
        price_result = await session.execute(
            sa.select(
                InvestScreenerSnapshot.symbol,
                InvestScreenerSnapshot.latest_close,
                InvestScreenerSnapshot.prev_close,
                InvestScreenerSnapshot.change_rate,
                InvestScreenerSnapshot.change_amount,
                InvestScreenerSnapshot.daily_volume,
                InvestScreenerSnapshot.closes_window,
            ).where(
                InvestScreenerSnapshot.market == "kr",
                InvestScreenerSnapshot.snapshot_date == price_date,
                InvestScreenerSnapshot.latest_close.is_not(None),
            )
        )
        price_rows = list(price_result.mappings().all())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "support_proximity: candidate query failed: %s", exc, exc_info=True
        )
        return None

    if not price_rows:
        reason, coverage_label = _partition_degradation(price_hp, rows_empty=True)
        return _SnapshotLoadResult(
            rows=[],
            partition_date=price_date,
            partition_computed_at=None,
            degradation_reason=reason,
            coverage_label=coverage_label,
        )

    symbols = [r["symbol"] for r in price_rows]

    from app.models.symbol_sectors import SymbolSector

    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    meta_map: dict[str, dict[str, Any]] = {}
    try:
        names = await session.execute(
            sa.select(
                KRSymbolUniverse.symbol,
                KRSymbolUniverse.name,
                SymbolSector.name_kr.label("sector_name_kr"),
                SymbolSector.name_en.label("sector_name_en"),
                KRSymbolUniverse.security_type,
                KRSymbolUniverse.is_common_share,
                KRSymbolUniverse.krx_trading_suspended,
                KRSymbolUniverse.nxt_trading_suspended,
            )
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        _name_rows = names.all()
        # ROB-976 verify R1: name_map/meta_map membership IS the active-universe
        # gate below (symbol not present -> excluded). Do not fall back to the
        # permissive "unknown name -> allow" heuristic here — that heuristic
        # exists for callers that only have a name with no universe row at all,
        # not for "this symbol isn't in the active universe query result".
        name_map = {row.symbol: row.name for row in _name_rows}
        sector_map = {
            row.symbol: label
            for row in _name_rows
            if (
                label := (
                    getattr(row, "sector_name_kr", None)
                    or getattr(row, "sector_name_en", None)
                )
            )
        }
        meta_map = {
            row.symbol: {
                "security_type": getattr(row, "security_type", None),
                "is_common_share": getattr(row, "is_common_share", None),
                "krx_trading_suspended": getattr(row, "krx_trading_suspended", None),
                "nxt_trading_suspended": getattr(row, "nxt_trading_suspended", None),
            }
            for row in _name_rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("support_proximity: name lookup failed: %s", exc, exc_info=True)

    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition as _resolve_val_hp,
    )
    from app.services.market_valuation_snapshots.repository import metric_rich_filter

    val_hp = await _resolve_val_hp(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="kr",
        universe_count=universe_count,
        row_filter=metric_rich_filter(),  # ROB-551: skip toss-only partitions
    )
    if val_hp is not None:
        market_cap_source = "fallback" if val_hp.is_fallback else "primary"
        try:
            _mc = await session.execute(
                sa.select(
                    MarketValuationSnapshot.symbol,
                    MarketValuationSnapshot.market_cap,
                ).where(
                    MarketValuationSnapshot.market == "kr",
                    MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                    MarketValuationSnapshot.symbol.in_(symbols),
                )
            )
            market_cap_map = {
                r.symbol: float(r.market_cap)
                for r in _mc.all()
                if r.market_cap is not None
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "support_proximity: market_cap lookup failed: %s", exc, exc_info=True
            )

    candidates: list[dict[str, Any]] = []
    for r in price_rows:
        sym = r["symbol"]
        # ROB-976 verify R1 [HIGH]: require ACTIVE universe membership (not just
        # an optional name label) — a symbol absent from name_map (e.g. active
        # universe is empty/stale, or the symbol was delisted) must be excluded,
        # never fall through on the permissive no-name heuristic.
        if sym not in name_map:
            continue
        name = name_map[sym]
        meta = meta_map.get(sym, {})
        if not _is_toss_common_stock_row(
            symbol=sym,
            name=name,
            security_type=meta.get("security_type"),
            is_common_share=meta.get("is_common_share"),
            trading_suspended=meta.get("krx_trading_suspended")
            or meta.get("nxt_trading_suspended"),
        ):
            continue
        market_cap = market_cap_map.get(sym)
        if market_cap is None or market_cap < floor_market_cap:
            continue
        close = float(r["latest_close"])
        volume = r["daily_volume"]
        turnover = close * float(volume) if volume is not None else None
        if turnover is None or turnover < floor_turnover:
            continue
        candidates.append(
            {
                "symbol": sym,
                "name": name,
                "sector": sector_map.get(sym),
                "latest_close": close,
                "prev_close": (
                    float(r["prev_close"]) if r["prev_close"] is not None else None
                ),
                "change_rate": (
                    float(r["change_rate"]) if r["change_rate"] is not None else None
                ),
                "change_amount": (
                    float(r["change_amount"])
                    if r["change_amount"] is not None
                    else None
                ),
                "daily_volume": volume,
                "market_cap": market_cap,
                "closes_window": r["closes_window"],
            }
        )

    if not candidates:
        reason, coverage_label = _partition_degradation(price_hp, rows_empty=True)
        return _SnapshotLoadResult(
            rows=[],
            partition_date=price_date,
            partition_computed_at=None,
            degradation_reason=reason,
            coverage_label=coverage_label,
        )

    # Stage 1 (스냅샷 기반, zero live calls): rank every quality-filtered
    # candidate by a cheap Bollinger-lower-band proxy computed purely from the
    # persisted closes_window. Stage 2 below only live-verifies the top of
    # THIS ranking — "상위 후보만 실시간 재검증", not "top by market cap".
    for c in candidates:
        c["_proxy_distance_pct"] = _proxy_distance_pct(
            c["closes_window"], c["latest_close"]
        )
    candidates.sort(key=lambda c: c["_proxy_distance_pct"])
    bounded_candidates = candidates[: max(candidate_pool_limit, limit)]

    from app.mcp_server.tooling.fundamentals._support_resistance import (
        get_support_resistance_impl,
    )

    semaphore = asyncio.Semaphore(_LIVE_CHECK_CONCURRENCY)

    async def _check(symbol: str) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await get_support_resistance_impl(symbol, market="kr")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "support_proximity: get_support_resistance failed for %s: %s",
                    symbol,
                    exc,
                    exc_info=True,
                )
                return None

    verified_at = now_fn()
    sr_results = await asyncio.gather(
        *(_check(c["symbol"]) for c in bounded_candidates)
    )

    # Stage 2 (상위 후보만 실시간 재검증): price/support/distance below are ALL
    # read from the SAME live `sr` payload — never mixed with the stage-1
    # snapshot price (ROB-976 verify R1 [BLOCKER] fix). The snapshot's
    # prev_close (yesterday's settled close, not time-sensitive intraday)
    # anchors change_amount/change_rate against the live price.
    rows: list[dict[str, Any]] = []
    no_support_count = 0
    for candidate, sr in zip(bounded_candidates, sr_results, strict=True):
        if sr is None or sr.get("error"):
            continue
        live_price = sr.get("current_price")
        if live_price is None or live_price <= 0:
            continue
        supports = sr.get("supports") or []
        if not supports:
            # ROB-976: no clustered level below current price is a real, expected
            # outcome (e.g. a fresh 52-week high has nothing to anchor below it),
            # never a crash. Excluded from the ranked list, not fabricated.
            no_support_count += 1
            continue
        nearest = supports[0]
        distance_pct = abs(float(nearest.get("distance_pct") or 0.0))
        live_price = float(live_price)
        prev_close = candidate["prev_close"]
        change_amount = (live_price - prev_close) if prev_close is not None else None
        change_rate = (
            round(change_amount / prev_close * 100, 2)
            if change_amount is not None and prev_close
            else None
        )
        rows.append(
            {
                "symbol": candidate["symbol"],
                "market": "kr",
                "name": candidate["name"],
                "sector": candidate["sector"],
                "close": live_price,
                "latest_close": live_price,
                "prev_close": prev_close,
                "change_rate": change_rate,
                "change_amount": change_amount,
                "volume": candidate["daily_volume"],
                "market_cap": candidate["market_cap"],
                "_market_cap_source": market_cap_source,
                "dist_to_support_pct": round(distance_pct, 2),
                "support_price": nearest.get("price"),
                "support_kind": ",".join(nearest.get("sources") or []) or None,
                "support_strength": nearest.get("strength"),
                "snapshot_date": price_date,
                "computed_at": verified_at,
                # ROB-976 verify R1: every field above is a live read taken at
                # verified_at, so "fresh" is now literally true of this row's
                # content — not a guess derived from partition coverage.
                "_screener_snapshot_state": "fresh",
            }
        )

    rows.sort(key=lambda row: row["dist_to_support_pct"])
    rows = rows[:limit]

    reason, coverage_label = _partition_degradation(price_hp, rows_empty=not rows)
    if reason is None and not rows and no_support_count:
        # Every checked candidate had no support below price this round — an
        # honest "no qualifiers", not a partition problem.
        reason = "healthy_no_matches"

    return _SnapshotLoadResult(
        rows=rows,
        partition_date=price_date,
        partition_computed_at=verified_at,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
