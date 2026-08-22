"""ROB-1272 (J7) — read-only cross-lane mock/paper/demo observation router.

GET paths only. No POST/PATCH/DELETE. No broker call, no database write, no
scheduler registration. Every response carries the twelve canonical lane
coverage rows plus the separately returned observation, anomaly, hold and
unlinked-evidence collections.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.mock_auto_read_model import (
    EvidenceSourceBinding,
    LifecycleObservationRow,
    MockAutoReadModelResponse,
)
from app.services.mock_auto_read_model import (
    build_default_ports,
    build_read_model,
    select_by_decision_intent_id,
    utc_now,
)

router = APIRouter(prefix="/trading", tags=["mock-auto-read-model"])

_BASE = "/api/mock-auto/read-model"


async def _read_model(session: AsyncSession) -> MockAutoReadModelResponse:
    return await build_read_model(
        ports=build_default_ports(session=session), as_of=utc_now()
    )


@router.get(f"{_BASE}/coverage", response_model=MockAutoReadModelResponse)
async def get_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> MockAutoReadModelResponse:
    """Twelve canonical lane coverage rows with their observed evidence."""

    del current_user
    return await _read_model(db)


@router.get(f"{_BASE}/observations", response_model=list[LifecycleObservationRow])
async def get_observations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    decision_intent_id: Annotated[str | None, Query()] = None,
    lane_id: Annotated[str | None, Query()] = None,
) -> list[LifecycleObservationRow]:
    """Lifecycle observations, optionally narrowed.

    ``decision_intent_id`` is the cross-lane fan-out path: one intent returns
    the same row shape regardless of which lane observed it.
    """

    del current_user
    response = await _read_model(db)
    rows: tuple[LifecycleObservationRow, ...] = response.lifecycle_rows
    if decision_intent_id is not None:
        rows = select_by_decision_intent_id(response, decision_intent_id)
    if lane_id is not None:
        rows = tuple(row for row in rows if row.lane_id == lane_id)
    return list(rows)


@router.get(f"{_BASE}/bindings", response_model=list[EvidenceSourceBinding])
async def get_source_bindings(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> list[EvidenceSourceBinding]:
    """The orch-stamped evidence source bindings this model reads from."""

    del current_user
    response = await _read_model(db)
    return list(response.source_bindings)
