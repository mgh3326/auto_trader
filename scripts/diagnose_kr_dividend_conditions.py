"""ROB-444 follow-up: binding-condition diagnostic for the KR dividend presets.

Why: after the DART-first hybrid (#1166) + backfill, steady_dividend stayed at 15
and future_dividend_king at 5 (vs Toss 27/14). Coverage is no longer the binding
constraint (operator: payout/dps ~95%, 2396 symbols with 3y depth). This read-only
script reports, for each dividend preset, how many candidates pass EACH condition
individually and the cumulative funnel — so we can see WHICH condition limits the
count and compare it to Toss's filter definition.

Mirrors ``kr_fundamentals_tv_screener._passes_thresholds`` exactly (DART-first
payout/streak, dividend_yield ÷100 per #1165, earnings-streak skip-if-absent). It
does NOT write anything. Run on prod (read-only):

    uv run python -m scripts.diagnose_kr_dividend_conditions
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.derive import (
    derive_fundamentals_metrics,
)
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
)
from app.services.invest_screener_snapshots.freshness import today_trading_date
from app.services.invest_view_model.fundamentals_screener import (
    FUTURE_DIVIDEND_KING_SPEC,
    STEADY_DIVIDEND_SPEC,
    _to_period,
)


def _dart_metric(dart: Any, attr: str):
    """Return the DART metric value if state=='ok', else None (mirrors loader)."""
    if dart is None:
        return None
    dm = getattr(dart, attr, None)
    if dm is not None and dm.state == "ok" and dm.value is not None:
        return dm.value
    return None


def _dart_first(dart: Any, attr: str, snap: Any, col: str):
    """DART-first value with tvscreener column fallback (mirrors _DIVIDEND_DART_CHECKS)."""
    v = _dart_metric(dart, attr)
    if v is not None:
        return v, "dart"
    fv = getattr(snap, col, None)
    return fv, ("tvscreener" if fv is not None else None)


def _ge(value: Any, threshold: Decimal) -> bool:
    return value is not None and Decimal(str(value)) >= threshold


def _conditions(spec, snap, dart) -> list[tuple[str, bool]]:
    """Per-condition (name, passed) for a dividend preset — same logic as the loader."""
    out: list[tuple[str, bool]] = []
    if spec.min_dividend_yield is not None:
        # PR1 (#1165): tvscreener dividend_yield is PERCENT → ÷100 to compare ratio.
        dy = snap.dividend_yield
        ok = (
            dy is not None
            and (Decimal(str(dy)) / Decimal("100")) >= spec.min_dividend_yield
        )
        out.append((f"dividend_yield>={spec.min_dividend_yield * 100:g}%", ok))
    if spec.min_payout_ratio is not None:
        v, _ = _dart_first(dart, "payout_ratio", snap, "payout_ratio_ttm")
        out.append(
            (f"payout_ratio>={spec.min_payout_ratio:g}", _ge(v, spec.min_payout_ratio))
        )
    if spec.min_dividend_paid_streak_years is not None:
        v, _ = _dart_first(
            dart, "dividend_paid_streak_years", snap, "continuous_dividend_payout"
        )
        out.append(
            (
                f"div_paid_streak>={spec.min_dividend_paid_streak_years}",
                _ge(v, Decimal(str(spec.min_dividend_paid_streak_years))),
            )
        )
    if spec.min_dividend_growth_streak_years is not None:
        v, _ = _dart_first(
            dart, "dividend_growth_streak_years", snap, "continuous_dividend_growth"
        )
        out.append(
            (
                f"div_growth_streak>={spec.min_dividend_growth_streak_years}",
                _ge(v, Decimal(str(spec.min_dividend_growth_streak_years))),
            )
        )
    if spec.min_earnings_increase_streak_years is not None:
        # DART-first SKIP: absent DART → skipped → passes (loader 316-342).
        dm_v = _dart_metric(dart, "earnings_increase_streak_years")
        ok = dm_v is None or Decimal(str(dm_v)) >= Decimal(
            str(spec.min_earnings_increase_streak_years)
        )
        out.append(
            (f"earnings_streak>={spec.min_earnings_increase_streak_years}(skip-ok)", ok)
        )
    return out


async def _diagnose(session, spec) -> None:
    now = dt.datetime.now(dt.UTC)
    report_date = today_trading_date("kr", now=now)

    latest = (
        await session.execute(
            sa.select(sa.func.max(InvestKrFundamentalsSnapshot.snapshot_date))
        )
    ).scalar_one_or_none()
    if latest is None:
        print(f"[{spec.preset_id}] no KR fundamentals snapshot partition")
        return
    snaps = list(
        (
            await session.execute(
                sa.select(InvestKrFundamentalsSnapshot).where(
                    InvestKrFundamentalsSnapshot.snapshot_date == latest
                )
            )
        )
        .scalars()
        .all()
    )
    symbols = [s.symbol for s in snaps]
    period_rows = await FinancialFundamentalsSnapshotsRepository(
        session
    ).latest_periods_for_symbols(market="kr", symbols=symbols)
    dart_by_symbol = {
        sym: derive_fundamentals_metrics(
            [_to_period(r) for r in rows], report_date=report_date
        )
        for sym, rows in period_rows.items()
    }

    cond_names: list[str] = []
    individual: dict[str, int] = {}
    survivors = list(snaps)
    funnel: list[tuple[str, int]] = []

    # individual pass counts (each condition over the full candidate set)
    for snap in snaps:
        for name, ok in _conditions(spec, snap, dart_by_symbol.get(snap.symbol)):
            if name not in individual:
                individual[name] = 0
                cond_names.append(name)
            if ok:
                individual[name] += 1

    # cumulative funnel (apply conditions in declared order)
    for idx, name in enumerate(cond_names):
        survivors = [
            s
            for s in survivors
            if _conditions(spec, s, dart_by_symbol.get(s.symbol))[idx][1]
        ]
        funnel.append((name, len(survivors)))

    print(f"\n=== {spec.preset_id} | partition={latest} | candidates={len(snaps)} ===")
    print("  individual pass (each condition over all candidates):")
    for name in cond_names:
        print(f"    {name:32} {individual[name]:5}")
    print("  cumulative funnel (declared order):")
    for name, n in funnel:
        print(f"    + {name:30} {n:5}")
    if cond_names:
        binding = min(cond_names, key=lambda n: individual[n])
        print(f"  most-restrictive (individual): {binding} ({individual[binding]})")
    print(f"  FINAL (all conditions): {funnel[-1][1] if funnel else len(snaps)}")


async def main() -> int:
    async with AsyncSessionLocal() as session:
        for spec in (STEADY_DIVIDEND_SPEC, FUTURE_DIVIDEND_KING_SPEC):
            await _diagnose(session, spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
