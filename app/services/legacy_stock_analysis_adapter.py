from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockAnalysisResult
from app.schemas.research_pipeline import SummaryOutput


class LegacyStockAnalysisAdapter:
    """Adapter to map SummaryOutput to the legacy StockAnalysisResult model for dual-write."""

    async def write(
        self,
        db: AsyncSession,
        summary: SummaryOutput,
        summary_id: int,
        stock_info_id: int,
    ) -> StockAnalysisResult:
        """
        Map SummaryOutput to StockAnalysisResult and write to database.

        Args:
            db: Database session
            summary: The SummaryOutput from the research pipeline
            summary_id: The ID of the research summary record
            stock_info_id: The ID of the stock_info record

        Returns:
            The created StockAnalysisResult instance
        """
        # Extract price analysis fields safely
        pa = summary.price_analysis

        # Prepare mapping
        legacy_result = StockAnalysisResult(
            stock_info_id=stock_info_id,
            model_name=summary.model_name or "research_pipeline",
            decision=summary.decision.value if hasattr(summary.decision, "value") else str(summary.decision),
            confidence=summary.confidence,
            appropriate_buy_min=pa.appropriate_buy_min if pa else None,
            appropriate_buy_max=pa.appropriate_buy_max if pa else None,
            appropriate_sell_min=pa.appropriate_sell_min if pa else None,
            appropriate_sell_max=pa.appropriate_sell_max if pa else None,
            buy_hope_min=pa.buy_hope_min if pa else None,
            buy_hope_max=pa.buy_hope_max if pa else None,
            sell_target_min=pa.sell_target_min if pa else None,
            sell_target_max=pa.sell_target_max if pa else None,
            reasons=summary.reasons,
            detailed_text=summary.detailed_text,
            prompt=f"research_summary:{summary_id}/prompt_version:{summary.prompt_version}",
        )

        db.add(legacy_result)
        await db.flush()
        # We don't commit here as it's usually part of a larger transaction
        return legacy_result
