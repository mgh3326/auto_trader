"""ROB-405 Slice D — per-cycle (kst_date) retrospective aggregation for the
mock autonomous loop. Read-only. Anchors triggered/filled/PnL/verdict/CF to
each day's watch-event correlation_ids; armed is bucketed by alert created_at
(KST) as a newly-armed proxy (alerts carry no correlation_id/kst_date)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func as safunc
from sqlalchemy import select

from app.core.timezone import to_kst_naive
from app.models.investment_reports import InvestmentWatchAlert, InvestmentWatchEvent
from app.models.review import TradeJournalCounterfactual, TradeJournalReview
from app.models.trade_journal import TradeJournal


def _iter_days(from_str: str, to_str: str):
    d0 = date.fromisoformat(from_str)
    d1 = date.fromisoformat(to_str)
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _avg(values: list) -> float | None:
    nums: list[Decimal] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(Decimal(str(v)))
        except (InvalidOperation, TypeError, ValueError):
            continue
    if not nums:
        return None
    return float(sum(nums) / len(nums))


async def build_mock_loop_retrospective(
    db, *, kst_date_from: str, kst_date_to: str, market: str | None = None
) -> list[dict[str, Any]]:
    """Per-day cycle summary over [kst_date_from, kst_date_to] (inclusive)."""
    # armed: bucket all (market-filtered) alerts by created_at KST date once.
    al_stmt = select(InvestmentWatchAlert)
    if market:
        al_stmt = al_stmt.where(InvestmentWatchAlert.market == market)
    armed_by_day: dict[str, int] = {}
    for a in (await db.execute(al_stmt)).scalars().all():
        d = to_kst_naive(a.created_at).date().isoformat()
        armed_by_day[d] = armed_by_day.get(d, 0) + 1

    cycles: list[dict[str, Any]] = []
    for day in _iter_days(kst_date_from, kst_date_to):
        ev_stmt = select(InvestmentWatchEvent).where(
            InvestmentWatchEvent.kst_date == day
        )
        if market:
            ev_stmt = ev_stmt.where(InvestmentWatchEvent.market == market)
        events = (await db.execute(ev_stmt)).scalars().all()
        corr_ids = [e.correlation_id for e in events if e.correlation_id]
        by_outcome: dict[str, int] = {}
        for e in events:
            by_outcome[e.outcome] = by_outcome.get(e.outcome, 0) + 1

        journals: list[TradeJournal] = []
        if corr_ids:
            journals = (
                await db.execute(
                    select(TradeJournal).where(
                        TradeJournal.account_type == "mock",
                        TradeJournal.correlation_id.in_(corr_ids),
                        TradeJournal.status.in_(("active", "closed")),
                    )
                )
            ).scalars().all()
        closed = [
            j for j in journals if j.status == "closed" and j.pnl_pct is not None
        ]
        hits = sum(1 for j in closed if j.pnl_pct > 0)
        misses = sum(1 for j in closed if j.pnl_pct <= 0)
        hit_ratio = (hits / (hits + misses)) if (hits + misses) > 0 else None

        verdict = {"good": 0, "neutral": 0, "bad": 0}
        journal_ids = [j.id for j in journals]
        if journal_ids:
            for v, cnt in (
                await db.execute(
                    select(TradeJournalReview.verdict, safunc.count())
                    .where(TradeJournalReview.journal_id.in_(journal_ids))
                    .group_by(TradeJournalReview.verdict)
                )
            ).all():
                if v in verdict:
                    verdict[v] = int(cnt)

        cf_ft: list = []
        cf_naf: list = []
        cf_count = 0
        if corr_ids:
            cfs = (
                await db.execute(
                    select(TradeJournalCounterfactual).where(
                        TradeJournalCounterfactual.correlation_id.in_(corr_ids)
                    )
                )
            ).scalars().all()
            cf_count = len(cfs)
            cf_ft = [c.fill_vs_trigger_pct for c in cfs]
            cf_naf = [c.no_action_vs_fill_pct for c in cfs]

        cycles.append(
            {
                "kst_date": day,
                "armed": armed_by_day.get(day, 0),
                "triggered": len(events),
                "by_outcome": by_outcome,
                "filled": len(journals),
                "closed": len(closed),
                "avg_pnl_pct": _avg([j.pnl_pct for j in closed]),
                "hits": hits,
                "misses": misses,
                "hit_ratio": hit_ratio,
                "verdict": verdict,
                "counterfactual": {
                    "count": cf_count,
                    "avg_fill_vs_trigger_pct": _avg(cf_ft),
                    "avg_no_action_vs_fill_pct": _avg(cf_naf),
                },
            }
        )
    return cycles
