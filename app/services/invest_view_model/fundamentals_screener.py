"""Read-only loader for fundamentals-backed Toss-parity screener presets (ROB-422 PR2a).

Candidate universe comes from the latest market_valuation_snapshots partition
(valuation conditions, e.g. ROE). Each candidate's financial_fundamentals_snapshots
periods are run through the pure PIT-gated derive_fundamentals_metrics(report_date=today),
and the preset's fundamentals thresholds are applied. A metric whose state is not
'ok' excludes the candidate (never a silent pass). When the fundamentals table has
no rows (operator backfill pending), the result is empty with a 'missing' fundamentals
dependency state — honest, never fabricated.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.financial_fundamentals_snapshots.derive import (
    FundamentalPeriod,
    derive_fundamentals_metrics,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundamentalsPresetSpec:
    preset_id: str
    # valuation filters (applied in the SQL candidate query):
    min_roe: Decimal | None = None  # percent (e.g. 15)
    max_per: Decimal | None = None  # 0 < per <= max_per
    max_pbr: Decimal | None = None  # 0 < pbr <= max_pbr
    min_dividend_yield: Decimal | None = (
        None  # ratio (e.g. 0.01 == 1%), KR naver stores /100
    )
    # derive thresholds (applied in evaluate_fundamentals_candidates):
    min_gross_margin_ttm: Decimal | None = None  # ratio (0.20)
    min_revenue_growth_3y_avg: Decimal | None = None  # ratio (0.10)
    min_earnings_growth_3y_avg: Decimal | None = None  # ratio (0.10 / 0.20)
    min_earnings_increase_streak_years: int | None = None  # years (3)
    min_dividend_growth_streak_years: int | None = None  # years (3)
    min_dividend_paid_streak_years: int | None = None  # years (3)
    min_payout_ratio: Decimal | None = None  # percent (30) — DART 현금배당성향%
    min_earnings_growth_qoq: Decimal | None = None  # ratio (0.10)
    # ROB-430 PR-②: Toss "신고가" = a NEW 52-week high made within N days (a breakout
    # event), NOT proximity to the high. Implemented in the tvscreener KR loader via
    # week_high_52_date recency. ROB-432: N counts KRX *trading* sessions (XKRX,
    # holiday-aware via session_calendar), not calendar days — Toss's "20일" = 20 거래일.
    # The DART loader ignores it (no DART preset uses it).
    max_new_high_age_trading_days: int | None = (
        None  # 52w-high set within N KRX sessions
    )
    sort_by: str = "roe"  # any metric key carried on the output row
    # ROB-432: display sort direction. Default desc (highest metric first, e.g. ROE).
    # undervalued_breakout uses ascending PER (cheapest first) to mirror Toss's
    # 저평가 탈출 default order (lowest PER on top).
    sort_descending: bool = True


PROFITABLE_COMPANY_SPEC = FundamentalsPresetSpec(
    preset_id="profitable_company",
    min_roe=Decimal("15"),
    min_gross_margin_ttm=Decimal("0.20"),
    # ROB-432: Toss 돈 잘버는 회사 default order = 매출총이익률 desc (observed 100.00 >
    # 99.09 > 99.02 ... strictly; ROE jumps around → not the key).
    sort_by="gross_margin_ttm",
)

UNDERVALUED_GROWTH_SPEC = FundamentalsPresetSpec(
    preset_id="undervalued_growth",
    max_per=Decimal("20"),
    min_revenue_growth_3y_avg=Decimal("0.10"),
    min_earnings_growth_3y_avg=Decimal("0.20"),
    # ROB-432: Toss 저평가 성장주 default order = 연평균 매출액 증감률 desc (observed
    # 1806 > 333 > 290 ... strictly; the earnings-growth column is not monotonic).
    sort_by="revenue_growth_3y_avg",
)

STABLE_GROWTH_SPEC = FundamentalsPresetSpec(
    preset_id="stable_growth",
    min_roe=Decimal("15"),
    min_earnings_growth_3y_avg=Decimal("0.10"),
    min_earnings_increase_streak_years=3,
    sort_by="roe",
)

FUTURE_DIVIDEND_KING_SPEC = FundamentalsPresetSpec(
    preset_id="future_dividend_king",
    min_dividend_yield=Decimal("0.01"),
    min_dividend_growth_streak_years=3,
    min_earnings_increase_streak_years=3,
    min_payout_ratio=Decimal("30"),
    sort_by="dividend_yield",
)

CHEAP_VALUE_SPEC = FundamentalsPresetSpec(
    preset_id="cheap_value",
    max_per=Decimal("15"),
    max_pbr=Decimal("1.5"),
    min_earnings_growth_3y_avg=Decimal("0"),  # 3y-avg net income growth >= 0%
    # ROB-432: Toss 아직 저렴한 가치주 default order = PBR ascending (cheapest first;
    # observed 0.02 < 0.05 ... ; PER and earnings-growth columns are not monotonic).
    sort_by="pbr",
    sort_descending=False,
)

STEADY_DIVIDEND_SPEC = FundamentalsPresetSpec(
    preset_id="steady_dividend",
    min_dividend_yield=Decimal("0.03"),  # 3% (ratio; KR naver stores /100)
    min_payout_ratio=Decimal("30"),
    min_dividend_paid_streak_years=3,
    min_earnings_increase_streak_years=3,
    sort_by="dividend_yield",
)

GROWTH_EXPECTATION_TOSS_SPEC = FundamentalsPresetSpec(
    preset_id="growth_expectation_toss",
    min_earnings_growth_3y_avg=Decimal("0.03"),
    min_earnings_growth_qoq=Decimal("0.10"),
    # ROB-432: Toss 성장 기대주 default order = 연평균 순이익 증감률 desc (observed
    # 700 > 687 > 550 ... strictly; the QoQ column is not monotonic).
    sort_by="earnings_growth_3y_avg",
)

# ROB-428 PR-C: the last 2 KR Toss valuation presets, rerouted onto the tvscreener
# KR snapshot (replicates the OLD load_high_yield_value/undervalued_breakout rules
# exactly so display fills category + uses tvscreener's 100% ROE coverage).
# high_yield_value: ROE >= 15 + 0 < PER <= 10.
HIGH_YIELD_VALUE_SPEC = FundamentalsPresetSpec(
    preset_id="high_yield_value",
    min_roe=Decimal("15"),
    max_per=Decimal("10"),
    sort_by="roe",
)

# undervalued_breakout: 0 < PER <= 10 + 0 < PBR <= 1 + 신고가 (a recent NEW 52-week
# high). ROB-430 PR-②: Toss's "신고가" filter default is "52주 신고가 / 20일 이내" — a
# recent breakout event, NOT price/52w-high proximity. Deep-value names (PER<=10,
# PBR<=1) sit far below their 52w high (probe: max proximity 0.94 → 0 matches under
# the old 0.95 rule), yet many DID set a new 52w high recently.
#
# Toss's "20일" is 20 KRX *trading* days. ROB-432: we now count exact trading
# sessions between week_high_52_date and the partition via XKRX (session_calendar,
# holiday-aware), replacing the earlier 30-calendar-day approximation. Earlier probe:
# PER<=10 & PBR<=1 = 580 (matches Toss); the new-high recency narrows it toward Toss's
# 77 (the trading-day window is tighter than 30 calendar days; comparable, not
# byte-identical). Out-of-XKRX-range dates fail closed (excluded), never mis-included.
_NEW_HIGH_RECENCY_TRADING_DAYS = 20
UNDERVALUED_BREAKOUT_SPEC = FundamentalsPresetSpec(
    preset_id="undervalued_breakout",
    max_per=Decimal("10"),
    max_pbr=Decimal("1"),
    max_new_high_age_trading_days=_NEW_HIGH_RECENCY_TRADING_DAYS,
    # ROB-432: Toss 저평가 탈출 default order = PER ascending (cheapest PER first;
    # observed PER 0.66 < 1.36 < 1.54 < 2.84). Was market_cap desc (ROB-430 PR-②
    # wrong assumption) → visible top-N mismatched Toss.
    sort_by="per",
    sort_descending=False,
)

FUNDAMENTALS_PRESET_SPECS: dict[str, FundamentalsPresetSpec] = {
    s.preset_id: s
    for s in (
        PROFITABLE_COMPANY_SPEC,
        UNDERVALUED_GROWTH_SPEC,
        STABLE_GROWTH_SPEC,
        FUTURE_DIVIDEND_KING_SPEC,
        CHEAP_VALUE_SPEC,
        STEADY_DIVIDEND_SPEC,
        GROWTH_EXPECTATION_TOSS_SPEC,
        HIGH_YIELD_VALUE_SPEC,
        UNDERVALUED_BREAKOUT_SPEC,
    )
}


@dataclass(frozen=True)
class FundamentalsScreenResult:
    rows: list[dict[str, Any]]
    valuation_partition_date: dt.date | None
    fundamentals_partition_date: dt.date | None
    fundamentals_collected_at: dt.datetime | None
    fundamentals_state: str  # 'fresh' | 'stale' | 'missing'
    excluded: list[dict[str, Any]] = field(default_factory=list)
    # ROB-428 PR-B: honest-divergence warnings the caller surfaces to the user
    # (e.g. the earnings-streak condition skipped because tvscreener omits it).
    # The DART loader leaves this empty; the tvscreener KR loader populates it.
    warnings: list[str] = field(default_factory=list)
    # ROB-429 B2: full-partition predicate match count BEFORE the display limit is
    # applied (the tvscreener KR loader sets it; the DART loader leaves 0).
    total_matched: int = 0


def _to_period(row: FinancialFundamentalsSnapshot) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=row.fiscal_period,
        period_type=row.period_type,
        period_end_date=row.period_end_date,
        filing_date=row.filing_date,
        revenue=row.revenue,
        net_income=row.net_income,
        gross_profit=row.gross_profit,
        cost_of_sales=row.cost_of_sales,
        discrete_revenue=row.discrete_revenue,
        discrete_net_income=row.discrete_net_income,
        payout_ratio=row.payout_ratio,
        dividend_per_share=row.dividend_per_share,
        roe=row.roe,
    )


# (spec field, derivation attribute) — each non-None spec field is checked.
_DERIVE_CHECKS: tuple[tuple[str, str], ...] = (
    ("min_gross_margin_ttm", "gross_margin_ttm"),
    ("min_revenue_growth_3y_avg", "revenue_growth_3y_avg"),
    ("min_earnings_growth_3y_avg", "earnings_growth_3y_avg"),
    ("min_payout_ratio", "payout_ratio"),
    ("min_earnings_increase_streak_years", "earnings_increase_streak_years"),
    ("min_dividend_growth_streak_years", "dividend_growth_streak_years"),
    ("min_dividend_paid_streak_years", "dividend_paid_streak_years"),
    ("min_earnings_growth_qoq", "earnings_growth_qoq"),
)

_CARRIED_DERIVE_METRICS = (
    "gross_margin_ttm",
    "revenue_growth_3y_avg",
    "earnings_growth_3y_avg",
    "payout_ratio",
    "earnings_increase_streak_years",
    "dividend_growth_streak_years",
    "dividend_paid_streak_years",
    "earnings_growth_qoq",
)


def _metric_float(m: Any) -> float | None:
    return float(m.value) if m is not None and m.value is not None else None


def evaluate_fundamentals_candidates(
    *,
    valuation_rows: list[dict[str, Any]],
    periods_by_symbol: dict[str, list[FundamentalPeriod]],
    spec: FundamentalsPresetSpec,
    report_date: dt.date,
    limit: int,
    name_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure: apply the preset spec to candidates. Returns (included_rows, excluded).

    Each active derive threshold (non-None spec field) must be 'ok' AND meet the
    threshold; state != 'ok' or value None excludes the candidate (never a silent pass).
    """
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for v in valuation_rows:
        symbol = v["symbol"]
        derivation = derive_fundamentals_metrics(
            periods_by_symbol.get(symbol, []), report_date=report_date
        )
        rejected = False
        for spec_field, metric_attr in _DERIVE_CHECKS:
            threshold = getattr(spec, spec_field)
            if threshold is None:
                continue
            metric = getattr(derivation, metric_attr)
            if metric.state != "ok" or metric.value is None:
                excluded.append(
                    {"symbol": symbol, "reason": f"{metric_attr} unavailable"}
                )
                rejected = True
                break
            if Decimal(str(metric.value)) < Decimal(str(threshold)):
                excluded.append(
                    {"symbol": symbol, "reason": f"{metric_attr} below threshold"}
                )
                rejected = True
                break
        if rejected:
            continue
        row = {
            "symbol": symbol,
            "market": "kr",
            "name": name_map.get(symbol),
            "roe": float(v["roe"]) if v.get("roe") is not None else None,
            "per": float(v["per"]) if v.get("per") is not None else None,
            "pbr": float(v["pbr"]) if v.get("pbr") is not None else None,
            "market_cap": float(v["market_cap"])
            if v.get("market_cap") is not None
            else None,
            "dividend_yield": float(v["dividend_yield"])
            if v.get("dividend_yield") is not None
            else None,
            "_screener_snapshot_state": v.get("_screener_snapshot_state", "fresh"),
        }
        for metric_attr in _CARRIED_DERIVE_METRICS:
            row[metric_attr] = _metric_float(getattr(derivation, metric_attr))
        included.append(row)
    # ROB-432: honor spec.sort_descending (mirror the tvscreener KR loader) so an
    # ascending preset (e.g. cheap_value sorts PBR ascending) is consistent on both
    # the DART (report/PIT) path and the display path. Nulls always last.
    if spec.sort_descending:
        included.sort(
            key=lambda r: (
                r.get(spec.sort_by) is None,
                -(r.get(spec.sort_by) or 0.0),
                r["symbol"],
            )
        )
    else:
        included.sort(
            key=lambda r: (
                r.get(spec.sort_by) is None,
                (r.get(spec.sort_by) or 0.0),
                r["symbol"],
            )
        )
    return included[:limit], excluded


async def load_fundamentals_preset_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    spec: FundamentalsPresetSpec,
    limit: int = 20,
    now: Any = None,
) -> FundamentalsScreenResult | None:
    """None when no valuation partition exists (caller → dataState=missing)."""
    if session is None or market != "kr":
        return None
    from datetime import UTC, datetime

    from app.services.invest_screener_snapshots.freshness import today_trading_date
    from app.services.invest_view_model.screener_service import _is_kr_toss_common_stock

    now_dt = now() if callable(now) else datetime.now(UTC)
    today_market_date = today_trading_date("kr", now=now_dt)

    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
    )

    val_hp = await resolve_healthy_partition(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="kr",
    )
    val_date = val_hp.partition_date if val_hp else None
    if val_date is None:
        return None

    cand_stmt = sa.select(
        MarketValuationSnapshot.symbol,
        MarketValuationSnapshot.roe,
        MarketValuationSnapshot.per,
        MarketValuationSnapshot.pbr,
        MarketValuationSnapshot.market_cap,
        MarketValuationSnapshot.dividend_yield,
    ).where(
        MarketValuationSnapshot.market == "kr",
        MarketValuationSnapshot.snapshot_date == val_date,
    )
    if spec.min_roe is not None:
        cand_stmt = cand_stmt.where(MarketValuationSnapshot.roe >= spec.min_roe)
    if spec.max_per is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.per > 0,
            MarketValuationSnapshot.per <= spec.max_per,
        )
    if spec.max_pbr is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.pbr > 0,
            MarketValuationSnapshot.pbr <= spec.max_pbr,
        )
    if spec.min_dividend_yield is not None:
        cand_stmt = cand_stmt.where(
            MarketValuationSnapshot.dividend_yield >= spec.min_dividend_yield
        )
    # Cap the candidate universe by market_cap (prefer liquid names); ranking by the
    # preset's sort_by happens AFTER derive. Surface truncation honestly (no silent cap).
    _cand_cap = max(limit * 8, 200)
    cand_stmt = cand_stmt.order_by(
        MarketValuationSnapshot.market_cap.desc().nullslast()
    ).limit(_cand_cap)
    cand_mappings = list((await session.execute(cand_stmt)).mappings().all())
    if len(cand_mappings) >= _cand_cap:
        logger.warning(
            "fundamentals_screener: candidate universe capped at %d for preset=%s "
            "(some lower-market-cap candidates not evaluated)",
            _cand_cap,
            spec.preset_id,
        )

    val_state = "fresh" if val_date == today_market_date else "stale"
    # ROB-426 PR2a: a thin/fallback valuation partition must not be labeled fresh
    # even when its date matches today (consistency with the other screener loaders).
    if val_hp and (val_hp.is_fallback or not val_hp.healthy):
        from app.services.invest_screener_snapshots.partition_health import (
            cap_degraded,
        )

        val_state = cap_degraded(val_state)
    symbols = [m["symbol"] for m in cand_mappings]

    name_map: dict[str, str] = {}
    if symbols:
        names = await session.execute(
            sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        name_map = {r.symbol: r.name for r in names.all()}

    # common-stock filter (drop ETF/preferred) + symbol dedup (defensive: KR is single-source
    # today, but a future second valuation source must not produce duplicate candidates).
    valuation_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in cand_mappings:
        sym = m["symbol"]
        if sym in seen:
            continue
        if not _is_kr_toss_common_stock(sym, name_map.get(sym)):
            continue
        seen.add(sym)
        valuation_rows.append({**dict(m), "_screener_snapshot_state": val_state})

    repo = FinancialFundamentalsSnapshotsRepository(session)
    period_rows = await repo.latest_periods_for_symbols(
        market="kr", symbols=[v["symbol"] for v in valuation_rows]
    )
    periods_by_symbol = {
        sym: [_to_period(r) for r in rows] for sym, rows in period_rows.items()
    }

    # fundamentals partition metadata + state (missing when nothing backfilled)
    fund_date = None
    fund_collected: dt.datetime | None = None
    if period_rows:
        all_rows = [r for rows in period_rows.values() for r in rows]
        fund_date = max((r.period_end_date for r in all_rows), default=None)
        fund_collected = max((r.source_collected_at for r in all_rows), default=None)
    fundamentals_state = "missing" if not period_rows else "fresh"

    included, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods_by_symbol,
        spec=spec,
        report_date=today_market_date,
        limit=limit,
        name_map=name_map,
    )
    for r in included:
        r["snapshot_date"] = val_date
    return FundamentalsScreenResult(
        rows=included,
        valuation_partition_date=val_date,
        fundamentals_partition_date=fund_date,
        fundamentals_collected_at=fund_collected,
        fundamentals_state=fundamentals_state,
        excluded=excluded,
        # The DART loader isn't the screener display path; total_matched mirrors
        # the displayed rows here (B2 consistency, not full-partition).
        total_matched=len(included),
    )
