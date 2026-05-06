"""ROB-121 — Read-only aggregation service for the retrospective page.

NEVER writes to the database. NEVER triggers brokers / scheduler / outbox.

Aggregates over (research_summaries, summary_stage_links, stage_analysis,
trading_decision_proposals, trading_decision_outcomes, trading_decision_sessions).

`market` filter maps to stock_info.instrument_type / proposal.instrument_type:
  KR -> equity_kr
  US -> equity_us
  CRYPTO -> crypto
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockInfo
from app.models.research_pipeline import (
    ResearchSession,
    ResearchSummary,
    StageAnalysis,
    SummaryStageLink,
)
from app.models.trading_decision import (
    TradingDecisionOutcome,
    TradingDecisionProposal,
    TradingDecisionSession,
)
from app.schemas.research_retrospective import (
    DecisionDistribution,
    Market,
    PnlSummary,
    RetrospectiveDecisionRow,
    RetrospectiveDecisionsResponse,
    RetrospectiveOverview,
    StageCoverageStat,
    StagePerformanceRow,
)

_MARKET_TO_INSTRUMENT: dict[str, str] = {
    "KR": "equity_kr",
    "US": "equity_us",
    "CRYPTO": "crypto",
}
_STAGES: tuple[str, ...] = ("market", "news", "fundamentals", "social")


def _market_label(instrument_type: str) -> Market:
    if instrument_type == "equity_kr":
        return "KR"
    if instrument_type == "equity_us":
        return "US"
    return "CRYPTO"


class ResearchRetrospectiveService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_overview(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
    ) -> RetrospectiveOverview:
        warnings: list[str] = []

        summary_rows = (
            await self.db.execute(self._summary_query(period_start, period_end, market))
        ).all()

        summaries_total = len(summary_rows)
        sessions_total = len({row.session_id for row in summary_rows})
        decision_counts: Counter[str] = Counter(row.decision for row in summary_rows)

        if summaries_total == 0:
            warnings.append("no_research_summaries_in_window")

        proposal_responses = await self._proposal_response_counts(
            period_start, period_end, market
        )

        distribution = DecisionDistribution(
            ai_buy=decision_counts.get("buy", 0),
            ai_hold=decision_counts.get("hold", 0),
            ai_sell=decision_counts.get("sell", 0),
            user_accept=proposal_responses.get("accept", 0),
            user_reject=proposal_responses.get("reject", 0),
            user_modify=proposal_responses.get("modify", 0)
            + proposal_responses.get("partial_accept", 0),
            user_defer=proposal_responses.get("defer", 0),
            user_pending=proposal_responses.get("pending", 0),
        )

        stage_stats = await self._stage_coverage(
            summary_rows=summary_rows,
        )
        pnl = await self._pnl_summary(period_start, period_end, market)

        return RetrospectiveOverview(
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            market=market,
            strategy=strategy,
            sessions_total=sessions_total,
            summaries_total=summaries_total,
            decision_distribution=distribution,
            stage_coverage=stage_stats,
            pnl=pnl,
            warnings=warnings,
        )

    async def build_stage_performance(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
    ) -> list[StagePerformanceRow]:
        del strategy  # reserved for future strategy_name filter

        rows = (
            await self.db.execute(self._summary_query(period_start, period_end, market))
        ).all()
        if not rows:
            return []

        summary_ids = [row.summary_id for row in rows]
        link_rows = (
            await self.db.execute(
                select(SummaryStageLink.summary_id, StageAnalysis.stage_type)
                .join(
                    StageAnalysis,
                    SummaryStageLink.stage_analysis_id == StageAnalysis.id,
                )
                .where(SummaryStageLink.summary_id.in_(summary_ids))
            )
        ).all()

        combos: dict[int, set[str]] = {}
        for sid, stage in link_rows:
            combos.setdefault(int(sid), set()).add(str(stage))

        outcomes = await self._outcomes_by_session(period_start, period_end, market)

        groups: dict[str, list[float]] = {}
        for row in rows:
            stages = sorted(combos.get(row.summary_id, set()))
            key = "+".join(stages) if stages else "no_stages"
            pnls = outcomes.get(int(row.session_id), [])
            groups.setdefault(key, []).extend(pnls)

        out: list[StagePerformanceRow] = []
        for combo, pnls in groups.items():
            sample = len(pnls)
            win_rate = (
                sum(1 for v in pnls if v > 0) / sample * 100.0 if sample else None
            )
            avg = sum(pnls) / sample if sample else None
            out.append(
                StagePerformanceRow(
                    stage_combo=combo,
                    sample_size=sample,
                    win_rate_pct=win_rate,
                    avg_realized_pnl_pct=avg,
                )
            )
        out.sort(key=lambda r: -r.sample_size)
        return out

    async def list_decisions(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
        strategy: str | None,
        limit: int,
    ) -> RetrospectiveDecisionsResponse:
        del strategy

        stmt = (
            select(
                ResearchSummary.id.label("summary_id"),
                ResearchSession.id.label("session_id"),
                StockInfo.symbol,
                StockInfo.instrument_type,
                ResearchSummary.executed_at,
                ResearchSummary.decision,
                TradingDecisionProposal.id.label("proposal_id"),
                TradingDecisionProposal.user_response,
            )
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .join(
                TradingDecisionProposal,
                TradingDecisionProposal.symbol == StockInfo.symbol,
                isouter=True,
            )
            .where(
                ResearchSummary.executed_at >= period_start,
                ResearchSummary.executed_at < period_end,
            )
            .order_by(desc(ResearchSummary.executed_at))
            .limit(limit)
        )
        if market is not None:
            stmt = stmt.where(
                StockInfo.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )

        rows = (await self.db.execute(stmt)).all()
        proposal_ids = [int(r.proposal_id) for r in rows if r.proposal_id is not None]
        outcome_pnls = await self._final_pnl_by_proposal(proposal_ids)

        result_rows: list[RetrospectiveDecisionRow] = [
            RetrospectiveDecisionRow(
                research_session_id=int(r.session_id),
                symbol=str(r.symbol),
                market=_market_label(str(r.instrument_type)),
                decided_at=r.executed_at.isoformat(),
                ai_decision=r.decision,
                user_response=str(r.user_response) if r.user_response else None,
                realized_pnl_pct=outcome_pnls.get(int(r.proposal_id))
                if r.proposal_id is not None
                else None,
                proposal_id=int(r.proposal_id) if r.proposal_id is not None else None,
            )
            for r in rows
        ]

        return RetrospectiveDecisionsResponse(
            total=len(result_rows),
            rows=result_rows,
        )

    # ---------- private helpers ----------

    def _summary_query(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> Any:
        stmt = (
            select(
                ResearchSummary.id.label("summary_id"),
                ResearchSummary.session_id,
                ResearchSummary.decision,
                ResearchSummary.executed_at,
            )
            .join(ResearchSession, ResearchSummary.session_id == ResearchSession.id)
            .join(StockInfo, ResearchSession.stock_info_id == StockInfo.id)
            .where(
                ResearchSummary.executed_at >= period_start,
                ResearchSummary.executed_at < period_end,
            )
        )
        if market is not None:
            stmt = stmt.where(
                StockInfo.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        return stmt

    async def _proposal_response_counts(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> dict[str, int]:
        stmt = (
            select(TradingDecisionProposal.user_response, func.count())
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
            )
            .group_by(TradingDecisionProposal.user_response)
        )
        if market is not None:
            stmt = stmt.where(
                TradingDecisionProposal.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        rows = (await self.db.execute(stmt)).all()
        return {str(response): int(count) for response, count in rows}

    async def _stage_coverage(
        self,
        *,
        summary_rows: list[Any],
    ) -> list[StageCoverageStat]:
        sessions_total = len({row.session_id for row in summary_rows})
        if sessions_total == 0:
            return [
                StageCoverageStat(
                    stage_type=s,  # type: ignore[arg-type]
                    coverage_pct=0.0,
                    stale_pct=0.0,
                    unavailable_pct=0.0,
                )
                for s in _STAGES
            ]
        summary_ids = [row.summary_id for row in summary_rows]
        stage_rows = (
            await self.db.execute(
                select(
                    StageAnalysis.stage_type,
                    StageAnalysis.verdict,
                    StageAnalysis.source_freshness,
                    SummaryStageLink.summary_id,
                )
                .join(
                    SummaryStageLink,
                    SummaryStageLink.stage_analysis_id == StageAnalysis.id,
                )
                .where(SummaryStageLink.summary_id.in_(summary_ids))
            )
        ).all()

        per_stage: dict[str, dict[str, int]] = {}
        for stage, verdict, freshness, _summary_id in stage_rows:
            bucket = per_stage.setdefault(
                str(stage), {"covered": 0, "stale": 0, "unavailable": 0}
            )
            bucket["covered"] += 1
            if verdict == "unavailable":
                bucket["unavailable"] += 1
            stale_flags = (
                freshness.get("stale_flags") if isinstance(freshness, dict) else None
            )
            if stale_flags:
                bucket["stale"] += 1

        out: list[StageCoverageStat] = []
        denom = sessions_total
        for stage in _STAGES:
            b = per_stage.get(stage, {"covered": 0, "stale": 0, "unavailable": 0})
            out.append(
                StageCoverageStat(
                    stage_type=stage,  # type: ignore[arg-type]
                    coverage_pct=b["covered"] / denom * 100.0,
                    stale_pct=b["stale"] / denom * 100.0,
                    unavailable_pct=b["unavailable"] / denom * 100.0,
                )
            )
        return out

    async def _pnl_summary(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> PnlSummary:
        stmt = (
            select(TradingDecisionOutcome.pnl_pct, TradingDecisionOutcome.horizon)
            .join(
                TradingDecisionProposal,
                TradingDecisionOutcome.proposal_id == TradingDecisionProposal.id,
            )
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
            )
        )
        if market is not None:
            stmt = stmt.where(
                TradingDecisionProposal.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        rows = (await self.db.execute(stmt)).all()
        realized = [
            float(pnl)
            for pnl, horizon in rows
            if horizon == "final" and pnl is not None
        ]
        unrealized = [
            float(pnl)
            for pnl, horizon in rows
            if horizon != "final" and pnl is not None
        ]
        return PnlSummary(
            realized_pnl_pct_avg=(sum(realized) / len(realized)) if realized else None,
            unrealized_pnl_pct_avg=(sum(unrealized) / len(unrealized))
            if unrealized
            else None,
            sample_size=len(rows),
        )

    async def _outcomes_by_session(
        self,
        period_start: datetime,
        period_end: datetime,
        market: Market | None,
    ) -> dict[int, list[float]]:
        stmt = (
            select(
                ResearchSession.id.label("session_id"),
                TradingDecisionOutcome.pnl_pct,
            )
            .join(
                TradingDecisionProposal,
                TradingDecisionOutcome.proposal_id == TradingDecisionProposal.id,
            )
            .join(
                TradingDecisionSession,
                TradingDecisionProposal.session_id == TradingDecisionSession.id,
            )
            .join(StockInfo, StockInfo.symbol == TradingDecisionProposal.symbol)
            .join(ResearchSession, ResearchSession.stock_info_id == StockInfo.id)
            .where(
                TradingDecisionSession.generated_at >= period_start,
                TradingDecisionSession.generated_at < period_end,
                TradingDecisionOutcome.horizon == "final",
                TradingDecisionOutcome.pnl_pct.is_not(None),
            )
        )
        if market is not None:
            stmt = stmt.where(
                TradingDecisionProposal.instrument_type == _MARKET_TO_INSTRUMENT[market]
            )
        rows = (await self.db.execute(stmt)).all()
        out: dict[int, list[float]] = {}
        for session_id, pnl in rows:
            out.setdefault(int(session_id), []).append(float(pnl))
        return out

    async def _final_pnl_by_proposal(self, proposal_ids: list[int]) -> dict[int, float]:
        if not proposal_ids:
            return {}
        stmt = select(
            TradingDecisionOutcome.proposal_id, TradingDecisionOutcome.pnl_pct
        ).where(
            TradingDecisionOutcome.proposal_id.in_(proposal_ids),
            TradingDecisionOutcome.horizon == "final",
            TradingDecisionOutcome.pnl_pct.is_not(None),
        )
        rows = (await self.db.execute(stmt)).all()
        return {int(pid): float(pnl) for pid, pnl in rows}
