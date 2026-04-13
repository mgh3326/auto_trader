"""Paper Journal Bridge — 전략 비교 및 실전 전환 추천.

Paper 주문→journal 연동(create/close)은 order_journal.py가 담당한다.
이 모듈은 journal 데이터를 기반으로 한 분석/추천 기능만 책임진다.

집계 규칙 (고정):
- 모든 지표(win_rate, total_return_pct, avg_pnl_pct, best/worst_trade)는
  **closed** journal 기준으로만 계산 (realized performance).
- active journal은 집계에서 제외.
- total_return_pct는 closed journal들의 pnl_pct 합산 (realized 기준).
- 집계 단위는 paper account 기준. strategy_name은 필터/표시 역할.
- strategy_name 필터는 TradeJournal.strategy 기준 (PaperAccount.strategy_name 아님).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.paper_trading import PaperAccount
from app.models.trade_journal import JournalStatus, TradeJournal

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def compare_strategies(
    days: int = 30,
    strategy_name: str | None = None,
    include_live_comparison: bool = True,
) -> dict[str, Any]:
    """Compare paper trading strategy performance over a given period.

    Shows per-account/per-strategy metrics such as win rate, realized return,
    and best/worst trade. All metrics are based on closed journals only.
    If include_live_comparison=True, also compares same-symbol live vs paper
    journal outcomes within the same period.
    """
    cutoff = now_kst() - timedelta(days=days)

    try:
        async with _session_factory()() as db:
            # 1. Paper accounts for metadata
            acct_stmt = select(PaperAccount).where(PaperAccount.is_active.is_(True))
            acct_result = await db.execute(acct_stmt)
            accounts = {a.name: a for a in acct_result.scalars().all()}

            # 2. Paper journals in period
            paper_filters: list = [
                TradeJournal.account_type == "paper",
                TradeJournal.created_at >= cutoff,
            ]
            if strategy_name is not None:
                paper_filters.append(TradeJournal.strategy == strategy_name)

            paper_stmt = (
                select(TradeJournal)
                .where(*paper_filters)
                .order_by(desc(TradeJournal.created_at))
            )
            paper_result = await db.execute(paper_stmt)
            paper_journals = list(paper_result.scalars().all())

            # 3. Aggregate by account (closed only)
            by_account: dict[str, list[TradeJournal]] = defaultdict(list)
            for j in paper_journals:
                if j.status == JournalStatus.closed and j.account:
                    by_account[j.account].append(j)

            strategies_out: list[dict[str, Any]] = []
            for account_name, journals in by_account.items():
                acct = accounts.get(account_name)
                total = len(journals)
                pnl_values = [
                    float(j.pnl_pct) for j in journals if j.pnl_pct is not None
                ]
                win_count = sum(1 for v in pnl_values if v > 0)
                loss_count = total - win_count
                total_return_pct = round(sum(pnl_values), 2) if pnl_values else 0.0
                avg_pnl_pct = (
                    round(total_return_pct / len(pnl_values), 2) if pnl_values else 0.0
                )
                win_rate = round(win_count / total * 100, 1) if total > 0 else 0.0

                best = max(journals, key=lambda j: float(j.pnl_pct or 0))
                worst = min(journals, key=lambda j: float(j.pnl_pct or 0))

                strategies_out.append({
                    "strategy_name": acct.strategy_name if acct else None,
                    "account_name": account_name,
                    "account_id": acct.id if acct else None,
                    "total_trades": total,
                    "win_count": win_count,
                    "loss_count": loss_count,
                    "win_rate": win_rate,
                    "total_return_pct": total_return_pct,
                    "avg_pnl_pct": avg_pnl_pct,
                    "best_trade": {
                        "symbol": best.symbol,
                        "pnl_pct": float(best.pnl_pct) if best.pnl_pct else 0.0,
                    },
                    "worst_trade": {
                        "symbol": worst.symbol,
                        "pnl_pct": float(worst.pnl_pct) if worst.pnl_pct else 0.0,
                    },
                })

            # 4. Live vs paper comparison (most recent closed per symbol)
            live_vs_paper: list[dict[str, Any]] = []
            if include_live_comparison:
                live_stmt = (
                    select(TradeJournal)
                    .where(
                        TradeJournal.account_type == "live",
                        TradeJournal.status == JournalStatus.closed,
                        TradeJournal.created_at >= cutoff,
                    )
                    .order_by(desc(TradeJournal.created_at))
                )
                live_result = await db.execute(live_stmt)
                live_journals = list(live_result.scalars().all())

                # Most recent closed per symbol
                live_by_symbol: dict[str, TradeJournal] = {}
                for j in live_journals:
                    if j.symbol not in live_by_symbol:
                        live_by_symbol[j.symbol] = j

                paper_closed = [
                    j for j in paper_journals if j.status == JournalStatus.closed
                ]
                paper_by_symbol: dict[str, TradeJournal] = {}
                for j in paper_closed:
                    if j.symbol not in paper_by_symbol:
                        paper_by_symbol[j.symbol] = j

                for sym in sorted(set(live_by_symbol) & set(paper_by_symbol)):
                    lj = live_by_symbol[sym]
                    pj = paper_by_symbol[sym]
                    l_pnl = float(lj.pnl_pct) if lj.pnl_pct is not None else 0.0
                    p_pnl = float(pj.pnl_pct) if pj.pnl_pct is not None else 0.0
                    live_vs_paper.append({
                        "symbol": sym,
                        "live_entry_price": (
                            float(lj.entry_price) if lj.entry_price else None
                        ),
                        "live_pnl_pct": l_pnl,
                        "paper_entry_price": (
                            float(pj.entry_price) if pj.entry_price else None
                        ),
                        "paper_pnl_pct": p_pnl,
                        "paper_strategy": pj.strategy,
                        "delta_pnl_pct": round(p_pnl - l_pnl, 4),
                    })

            return {
                "success": True,
                "period_days": days,
                "strategies": strategies_out,
                "live_vs_paper": live_vs_paper,
            }
    except Exception as exc:
        logger.exception("compare_strategies failed")
        return {"success": False, "error": f"compare_strategies failed: {exc}"}
