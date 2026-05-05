from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_decision import TradingDecisionSession, WorkflowStatus
from app.schemas.trading_decisions import (
    COMMITTEE_ALLOWED_ACCOUNT_MODES,
    COMMITTEE_SOURCE_PROFILE,
    CommitteeArtifacts,
)


class CommitteeWorkflowError(ValueError):
    """Raised when a committee workflow mutation violates safety rules."""


SIMULATION_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.created: frozenset(
        {WorkflowStatus.evidence_generating, WorkflowStatus.evidence_ready}
    ),
    WorkflowStatus.evidence_generating: frozenset(
        {WorkflowStatus.evidence_ready, WorkflowStatus.failed_evidence}
    ),
    WorkflowStatus.evidence_ready: frozenset({WorkflowStatus.debate_ready}),
    WorkflowStatus.debate_ready: frozenset({WorkflowStatus.trader_draft_ready}),
    WorkflowStatus.trader_draft_ready: frozenset(
        {WorkflowStatus.risk_review_ready, WorkflowStatus.failed_trader_draft}
    ),
    WorkflowStatus.risk_review_ready: frozenset(
        {
            WorkflowStatus.auto_approved,
            WorkflowStatus.failed_risk_review,
            WorkflowStatus.preview_blocked,
        }
    ),
    WorkflowStatus.auto_approved: frozenset({WorkflowStatus.preview_ready}),
    WorkflowStatus.preview_ready: frozenset(
        {WorkflowStatus.journal_ready, WorkflowStatus.preview_blocked}
    ),
    WorkflowStatus.journal_ready: frozenset({WorkflowStatus.completed}),
}

TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.completed,
        WorkflowStatus.failed_evidence,
        WorkflowStatus.failed_trader_draft,
        WorkflowStatus.failed_risk_review,
        WorkflowStatus.preview_blocked,
    }
)


class CommitteeSessionService:
    @staticmethod
    async def update_workflow_status(
        session: AsyncSession,
        *,
        session_uuid: UUID,
        user_id: int,
        status: WorkflowStatus,
    ) -> TradingDecisionSession | None:
        """Update workflow status after validating committee safety gates."""
        db_session = await CommitteeSessionService._get_committee_session(
            session, session_uuid=session_uuid, user_id=user_id
        )
        if not db_session:
            return None

        CommitteeSessionService._validate_workflow_transition(db_session, status)

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
        """Patch committee artifacts after validating the merged JSON shape."""
        db_session = await CommitteeSessionService._get_committee_session(
            session, session_uuid=session_uuid, user_id=user_id
        )
        if not db_session:
            return None

        CommitteeSessionService._ensure_mutable_committee_session(db_session)

        current = db_session.artifacts or {}
        # Simple top-level merge for MVP.
        # Deep merge could be used if needed, but the schemas are fairly flat.
        updated = {**current, **artifacts_patch}
        try:
            CommitteeArtifacts.model_validate(updated)
        except ValidationError as exc:
            raise CommitteeWorkflowError("Invalid committee artifacts payload") from exc

        db_session.artifacts = updated
        db_session.updated_at = datetime.now(
            db_session.updated_at.tzinfo if db_session.updated_at else UTC
        )
        await session.flush()
        await session.refresh(db_session)
        return db_session

    @staticmethod
    async def _get_committee_session(
        session: AsyncSession,
        *,
        session_uuid: UUID,
        user_id: int,
    ) -> TradingDecisionSession | None:
        stmt = select(TradingDecisionSession).where(
            TradingDecisionSession.session_uuid == session_uuid,
            TradingDecisionSession.user_id == user_id,
            TradingDecisionSession.source_profile == COMMITTEE_SOURCE_PROFILE,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _ensure_mutable_committee_session(db_session: TradingDecisionSession) -> None:
        if db_session.status != "open":
            raise CommitteeWorkflowError("Committee session is not open")
        if db_session.account_mode not in COMMITTEE_ALLOWED_ACCOUNT_MODES:
            raise CommitteeWorkflowError(
                "Committee workflow is only enabled for KIS mock / Alpaca paper"
            )

    @staticmethod
    def _validate_workflow_transition(
        db_session: TradingDecisionSession, next_status: WorkflowStatus
    ) -> None:
        CommitteeSessionService._ensure_mutable_committee_session(db_session)

        current_status = (
            WorkflowStatus(db_session.workflow_status)
            if db_session.workflow_status
            else WorkflowStatus.created
        )
        if current_status in TERMINAL_STATUSES:
            raise CommitteeWorkflowError(
                f"Committee workflow status {current_status.value!r} is terminal"
            )
        allowed = SIMULATION_TRANSITIONS.get(current_status, frozenset())
        if next_status not in allowed:
            raise CommitteeWorkflowError(
                f"Invalid committee workflow transition: "
                f"{current_status.value} -> {next_status.value}"
            )
        if next_status in {WorkflowStatus.auto_approved, WorkflowStatus.preview_ready}:
            risk_review = (db_session.artifacts or {}).get("risk_review") or {}
            if risk_review.get("verdict") == "vetoed":
                raise CommitteeWorkflowError(
                    "Risk review veto prevents auto approval or preview"
                )
