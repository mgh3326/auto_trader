"""Read-only loader for the KR /invest/screener fundamentals presets, backed by
the tvscreener KR snapshot (``invest_kr_fundamentals_snapshots``, ROB-428 PR-A).

This is the DISPLAY read-path for the 7 ``FUNDAMENTALS_PRESET_SPECS`` presets
(cheap_value, steady_dividend, profitable_company, undervalued_growth,
stable_growth, future_dividend_king, growth_expectation_toss). It replaces the
DART-backed ``load_fundamentals_preset_from_snapshots`` ONLY for KR display;
the DART loader stays in place for reports/PIT (ROB-330) and is untouched.

Honest-divergence notes (ROB-428 spec §"미충족"):

* ``min_earnings_increase_streak_years`` — tvscreener exposes only *dividend*
  streaks, no *net-income* streak. That sub-condition is SKIPPED (never
  fail-closed, never fabricated) and the result surfaces
  :data:`EARNINGS_STREAK_SKIP_WARNING` so the caller can tell the user.
* ``min_revenue_growth_3y_avg`` / ``min_earnings_growth_3y_avg`` — tvscreener
  serves 1-year YoY, not Toss's 3-year average. We apply the YoY column as a
  documented PROXY (comparable, not identical).

Every other active spec threshold is applied fail-closed: a candidate whose
required tvscreener column is NULL is EXCLUDED (never a silent pass).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_screener_snapshots.partition_health import (
    HealthyPartition,
    active_universe_count,
    cap_degraded,
)
from app.services.invest_view_model.fundamentals_screener import (
    FundamentalsPresetSpec,
    FundamentalsScreenResult,
)
from app.services.market_events.session_calendar import trading_sessions_in_range

logger = logging.getLogger(__name__)

EARNINGS_STREAK_SKIP_WARNING = "순이익 연속증가 조건은 tvscreener 미제공으로 미적용"

#: Mirror partition_health._MIN_HEALTHY_COVERAGE_RATIO so a thin tvscreener smoke
#: partition cannot shadow a healthy one. Locked here; change = telemetry-backed PR.
_MIN_HEALTHY_COVERAGE_RATIO = 0.50
_MAX_PARTITION_SCAN_BACK = 10

#: ROB-429 B1: the read-path now evaluates the FULL healthy partition (no market-cap
#: cand_cap that excluded small caps). This is a pathological safety bound only —
#: a KR partition is ~4,250 rows, so hitting it signals a corrupt partition.
_MAX_PARTITION_ROWS = 20_000


# (spec threshold field, snapshot column attr, require_positive) — each non-None
# spec field is applied fail-closed against the tvscreener column. PROXY mappings
# (3y-avg → 1yr YoY) are noted in the module docstring.
_THRESHOLD_CHECKS: tuple[tuple[str, str, bool], ...] = (
    ("min_roe", "roe_ttm", False),
    ("max_per", "per", True),  # also requires per > 0
    ("max_pbr", "pbr", True),  # also requires pbr > 0
    ("min_dividend_yield", "dividend_yield", False),
    ("min_gross_margin_ttm", "gross_margin_ttm", False),
    ("min_payout_ratio", "payout_ratio_ttm", False),
    ("min_revenue_growth_3y_avg", "revenue_yoy", False),  # PROXY: YoY not 3y-avg
    ("min_earnings_growth_3y_avg", "eps_yoy", False),  # PROXY: YoY not 3y-avg
    ("min_earnings_growth_qoq", "eps_qoq", False),
    ("min_dividend_paid_streak_years", "continuous_dividend_payout", False),
    ("min_dividend_growth_streak_years", "continuous_dividend_growth", False),
)

#: ``min_*`` thresholds are lower bounds (column >= threshold); ``max_*`` are
#: upper bounds (column <= threshold).
_MAX_FIELDS = frozenset({"max_per", "max_pbr"})

#: spec.sort_by metric key -> the row dict key carrying that metric. The DART
#: loader carries derive metrics under these same keys, so _METRIC_FIELD,
#: _CARRIED_DERIVE_METRICS, and the ScreenerResultRow renderer all work unchanged.
_SORT_KEY_TO_ROW_KEY: dict[str, str] = {
    "roe": "roe",
    "per": "per",
    "pbr": "pbr",
    "dividend_yield": "dividend_yield",
    "gross_margin_ttm": "gross_margin_ttm",
    "revenue_growth_3y_avg": "revenue_growth_3y_avg",
    "earnings_growth_3y_avg": "earnings_growth_3y_avg",
    "earnings_growth_qoq": "earnings_growth_qoq",
    "payout_ratio": "payout_ratio",
    # ROB-428 PR-C: 52w-high proximity remains an emitted (informational) row key.
    "high_52w_proximity": "high_52w_proximity",
    # market_cap is a valid sort key (generic). NOTE (ROB-432): undervalued_breakout
    # now sorts by PER ascending (Toss 저평가 탈출 default), not market_cap.
    "market_cap": "market_cap",
}


def _high_52w_proximity(snap: InvestKrFundamentalsSnapshot) -> Decimal | None:
    """price / week_high_52, or None when either is missing or week_high_52 <= 0.

    ROB-428 PR-C: this is a *derived* threshold (not a single-column compare), so
    it is checked explicitly in :func:`_passes_thresholds` and emitted in
    :func:`_build_row` rather than via the ``_THRESHOLD_CHECKS`` column tuples.
    """
    price = snap.price
    high = snap.week_high_52
    if price is None or high is None or high <= 0:
        return None
    return Decimal(str(price)) / Decimal(str(high))


def _to_float(value: Any) -> float | None:
    return float(value) if value is not None else None


async def _resolve_kr_partition(
    session: AsyncSession,
    *,
    universe_count: int | None,
) -> HealthyPartition | None:
    """Latest-healthy-partition selection for the market-less KR snapshot table.

    Mirrors ``resolve_healthy_partition`` (coverage ratio, bounded scan-back,
    fail-open, degraded last-resort) but the ``invest_kr_fundamentals_snapshots``
    table is KR-only and has NO market column, so we cannot pass a ``market_col``.
    Returns None only when the table has no partitions.
    """
    date_col = InvestKrFundamentalsSnapshot.snapshot_date
    try:
        dates = [
            d
            for (d,) in (
                await session.execute(
                    sa.select(date_col)
                    .distinct()
                    .order_by(date_col.desc())
                    .limit(_MAX_PARTITION_SCAN_BACK)
                )
            ).all()
        ]
        if not dates:
            return None
        newest = dates[0]

        if universe_count is None:
            universe_count = await active_universe_count(session, market="kr")
        if universe_count <= 0:
            # No coverage denominator → cannot judge health; treat newest as healthy
            # (fail-open, never reduce availability).
            return HealthyPartition(
                partition_date=newest,
                row_count=0,
                coverage_ratio=0.0,
                is_fallback=False,
                healthy=True,
            )

        floor = math.ceil(universe_count * _MIN_HEALTHY_COVERAGE_RATIO)
        for d in dates:
            count = await _partition_row_count(session, partition_date=d)
            if count >= floor:
                return HealthyPartition(
                    partition_date=d,
                    row_count=count,
                    coverage_ratio=count / universe_count,
                    is_fallback=(d != newest),
                    healthy=True,
                )

        newest_count = await _partition_row_count(session, partition_date=newest)
        return HealthyPartition(
            partition_date=newest,
            row_count=newest_count,
            coverage_ratio=newest_count / universe_count,
            is_fallback=False,
            healthy=False,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open, never reduce availability
        logger.warning(
            "kr_fundamentals_tv_screener: partition resolve failed; "
            "falling back to max(): %s",
            exc,
            exc_info=True,
        )
        try:
            newest = (
                await session.execute(sa.select(sa.func.max(date_col)))
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            return None
        if newest is None:
            return None
        return HealthyPartition(
            partition_date=newest,
            row_count=0,
            coverage_ratio=0.0,
            is_fallback=False,
            healthy=True,
        )


async def _partition_row_count(
    session: AsyncSession, *, partition_date: dt.date
) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(InvestKrFundamentalsSnapshot)
                .where(InvestKrFundamentalsSnapshot.snapshot_date == partition_date)
            )
        ).scalar()
        or 0
    )


def _new_high_age_trading_days(
    snap: InvestKrFundamentalsSnapshot, *, partition_date: dt.date
) -> int | None:
    """KRX *trading* sessions from the 52-week-high date to the partition date.

    ROB-432: counts holiday-aware KRX sessions (XKRX via session_calendar) in the
    half-open range ``(week_high_52_date, partition_date]`` — a smaller value = a
    more recent new 52-week high. Toss's "20일" is 20 거래일, so trading days (not
    calendar days) are the correct unit.

    Returns None (fail-closed → excluded) when:
    * week_high_52_date is missing, or in the future relative to the partition
      (a future high-date is treated as unavailable, not recent); or
    * the range is outside the XKRX calendar's precomputed bounds / any calendar
      error (``trading_sessions_in_range`` returns [] → never mis-included).
    """
    high_date = snap.week_high_52_date
    if high_date is None or high_date > partition_date:
        return None
    sessions = trading_sessions_in_range("kr", high_date, partition_date)
    if not sessions:
        # Out of XKRX range or calendar error → cannot confirm recency → fail-closed.
        return None
    return sum(1 for s in sessions if s > high_date)


def _passes_thresholds(
    snap: InvestKrFundamentalsSnapshot,
    spec: FundamentalsPresetSpec,
    *,
    partition_date: dt.date,
) -> tuple[bool, str | None]:
    """Apply the spec's active thresholds against tvscreener columns (fail-closed).

    Returns (passes, reject_reason). ``min_earnings_increase_streak_years`` is
    intentionally NOT in _THRESHOLD_CHECKS — it has no tvscreener column and is
    skipped (the caller surfaces EARNINGS_STREAK_SKIP_WARNING instead).
    """
    for spec_field, col_attr, require_positive in _THRESHOLD_CHECKS:
        threshold = getattr(spec, spec_field)
        if threshold is None:
            continue
        value = getattr(snap, col_attr)
        if value is None:
            return False, f"{col_attr} unavailable"
        if require_positive and value <= 0:
            return False, f"{col_attr} not positive"
        if spec_field in _MAX_FIELDS:
            if Decimal(str(value)) > Decimal(str(threshold)):
                return False, f"{col_attr} above max"
        else:
            if Decimal(str(value)) < Decimal(str(threshold)):
                return False, f"{col_attr} below min"

    # ROB-430 PR-② / ROB-432: 신고가 = a NEW 52-week high made within
    # max_new_high_age_trading_days KRX trading sessions of the partition (a breakout
    # event), checked via week_high_52_date recency. Fail-closed with DISTINCT reasons
    # so an operator can tell a missing-data exclude from a calendar-range one:
    #   NULL date / future date / out-of-XKRX-range each get their own reason.
    if spec.max_new_high_age_trading_days is not None:
        high_date = snap.week_high_52_date
        if high_date is None:
            return False, "52w-high date unavailable"
        if high_date > partition_date:
            return False, "52w-high date in future"
        age = _new_high_age_trading_days(snap, partition_date=partition_date)
        if age is None:
            # date present & not future, yet no sessions in (high, partition] →
            # the date is outside the XKRX calendar's range (or a calendar error).
            return False, "52w-high recency unavailable (calendar range)"
        if age > spec.max_new_high_age_trading_days:
            return False, "52w high not recent"

    return True, None


def _build_row(
    snap: InvestKrFundamentalsSnapshot,
    *,
    name: str | None,
    state: str,
    partition_date: dt.date,
) -> dict[str, Any]:
    """Row dict using the metric keys the screener renderer + _METRIC_FIELD expect."""
    return {
        "symbol": snap.symbol,
        "market": "kr",
        "name": name,  # from KRSymbolUniverse — NOT snap.name (ticker for KR)
        "close": _to_float(snap.price),
        "change_rate": _to_float(snap.change_rate),
        "volume": _to_float(snap.volume),
        "market_cap": _to_float(snap.market_cap),
        # ScreenerResultRow reads `row.get("sector") or row.get("category")`; the
        # granular industry best matches Toss labels, sector is the fallback.
        "category": snap.industry or snap.sector,
        "per": _to_float(snap.per),
        "pbr": _to_float(snap.pbr),
        "roe": _to_float(snap.roe_ttm),
        "dividend_yield": _to_float(snap.dividend_yield),
        "gross_margin_ttm": _to_float(snap.gross_margin_ttm),
        "payout_ratio": _to_float(snap.payout_ratio_ttm),
        # PROXY: tvscreener YoY mapped onto the 3y-avg metric keys (see docstring).
        "revenue_growth_3y_avg": _to_float(snap.revenue_yoy),
        "earnings_growth_3y_avg": _to_float(snap.eps_yoy),
        "earnings_growth_qoq": _to_float(snap.eps_qoq),
        "dividend_paid_streak_years": _to_float(snap.continuous_dividend_payout),
        "dividend_growth_streak_years": _to_float(snap.continuous_dividend_growth),
        "earnings_increase_streak_years": None,  # tvscreener does not provide it
        "rsi": _to_float(snap.rsi14),
        # ROB-430 PR-② / ROB-432: undervalued_breakout's signal is a recent NEW 52w
        # high. week_high_52_date (the high date) + new_high_age_trading_days (KRX
        # trading sessions since, smaller = more recent) are the honest fields;
        # high_52w_proximity stays as an informational column. None when the date /
        # 52w-high cannot be derived or the date is out of the XKRX calendar range.
        "week_high_52": _to_float(snap.week_high_52),
        "week_high_52_date": (
            snap.week_high_52_date.isoformat()
            if snap.week_high_52_date is not None
            else None
        ),
        "new_high_age_trading_days": _new_high_age_trading_days(
            snap, partition_date=partition_date
        ),
        "high_52w_proximity": (
            float(prox) if (prox := _high_52w_proximity(snap)) is not None else None
        ),
        "_screener_snapshot_state": state,
        "snapshot_date": partition_date,
    }


async def load_kr_fundamentals_preset_from_tv_snapshot(
    session: AsyncSession | None,
    *,
    market: str,
    spec: FundamentalsPresetSpec,
    limit: int = 20,
    now: Any = None,
    universe_count: int | None = None,
) -> FundamentalsScreenResult | None:
    """KR display read-path: serve a fundamentals preset from the tvscreener snapshot.

    Returns None when session is None, market != "kr", or no snapshot partition
    exists (caller → dataState=missing). Otherwise returns a
    :class:`FundamentalsScreenResult` with filled rows (price/change/volume/
    category/market_cap + metrics), the (capped) freshness state, and any honest
    skip warnings (e.g. the earnings-streak gap).
    """
    if session is None or market != "kr":
        return None

    from datetime import UTC, datetime

    from app.services.invest_screener_snapshots.freshness import today_trading_date
    from app.services.invest_view_model.screener_service import (
        _is_kr_toss_common_stock,
    )

    now_dt = now() if callable(now) else datetime.now(UTC)
    today_market_date = today_trading_date("kr", now=now_dt)

    hp = await _resolve_kr_partition(session, universe_count=universe_count)
    partition_date = hp.partition_date if hp else None
    if partition_date is None:
        return None

    state = "fresh" if partition_date == today_market_date else "stale"
    if hp and (hp.is_fallback or not hp.healthy):
        state = cap_degraded(state)

    # ROB-429 B1: evaluate the FULL healthy partition (no market-cap cand_cap that
    # excluded small caps). ~4,250 rows in-memory is cheap. A generous safety bound
    # only guards a pathological partition; hitting it is logged (not a normal cap).
    cand_stmt = (
        sa.select(InvestKrFundamentalsSnapshot)
        .where(InvestKrFundamentalsSnapshot.snapshot_date == partition_date)
        .order_by(InvestKrFundamentalsSnapshot.symbol)
        .limit(_MAX_PARTITION_ROWS)
    )
    snaps = list((await session.execute(cand_stmt)).scalars().all())
    if len(snaps) >= _MAX_PARTITION_ROWS:
        logger.warning(
            "kr_fundamentals_tv_screener: partition row load hit the safety bound "
            "%d for preset=%s (partition may be corrupt; some rows not evaluated)",
            _MAX_PARTITION_ROWS,
            spec.preset_id,
        )

    symbols = [s.symbol for s in snaps]
    name_map: dict[str, str] = {}
    if symbols:
        names = await session.execute(
            sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        name_map = {r.symbol: r.name for r in names.all()}

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_computed_at: dt.datetime | None = None
    for snap in snaps:
        sym = snap.symbol
        if sym in seen:
            continue
        name = name_map.get(sym)
        # common-stock filter (drop ETF/preferred/SPAC) + symbol dedup
        if not _is_kr_toss_common_stock(sym, name):
            continue
        seen.add(sym)
        passes, reason = _passes_thresholds(snap, spec, partition_date=partition_date)
        if not passes:
            excluded.append({"symbol": sym, "reason": reason})
            continue
        if snap.computed_at is not None and (
            last_computed_at is None or snap.computed_at > last_computed_at
        ):
            last_computed_at = snap.computed_at
        included.append(
            _build_row(snap, name=name, state=state, partition_date=partition_date)
        )

    # ROB-429 B2: full-partition match total BEFORE the display limit.
    total_matched = len(included)

    sort_row_key = _SORT_KEY_TO_ROW_KEY.get(spec.sort_by, spec.sort_by)
    # ROB-432: nulls always last; direction from spec.sort_descending. Ascending
    # (sort_descending=False) puts the smallest metric first — e.g. undervalued_breakout
    # sorts by PER ascending (cheapest first) to mirror Toss's 저평가 탈출 order.
    if spec.sort_descending:
        included.sort(
            key=lambda r: (
                r.get(sort_row_key) is None,
                -(r.get(sort_row_key) or 0.0),
                r["symbol"],
            )
        )
    else:
        included.sort(
            key=lambda r: (
                r.get(sort_row_key) is None,
                (r.get(sort_row_key) or 0.0),
                r["symbol"],
            )
        )
    included = included[:limit]

    warnings: list[str] = []
    if spec.min_earnings_increase_streak_years is not None:
        warnings.append(EARNINGS_STREAK_SKIP_WARNING)

    return FundamentalsScreenResult(
        rows=included,
        valuation_partition_date=partition_date,
        fundamentals_partition_date=partition_date,
        fundamentals_collected_at=last_computed_at,
        fundamentals_state="fresh" if snaps else "missing",
        excluded=excluded,
        warnings=warnings,
        total_matched=total_matched,
    )
