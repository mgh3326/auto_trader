"""ROB-116 — Read-only aggregator for the holdings action board.

Combines:
  - MergedPortfolioService (KIS + manual holdings, KR + US)
  - Upbit balances via existing helpers (CRYPTO)
  - ResearchPipelineService latest summary (decision/verdict/levels)
  - PortfolioDashboardService journal snapshot (journal_status)

NEVER mutates broker state. NEVER writes order-intent / watch / trade rows.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_pipeline import ResearchSession, ResearchSummary
from app.schemas.portfolio_actions import (
    PortfolioActionCandidate,
    PortfolioActionsResponse,
)
from app.services.portfolio_action_classifier import (
    ClassifierInputs,
    classify_position,
)


class PortfolioActionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_action_board(
        self,
        *,
        user_id: int,
        market_filter: str | None = None,
    ) -> PortfolioActionsResponse:
        holdings, total_value, load_warnings = await self._load_holdings(
            user_id, market_filter
        )
        candidates: list[PortfolioActionCandidate] = []
        warnings: list[str] = list(load_warnings)

        for holding in holdings:
            quantity = _holding_quantity(holding)
            if quantity <= 0.0:
                continue

            symbol = str(getattr(holding, "ticker", "") or "")
            if not symbol:
                continue

            evaluation = float(getattr(holding, "evaluation", 0.0) or 0.0)
            weight = (evaluation / total_value * 100.0) if total_value else None
            market_value = _normalize_market(getattr(holding, "market_type", None))
            profit_rate = getattr(holding, "profit_loss_rate", None)
            if profit_rate is None:
                profit_rate = getattr(holding, "profit_rate", None)

            summary = await self._load_latest_summary(symbol)
            journal_status = await self._load_journal_status(symbol)

            inputs = ClassifierInputs(
                symbol=symbol,
                position_weight_pct=weight,
                profit_rate=float(profit_rate) if profit_rate is not None else None,
                summary_decision=(summary or {}).get("decision"),
                summary_confidence=(summary or {}).get("confidence"),
                market_verdict=(summary or {}).get("market_verdict"),
                nearest_support_pct=(summary or {}).get("nearest_support_pct"),
                nearest_resistance_pct=(summary or {}).get("nearest_resistance_pct"),
                journal_status=journal_status,
                sellable_quantity=getattr(holding, "sellable_quantity", None),
                staked_quantity=getattr(holding, "staked_quantity", None),
            )
            verdict = classify_position(inputs)

            candidates.append(
                PortfolioActionCandidate(
                    symbol=symbol,
                    name=getattr(holding, "name", None),
                    market=market_value,
                    instrument_type=getattr(holding, "instrument_type", None),
                    position_weight_pct=weight,
                    profit_rate=inputs.profit_rate,
                    quantity=quantity,
                    sellable_quantity=inputs.sellable_quantity,
                    staked_quantity=inputs.staked_quantity,
                    latest_research_session_id=(summary or {}).get("session_id"),
                    summary_decision=inputs.summary_decision,
                    summary_confidence=inputs.summary_confidence,
                    market_verdict=inputs.market_verdict,
                    nearest_support_pct=inputs.nearest_support_pct,
                    nearest_resistance_pct=inputs.nearest_resistance_pct,
                    journal_status=journal_status,
                    candidate_action=verdict.candidate_action,
                    suggested_trim_pct=verdict.suggested_trim_pct,
                    reason_codes=verdict.reason_codes,
                    missing_context_codes=verdict.missing_context_codes,
                )
            )

        return PortfolioActionsResponse(
            generated_at=datetime.now(UTC).isoformat(),
            total=len(candidates),
            candidates=candidates,
            warnings=warnings,
        )

    async def _load_holdings(
        self, user_id: int, market_filter: str | None
    ) -> tuple[list[Any], float, list[str]]:
        from app.services.brokers.kis import KISClient
        from app.services.merged_portfolio_service import MergedPortfolioService

        service = MergedPortfolioService(self.db)
        holdings: list[Any] = []
        warnings: list[str] = []

        if market_filter in (None, "KR"):
            try:
                holdings.extend(
                    await service.get_merged_portfolio_domestic(user_id, KISClient())
                )
            except Exception as exc:
                warnings.append(f"KR holdings unavailable: {type(exc).__name__}")
        if market_filter in (None, "US"):
            try:
                holdings.extend(
                    await service.get_merged_portfolio_overseas(user_id, KISClient())
                )
            except Exception as exc:
                warnings.append(f"US holdings unavailable: {type(exc).__name__}")
        if market_filter in (None, "CRYPTO"):
            holdings.extend(await self._load_crypto_holdings(user_id))

        total = sum(float(getattr(h, "evaluation", 0.0) or 0.0) for h in holdings)
        return holdings, total, warnings

    async def _load_crypto_holdings(self, user_id: int) -> list[Any]:
        try:
            from app.services.upbit_holdings_service import (
                fetch_upbit_holdings_for_user,
            )
        except ImportError:
            return []
        try:
            return await fetch_upbit_holdings_for_user(self.db, user_id)
        except Exception:
            return []

    async def _load_latest_summary(self, symbol: str) -> dict[str, Any] | None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.models.analysis import StockInfo

        stmt = (
            select(ResearchSession)
            .join(ResearchSession.stock_info)
            .where(StockInfo.symbol == symbol)
            .options(selectinload(ResearchSession.summaries))
            .order_by(ResearchSession.created_at.desc())
            .limit(1)
        )
        row = (await self.db.execute(stmt)).scalars().first()
        if row is None or not row.summaries:
            return None

        summary: ResearchSummary = row.summaries[-1]
        price_analysis = summary.price_analysis or {}
        return {
            "session_id": row.id,
            "decision": summary.decision,
            "confidence": summary.confidence,
            "market_verdict": price_analysis.get("market_verdict"),
            "nearest_support_pct": price_analysis.get("nearest_support_pct"),
            "nearest_resistance_pct": price_analysis.get("nearest_resistance_pct"),
        }

    async def _load_journal_status(self, symbol: str) -> str:
        try:
            from app.services.portfolio_dashboard_service import (
                PortfolioDashboardService,
            )
        except ImportError:
            return "missing"
        try:
            service = PortfolioDashboardService(self.db)
            snapshot = await service.get_latest_journal_snapshot(symbol)
        except Exception:
            return "missing"
        if snapshot is None:
            return "missing"
        return "present"


def _holding_quantity(value: Any) -> float:
    """Return the displayed position quantity for heterogeneous holding DTOs.

    MergedPortfolioService.MergedHolding exposes ``total_quantity`` instead of
    ``quantity``; Upbit/manual DTOs may expose ``quantity`` directly.
    """

    for attr in ("quantity", "total_quantity"):
        raw = getattr(value, attr, None)
        if raw is None:
            continue
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _normalize_market(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "KR").strip().lower()
    if text in {"crypto", "cr", "coin"}:
        return "CRYPTO"
    if text in {"us", "usa", "overseas"}:
        return "US"
    return "KR"
