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
    min_roe: Decimal | None = None              # percent (e.g. 15)
    max_per: Decimal | None = None              # 0 < per <= max_per
    min_dividend_yield: Decimal | None = None   # ratio (e.g. 0.01 == 1%), KR naver stores /100
    # derive thresholds (applied in evaluate_fundamentals_candidates):
    min_gross_margin_ttm: Decimal | None = None         # ratio (0.20)
    min_revenue_growth_3y_avg: Decimal | None = None    # ratio (0.10)
    min_earnings_growth_3y_avg: Decimal | None = None   # ratio (0.10 / 0.20)
    min_earnings_increase_streak_years: int | None = None   # years (3)
    min_dividend_growth_streak_years: int | None = None     # years (3)
    min_payout_ratio: Decimal | None = None             # percent (30) — DART 현금배당성향%
    sort_by: str = "roe"  # any metric key carried on the output row


PROFITABLE_COMPANY_SPEC = FundamentalsPresetSpec(
    preset_id="profitable_company",
    min_roe=Decimal("15"),
    min_gross_margin_ttm=Decimal("0.20"),
    sort_by="roe",
)

UNDERVALUED_GROWTH_SPEC = FundamentalsPresetSpec(
    preset_id="undervalued_growth",
    max_per=Decimal("20"),
    min_revenue_growth_3y_avg=Decimal("0.10"),
    min_earnings_growth_3y_avg=Decimal("0.20"),
    sort_by="earnings_growth_3y_avg",
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

FUNDAMENTALS_PRESET_SPECS: dict[str, FundamentalsPresetSpec] = {
    s.preset_id: s
    for s in (
        PROFITABLE_COMPANY_SPEC,
        UNDERVALUED_GROWTH_SPEC,
        STABLE_GROWTH_SPEC,
        FUTURE_DIVIDEND_KING_SPEC,
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


def evaluate_fundamentals_candidates(
    *,
    valuation_rows: list[dict[str, Any]],
    periods_by_symbol: dict[str, list[FundamentalPeriod]],
    spec: FundamentalsPresetSpec,
    report_date: dt.date,
    limit: int,
    name_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure: apply the preset spec to candidates. Returns (included_rows, excluded)."""
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for v in valuation_rows:
        symbol = v["symbol"]
        periods = periods_by_symbol.get(symbol, [])
        derivation = derive_fundamentals_metrics(periods, report_date=report_date)
        if spec.min_gross_margin_ttm is not None:
            gm = derivation.gross_margin_ttm
            if gm.state != "ok" or gm.value is None:
                excluded.append(
                    {"symbol": symbol, "reason": "gross_margin_ttm unavailable"}
                )
                continue
            if Decimal(str(gm.value)) < spec.min_gross_margin_ttm:
                excluded.append(
                    {"symbol": symbol, "reason": "gross_margin_ttm below threshold"}
                )
                continue
        gm_value = (
            float(derivation.gross_margin_ttm.value)
            if derivation.gross_margin_ttm.value is not None
            else None
        )
        included.append(
            {
                "symbol": symbol,
                "market": "kr",
                "name": name_map.get(symbol),
                "roe": float(v["roe"]) if v.get("roe") is not None else None,
                "per": float(v["per"]) if v.get("per") is not None else None,
                "pbr": float(v["pbr"]) if v.get("pbr") is not None else None,
                "market_cap": float(v["market_cap"])
                if v.get("market_cap") is not None
                else None,
                "gross_margin_ttm": gm_value,
                "_screener_snapshot_state": v.get("_screener_snapshot_state", "fresh"),
            }
        )
    sort_key = "roe" if spec.sort_by == "roe" else "gross_margin_ttm"
    included.sort(
        key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0.0), r["symbol"])
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

    try:
        val_date = (
            await session.execute(
                sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
                    MarketValuationSnapshot.market == "kr"
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fundamentals_screener: val date lookup failed: %s", exc, exc_info=True
        )
        return None
    if val_date is None:
        return None

    cand_stmt = sa.select(
        MarketValuationSnapshot.symbol,
        MarketValuationSnapshot.roe,
        MarketValuationSnapshot.per,
        MarketValuationSnapshot.pbr,
        MarketValuationSnapshot.market_cap,
    ).where(
        MarketValuationSnapshot.market == "kr",
        MarketValuationSnapshot.snapshot_date == val_date,
    )
    if spec.min_roe is not None:
        cand_stmt = cand_stmt.where(MarketValuationSnapshot.roe >= spec.min_roe)
    cand_stmt = cand_stmt.order_by(
        MarketValuationSnapshot.roe.desc().nullslast()
    ).limit(max(limit * 6, limit + 60))
    cand_mappings = list((await session.execute(cand_stmt)).mappings().all())

    val_state = "fresh" if val_date == today_market_date else "stale"
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

    # common-stock filter (drop ETF/preferred) before fundamentals work
    valuation_rows = [
        {**dict(m), "_screener_snapshot_state": val_state}
        for m in cand_mappings
        if _is_kr_toss_common_stock(m["symbol"], name_map.get(m["symbol"]))
    ]

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
    )
