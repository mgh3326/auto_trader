"""ROB-112 — Research pipeline service."""

from sqlalchemy.ext.asyncio import AsyncSession
from app.analysis.pipeline import run_research_session

class ResearchPipelineService:
    """Service to wrap and export research pipeline functionality."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def run_session(
        self,
        symbol: str,
        name: str,
        instrument_type: str,
        research_run_id: int | None = None,
        user_id: int | None = None,
    ) -> int:
        """
        Runs a research session for the given symbol.
        """
        return await run_research_session(
            db=self.db,
            symbol=symbol,
            name=name,
            instrument_type=instrument_type,
            research_run_id=research_run_id,
            user_id=user_id,
        )
