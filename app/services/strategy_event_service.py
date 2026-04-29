"""ROB-41 strategy event service.

DB-only. NO broker / order / watch / paper / live execution imports.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_decision import (
    TradingDecisionSession,
    TradingDecisionStrategyEvent,
)
from app.schemas.strategy_events import (
    StrategyEventCreateRequest,
    StrategyEventDetail,
    StrategyEventListResponse,
)


class UnknownSessionUUIDError(LookupError):
    """Raised when session_uuid is provided but no session matches."""


async def _resolve_session_id(
    db: AsyncSession, *, session_uuid: UUID | None
) -> int | None:
    if session_uuid is None:
        return None
    stmt = select(TradingDecisionSession.id).where(
        TradingDecisionSession.session_uuid == session_uuid
    )
    result = await db.execute(stmt)
    session_id = result.scalar_one_or_none()
    if session_id is None:
        raise UnknownSessionUUIDError(str(session_uuid))
    return session_id


def _to_detail(
    row: TradingDecisionStrategyEvent, *, session_uuid: UUID | None
) -> StrategyEventDetail:
    return StrategyEventDetail(
        id=row.id,
        event_uuid=row.event_uuid,
        session_uuid=session_uuid,
        source=row.source,
        event_type=row.event_type,
        source_text=row.source_text,
        normalized_summary=row.normalized_summary,
        affected_markets=list(row.affected_markets or []),
        affected_sectors=list(row.affected_sectors or []),
        affected_themes=list(row.affected_themes or []),
        affected_symbols=list(row.affected_symbols or []),
        severity=row.severity,
        confidence=row.confidence,
        created_by_user_id=row.created_by_user_id,
        metadata=row.event_metadata,
        created_at=row.created_at,
    )


async def create_strategy_event(
    db: AsyncSession,
    *,
    request: StrategyEventCreateRequest,
    user_id: int,
) -> StrategyEventDetail:
    session_id = await _resolve_session_id(db, session_uuid=request.session_uuid)

    row = TradingDecisionStrategyEvent(
        session_id=session_id,
        source=request.source,
        event_type=request.event_type,
        source_text=request.source_text,
        normalized_summary=request.normalized_summary,
        affected_markets=request.affected_markets,
        affected_sectors=request.affected_sectors,
        affected_themes=request.affected_themes,
        affected_symbols=request.affected_symbols,
        severity=request.severity,
        confidence=request.confidence,
        created_by_user_id=user_id,
        event_metadata=request.metadata,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _to_detail(row, session_uuid=request.session_uuid)


async def get_strategy_event_by_uuid(
    db: AsyncSession, *, event_uuid: UUID
) -> StrategyEventDetail | None:
    stmt = (
        select(TradingDecisionStrategyEvent, TradingDecisionSession.session_uuid)
        .outerjoin(
            TradingDecisionSession,
            TradingDecisionStrategyEvent.session_id == TradingDecisionSession.id,
        )
        .where(TradingDecisionStrategyEvent.event_uuid == event_uuid)
    )
    result = await db.execute(stmt)
    pair = result.first()
    if pair is None:
        return None
    row, sess_uuid = pair
    return _to_detail(row, session_uuid=sess_uuid)


async def list_strategy_events(
    db: AsyncSession,
    *,
    session_uuid: UUID | None = None,
    user_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> StrategyEventListResponse:
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    base = select(
        TradingDecisionStrategyEvent, TradingDecisionSession.session_uuid
    ).outerjoin(
        TradingDecisionSession,
        TradingDecisionStrategyEvent.session_id == TradingDecisionSession.id,
    )
    count_base = select(func.count(TradingDecisionStrategyEvent.id))

    if session_uuid is not None:
        sess_id = await _resolve_session_id(db, session_uuid=session_uuid)
        base = base.where(TradingDecisionStrategyEvent.session_id == sess_id)
        count_base = count_base.where(
            TradingDecisionStrategyEvent.session_id == sess_id
        )
    if user_id is not None:
        base = base.where(TradingDecisionStrategyEvent.created_by_user_id == user_id)
        count_base = count_base.where(
            TradingDecisionStrategyEvent.created_by_user_id == user_id
        )

    base = base.order_by(TradingDecisionStrategyEvent.created_at.desc()).limit(
        limit
    ).offset(offset)

    total = (await db.execute(count_base)).scalar_one()
    rows = (await db.execute(base)).all()
    events = [_to_detail(row, session_uuid=sess_uuid) for row, sess_uuid in rows]
    return StrategyEventListResponse(
        events=events, total=total, limit=limit, offset=offset
    )
