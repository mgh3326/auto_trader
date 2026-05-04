from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_decision import TradingDecisionSession, WorkflowStatus


class CommitteeSessionService:
    @staticmethod
    async def update_workflow_status(
        session: AsyncSession,
        *,
        session_uuid: UUID,
        user_id: int,
        status: WorkflowStatus,
    ) -> TradingDecisionSession | None:
        """Update workflow status of a committee session."""
        stmt = select(TradingDecisionSession).where(
            TradingDecisionSession.session_uuid == session_uuid,
            TradingDecisionSession.user_id == user_id,
        )
        result = await session.execute(stmt)
        db_session = result.scalar_one_or_none()
        if not db_session:
            return None

        db_session.workflow_status = status
        await session.flush()
        await session.refresh(db_session)
        return db_session

    @staticmethod
    async def update_committee_artifacts(
        session: AsyncSession,
        *,
        session_uuid: UUID,
        user_id: int,
        artifacts_patch: dict,
    ) -> TradingDecisionSession | None:
        """Patch committee artifacts (JSONB merge)."""
        stmt = select(TradingDecisionSession).where(
            TradingDecisionSession.session_uuid == session_uuid,
            TradingDecisionSession.user_id == user_id,
        )
        result = await session.execute(stmt)
        db_session = result.scalar_one_or_none()
        if not db_session:
            return None

        current = db_session.artifacts or {}
        # Simple top-level merge for MVP.
        # Deep merge could be used if needed, but the schemas are fairly flat.
        updated = {**current, **artifacts_patch}
        db_session.artifacts = updated

        db_session.updated_at = datetime.now(db_session.updated_at.tzinfo)
        await session.flush()
        await session.refresh(db_session)
        return db_session
