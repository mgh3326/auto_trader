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
# ROB-433: shown when some displayed rows fell back to the tvscreener 1yr-YoY proxy
# for a 3년평균 증감률 condition because DART (financial_fundamentals_snapshots) was
# not backfilled for them. Exact 3y-avg rises as the operator backfills DART.
GROWTH_PROXY_WARNING = (
    "일부 종목의 3년평균 증감률은 근사치(연간 YoY)로 평가됨 (DART 미적재)"
)

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
    ("min_earnings_growth_qoq", "eps_qoq", False),
    # ROB-444: payout_ratio + dividend streaks moved to the DART-first block below
    # (_DIVIDEND_DART_CHECKS) — tvscreener payout_ratio_ttm was 2.6% sparse.
)

# ROB-444: dividend payout/streak — DART-first (financial_fundamentals derive),
# tvscreener column fallback. DART payout_ratio (현금배당성향, percent — same unit as
# the tvscreener column + spec) covers far more KR symbols than tvscreener's
# payout_ratio_ttm (~2.6%). Streaks fall back to tvscreener continuous_dividend_*
# (~64%) when DART is absent. (spec_field, tvscreener_fallback_col, dart_metric_attr)
_DIVIDEND_DART_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("min_payout_ratio", "payout_ratio_ttm", "payout_ratio"),
    (
        "min_dividend_paid_streak_years",
        "continuous_dividend_payout",
        "dividend_paid_streak_years",
    ),
    (
        "min_dividend_growth_streak_years",
        "continuous_dividend_growth",
        "dividend_growth_streak_years",
    ),
)

# ROB-433: 3-year-average growth thresholds are evaluated DART-first. Each entry is
# (spec_field, tvscreener_proxy_col, dart_metric_attr). When the DART derivation
# (financial_fundamentals_snapshots) has the exact 3y-avg metric (state == "ok") we
# use it (growth_source="dart"); otherwise we fall back to the tvscreener 1yr-YoY
# PROXY column (growth_source="proxy"), surfaced honestly per row + via a warning.
_GROWTH_3Y_AVG_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("min_revenue_growth_3y_avg", "revenue_yoy", "revenue_growth_3y_avg"),
    ("min_earnings_growth_3y_avg", "eps_yoy", "earnings_growth_3y_avg"),
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
    dart: Any = None,
) -> tuple[bool, str | None, dict[str, str | None]]:
    """Apply the spec's active thresholds (fail-closed). Returns
    ``(passes, reject_reason, provenance)``.

    ROB-433: the 3y-avg growth thresholds and the net-income streak are evaluated
    DART-first (``dart`` = the FundamentalsDerivation for this symbol, or None) and
    fall back to the tvscreener YoY proxy (growth) / SKIP (streak) when DART is
    absent. provenance = ``{"growth_source": "dart"|"proxy"|None,
    "streak_source": "dart"|"skipped"|None}`` and is only meaningful when passes.
    """
    provenance: dict[str, str | None] = {
        "growth_source": None,
        "streak_source": None,
        "dividend_source": None,  # ROB-444: "dart" | "tvscreener"
    }

    for spec_field, col_attr, require_positive in _THRESHOLD_CHECKS:
        threshold = getattr(spec, spec_field)
        if threshold is None:
            continue
        value = getattr(snap, col_attr)
        if value is None:
            return False, f"{col_attr} unavailable", provenance
        if require_positive and value <= 0:
            return False, f"{col_attr} not positive", provenance
        # ROB-444: tvscreener dividend_yield is a PERCENT (e.g. 6.32 = 6.32%), but the
        # shared spec threshold is a RATIO (min_dividend_yield=0.03 = 3%; US
        # market_valuation stores dividend_yield as a ratio). Compare in ratio so the
        # 3% gate actually filters — without this, 0.48% passed `0.48 < 0.03` = False.
        # The row's displayed dividend_yield stays percent (metric label `{v:.2f}%`).
        cmp_value = (
            Decimal(str(value)) / Decimal("100")
            if col_attr == "dividend_yield"
            else Decimal(str(value))
        )
        threshold_dec = Decimal(str(threshold))
        if spec_field in _MAX_FIELDS:
            if cmp_value > threshold_dec:
                return False, f"{col_attr} above max", provenance
        else:
            if cmp_value < threshold_dec:
                return False, f"{col_attr} below min", provenance

    # ROB-433: 3y-avg growth — DART (exact 3년평균) first, tvscreener YoY proxy fallback.
    for spec_field, proxy_col, dart_attr in _GROWTH_3Y_AVG_CHECKS:
        threshold = getattr(spec, spec_field)
        if threshold is None:
            continue
        dm = getattr(dart, dart_attr, None) if dart is not None else None
        if dm is not None and dm.state == "ok" and dm.value is not None:
            value, source = dm.value, "dart"
        else:
            value, source = getattr(snap, proxy_col), "proxy"
        if value is None:
            return False, f"{dart_attr} unavailable", provenance
        if Decimal(str(value)) < Decimal(str(threshold)):
            return False, f"{dart_attr} below min", provenance
        # "proxy" wins (least-precise): if ANY growth metric fell back, mark proxy.
        if provenance["growth_source"] != "proxy":
            provenance["growth_source"] = source

    # ROB-433: 순이익 연속증가 streak — DART first; SKIP (fail-open) when DART absent
    # (no tvscreener column). Skipped rows still pass; the caller warns.
    if spec.min_earnings_increase_streak_years is not None:
        dm = (
            getattr(dart, "earnings_increase_streak_years", None)
            if dart is not None
            else None
        )
        if dm is not None and dm.state == "ok" and dm.value is not None:
            if Decimal(str(dm.value)) < Decimal(
                str(spec.min_earnings_increase_streak_years)
            ):
                return False, "earnings_increase_streak_years below min", provenance
            provenance["streak_source"] = "dart"
        else:
            provenance["streak_source"] = "skipped"

    # ROB-444: dividend payout/streak — DART-first, tvscreener column fallback.
    # tvscreener payout_ratio_ttm is ~2.6% sparse (the binding constraint behind
    # steady_dividend 3 / future_dividend_king 0); DART payout_ratio + dividend
    # streaks (financial_fundamentals derive) recover coverage. NULL on BOTH sources
    # is fail-closed (a real dividend condition, never fabricated).
    for spec_field, fallback_col, dart_attr in _DIVIDEND_DART_CHECKS:
        threshold = getattr(spec, spec_field)
        if threshold is None:
            continue
        dm = getattr(dart, dart_attr, None) if dart is not None else None
        if dm is not None and dm.state == "ok" and dm.value is not None:
            value, source = dm.value, "dart"
        else:
            value, source = getattr(snap, fallback_col), "tvscreener"
        if value is None:
            return False, f"{dart_attr} unavailable", provenance
        if Decimal(str(value)) < Decimal(str(threshold)):
            return False, f"{dart_attr} below min", provenance
        # DART wins; only downgrade to tvscreener if not already DART-sourced.
        if provenance.get("dividend_source") != "tvscreener":
            provenance["dividend_source"] = source

    # ROB-430 PR-② / ROB-432: 신고가 = a NEW 52-week high made within
    # max_new_high_age_trading_days KRX trading sessions of the partition (a breakout
    # event), checked via week_high_52_date recency. Fail-closed with DISTINCT reasons
    # so an operator can tell a missing-data exclude from a calendar-range one:
    #   NULL date / future date / out-of-XKRX-range each get their own reason.
    if spec.max_new_high_age_trading_days is not None:
        high_date = snap.week_high_52_date
        if high_date is None:
            return False, "52w-high date unavailable", provenance
        if high_date > partition_date:
            return False, "52w-high date in future", provenance
        age = _new_high_age_trading_days(snap, partition_date=partition_date)
        if age is None:
            # date present & not future, yet no sessions in (high, partition] →
            # the date is outside the XKRX calendar's range (or a calendar error).
            return False, "52w-high recency unavailable (calendar range)", provenance
        if age > spec.max_new_high_age_trading_days:
            return False, "52w high not recent", provenance

    return True, None, provenance


def _build_row(
    snap: InvestKrFundamentalsSnapshot,
    *,
    name: str | None,
    state: str,
    partition_date: dt.date,
    provenance: dict[str, str | None] | None = None,
    master_sector: str | None = None,
) -> dict[str, Any]:
    """Row dict using the metric keys the screener renderer + _METRIC_FIELD expect."""
    prov = provenance or {}
    return {
        "symbol": snap.symbol,
        "market": "kr",
        "name": name,  # from KRSymbolUniverse — NOT snap.name (ticker for KR)
        # ROB-433: per-row provenance for the DART-first growth/streak metrics —
        # "dart" (exact 3년평균/연속증가) vs "proxy" (tvscreener YoY) / "skipped".
        "growth_source": prov.get("growth_source"),
        "streak_source": prov.get("streak_source"),
        "close": _to_float(snap.price),
        "change_rate": _to_float(snap.change_rate),
        "volume": _to_float(snap.volume),
        "market_cap": _to_float(snap.market_cap),
        # ROB-512 갭3: 마스터 한글 업종(symbol_sectors). 포맷터가 sector를
        # category보다 먼저 읽으므로 워밍된 종목은 한글, 아니면 영문 fallback.
        "sector": master_sector,
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
    sector_map: dict[str, str] = {}
    if symbols:
        from app.models.symbol_sectors import SymbolSector

        names = await session.execute(
            sa.select(
                KRSymbolUniverse.symbol,
                KRSymbolUniverse.name,
                SymbolSector.name_kr.label("sector_name_kr"),
                SymbolSector.name_en.label("sector_name_en"),
            )
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        _name_rows = names.all()
        name_map = {r.symbol: r.name for r in _name_rows}
        # ROB-512 갭3: 마스터 한글 업종 우선 — 워밍된 심볼만 라벨이 잡히고,
        # 나머지는 _build_row의 영문 category(tvscreener) fallback이 그대로 표시.
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

    # ROB-436 C-1: the tvscreener snapshot market_cap is unreliable for KR display
    # (renders absurd-high like 3,468조원 AND bogus-low like 1억원 for real mid-caps).
    # Override with the trusted Naver KRW market cap from the latest healthy
    # market_valuation_snapshots partition when present. Symbols without a valuation
    # row keep the tvscreener value (the ROB-436 C-1 absurd-high ceiling still hides
    # implausible ones). Coverage scales with the operator's valuation-snapshot builds.
    market_cap_map: dict[str, float] = {}
    if symbols:
        from app.models.market_valuation_snapshot import MarketValuationSnapshot
        from app.services.invest_screener_snapshots.partition_health import (
            resolve_healthy_partition,
        )
        from app.services.market_valuation_snapshots.repository import (
            metric_rich_filter,
        )

        val_hp = await resolve_healthy_partition(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
            row_filter=metric_rich_filter(),  # ROB-551: skip toss-only partitions
        )
        if val_hp and val_hp.partition_date is not None:
            val_rows = await session.execute(
                sa.select(
                    MarketValuationSnapshot.symbol,
                    MarketValuationSnapshot.market_cap,
                ).where(
                    MarketValuationSnapshot.market == "kr",
                    MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                    MarketValuationSnapshot.symbol.in_(symbols),
                )
            )
            for r in val_rows.all():
                if r.market_cap is not None:
                    market_cap_map[r.symbol] = float(r.market_cap)

    # ROB-433: DART-first growth/streak. Only fetch the financial_fundamentals
    # derivation when this preset uses a 3년평균 growth or 순이익 연속증가 threshold
    # (valuation-only presets skip the extra query). DART coverage is sparse
    # (operator backfill), so most symbols fall back to the tvscreener proxy / SKIP,
    # surfaced honestly per row (growth_source/streak_source) + via the warning below.
    dart_by_symbol: dict[str, Any] = {}
    if symbols and (
        spec.min_revenue_growth_3y_avg is not None
        or spec.min_earnings_growth_3y_avg is not None
        or spec.min_earnings_increase_streak_years is not None
        # ROB-444: dividend payout/streak are DART-first too (tvscreener
        # payout_ratio_ttm is ~2.6% sparse → it was the binding constraint).
        or spec.min_payout_ratio is not None
        or spec.min_dividend_paid_streak_years is not None
        or spec.min_dividend_growth_streak_years is not None
    ):
        from app.services.financial_fundamentals_snapshots.derive import (
            derive_fundamentals_metrics,
        )
        from app.services.financial_fundamentals_snapshots.repository import (
            FinancialFundamentalsSnapshotsRepository,
        )
        from app.services.invest_view_model.fundamentals_screener import _to_period

        try:
            period_rows = await FinancialFundamentalsSnapshotsRepository(
                session
            ).latest_periods_for_symbols(market="kr", symbols=symbols)
            dart_by_symbol = {
                sym: derive_fundamentals_metrics(
                    [_to_period(r) for r in rows], report_date=today_market_date
                )
                for sym, rows in period_rows.items()
            }
        except Exception as exc:  # noqa: BLE001 — DART optional; proxy fallback on error
            logger.warning(
                "kr_fundamentals_tv_screener: DART derivation failed (proxy "
                "fallback) for preset=%s: %s",
                spec.preset_id,
                exc,
                exc_info=True,
            )

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
        passes, reason, prov = _passes_thresholds(
            snap, spec, partition_date=partition_date, dart=dart_by_symbol.get(sym)
        )
        if not passes:
            excluded.append({"symbol": sym, "reason": reason})
            continue
        if snap.computed_at is not None and (
            last_computed_at is None or snap.computed_at > last_computed_at
        ):
            last_computed_at = snap.computed_at
        included.append(
            _build_row(
                snap,
                name=name,
                state=state,
                partition_date=partition_date,
                provenance=prov,
                master_sector=sector_map.get(sym),
            )
        )

    # ROB-436 C-1: apply the trusted KRW market cap to display + sort. market_cap_krw
    # is what _normalize_market_cap_krw prefers for the label; overwrite market_cap too
    # so a market_cap-sorted preset orders on the trusted value, not the tvscreener one.
    if market_cap_map:
        for row in included:
            trusted = market_cap_map.get(row["symbol"])
            if trusted is not None:
                row["market_cap"] = trusted
                row["market_cap_krw"] = trusted

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

    # ROB-433: warnings reflect the DISPLAYED rows' provenance. The streak warning
    # now fires only when some displayed row actually fell back to SKIP (DART
    # applied it elsewhere); the growth warning fires when any displayed row used
    # the YoY proxy. Both shrink automatically as the operator backfills DART.
    warnings: list[str] = []
    if spec.min_earnings_increase_streak_years is not None and any(
        r.get("streak_source") == "skipped" for r in included
    ):
        warnings.append(EARNINGS_STREAK_SKIP_WARNING)
    if any(r.get("growth_source") == "proxy" for r in included):
        warnings.append(GROWTH_PROXY_WARNING)

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
