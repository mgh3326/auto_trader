"""Authenticated funding-advisory reads and the sole external-cash UI write."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin_router import require_admin
from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.funding_advisory import ExternalCashDeclarationRequest
from app.services.funding_advisory.external_cash import (
    ExternalCashAmbiguousHeadError,
    ExternalCashAuthorizationError,
    ExternalCashConflictError,
    ExternalCashDeclarationService,
    ExternalCashValidationError,
)
from app.services.funding_advisory.initial_declaration import (
    INITIAL_PARKING_AMOUNT,
)
from app.services.funding_advisory.service import (
    FundingAdvisoryNotFound,
    FundingAdvisoryService,
)

router = APIRouter(prefix="/invest/api/funding", tags=["invest-funding"])


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/advisories")
async def list_advisories(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    state_filter: Annotated[
        Literal["active", "resolved", "superseded"] | None,
        Query(alias="state"),
    ] = "active",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    service = FundingAdvisoryService(db)
    rows = await service.list_details(
        owner_user_id=user.id,
        state=state_filter,
        limit=limit,
    )
    return {"advisories": rows, "count": len(rows)}


@router.get("/allocation")
async def get_cross_market_allocation(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    service = FundingAdvisoryService(db)
    return await service.cross_market_allocation(owner_user_id=user.id, now=_now())


@router.get("/advisories/{advisory_id}")
async def get_advisory(
    advisory_id: UUID,
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    service = FundingAdvisoryService(db)
    try:
        if refresh:
            refreshed = await service.refresh_detail(
                advisory_id=advisory_id,
                owner_user_id=user.id,
                now=_now(),
            )
            if refreshed.get("status") == "triggered":
                return refreshed
            detail = await service.get_detail(
                advisory_id=advisory_id,
                owner_user_id=user.id,
            )
            detail["refresh"] = refreshed
            return detail
        return await service.get_detail(
            advisory_id=advisory_id,
            owner_user_id=user.id,
        )
    except FundingAdvisoryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc


@router.get("/external-cash/current")
async def get_external_cash_current(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    location_key: Annotated[str, Query(alias="locationKey")] = "parking_primary",
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "KRW",
) -> dict[str, Any]:
    service = ExternalCashDeclarationService(db)
    view = await service.current(
        owner_user_id=user.id,
        location_key=location_key,
        currency=currency,
        now=_now(),
    )
    return view.model_dump(mode="json")


@router.get("/external-cash/history")
async def get_external_cash_history(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    location_key: Annotated[str, Query(alias="locationKey")] = "parking_primary",
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "KRW",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    service = ExternalCashDeclarationService(db)
    rows = await service.history(
        owner_user_id=user.id,
        location_key=location_key,
        currency=currency,
        limit=limit,
    )
    return {
        "declarations": [row.model_dump(mode="json") for row in rows],
        "count": len(rows),
    }


@router.get("/external-cash/form")
async def get_external_cash_form(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    owner_user_id: Annotated[int | None, Query(alias="ownerUserId", gt=0)] = None,
) -> dict[str, Any]:
    owner_id = owner_user_id or admin.id
    service = ExternalCashDeclarationService(db)
    current = await service.current(
        owner_user_id=owner_id,
        location_key="parking_primary",
        currency="KRW",
        now=_now(),
    )
    head = current.current
    return {
        "owner_user_id": owner_id,
        "location_key": "parking_primary",
        "display_label": head.display_label if head else "파킹통장",
        "currency": "KRW",
        "amount": str(head.amount if head else INITIAL_PARKING_AMOUNT),
        "as_of": None,
        "source_note": head.source_note if head else "토스증권 → 파킹통장 이동",
        "expected_head_declaration_id": (
            str(head.declaration_id) if head is not None else None
        ),
        "idempotency_key": f"funding-ui:{uuid.uuid4()}",
        "requires_exact_operator_confirmed_time": True,
        "creates_money_movement": False,
    }


@router.post("/external-cash/declarations", status_code=status.HTTP_201_CREATED)
async def declare_external_cash(
    request: ExternalCashDeclarationRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    service = ExternalCashDeclarationService(db)
    try:
        row = await service.declare(request, admin, _now())
        return row.model_dump(mode="json")
    except (ExternalCashConflictError, ExternalCashAmbiguousHeadError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ExternalCashAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ExternalCashValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
