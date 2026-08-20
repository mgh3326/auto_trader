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
from app.schemas.funding_advisory import (
    ExternalCashDeclarationRequest,
    canonical_decimal,
)
from app.services.funding_advisory.external_cash import (
    NO_AUTO_ADD_NOTICE,
    ExternalCashAmbiguousHeadError,
    ExternalCashAuthorizationError,
    ExternalCashConflictError,
    ExternalCashDeclarationService,
    ExternalCashValidationError,
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


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="as_of must include a timezone",
        )
    return value.astimezone(UTC)


def _head_summary(view: Any) -> dict[str, Any] | None:
    record = view.current
    if record is None:
        return None
    return {
        "location_key": record.location_key,
        "display_label": record.display_label,
        "currency": record.currency,
        "amount": canonical_decimal(record.amount),
        "as_of": record.as_of,
        "status": view.status,
        "expected_head_declaration_id": str(record.declaration_id),
    }


@router.get("/external-cash/current")
async def get_external_cash_current(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    location_key: Annotated[str | None, Query(alias="locationKey")] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
) -> dict[str, Any]:
    service = ExternalCashDeclarationService(db)
    views = await service.list_current(owner_user_id=user.id, now=_now())
    if location_key is not None:
        views = [
            view
            for view in views
            if view.current is not None
            and view.current.location_key == location_key
            and (currency is None or view.current.currency == currency)
        ]
    elif currency is not None:
        views = [
            view
            for view in views
            if view.current is not None and view.current.currency == currency
        ]
    return {
        "heads": [view.model_dump(mode="json") for view in views],
        "count": len(views),
        "notice": NO_AUTO_ADD_NOTICE,
    }


@router.get("/external-cash/history")
async def get_external_cash_history(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    location_key: Annotated[str | None, Query(alias="locationKey")] = None,
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
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
        "notice": NO_AUTO_ADD_NOTICE,
    }


@router.get("/external-cash/form")
async def get_external_cash_form(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    owner_user_id: Annotated[int | None, Query(alias="ownerUserId", gt=0)] = None,
) -> dict[str, Any]:
    owner_id = owner_user_id or admin.id
    now = _now()
    service = ExternalCashDeclarationService(db)
    views = await service.list_current(owner_user_id=owner_id, now=now)
    parking = next(
        (
            view
            for view in views
            if view.current is not None
            and view.current.location_key == "parking_primary"
            and view.current.currency == "KRW"
        ),
        None,
    )
    head = parking.current if parking is not None else None
    return {
        "owner_user_id": owner_id,
        "as_of": now,
        "as_of_fixed": True,
        "creates_money_movement": False,
        "idempotency_key": f"funding-ui:{uuid.uuid4()}",
        "notice": NO_AUTO_ADD_NOTICE,
        "currencies": ["KRW", "USD"],
        "default_location_key": "parking_primary",
        "default_display_label": head.display_label if head else "파킹통장",
        "default_currency": "KRW",
        "default_amount": canonical_decimal(head.amount) if head else "0",
        "default_source_note": head.source_note if head else "운영자 선언",
        "expected_head_declaration_id": (
            str(head.declaration_id) if head is not None else None
        ),
        "heads": [
            summary
            for summary in (_head_summary(view) for view in views)
            if summary is not None
        ],
    }


@router.post("/external-cash/declarations", status_code=status.HTTP_201_CREATED)
async def declare_external_cash(
    request: ExternalCashDeclarationRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    now = _now()
    if _aware_utc(request.as_of) > now:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="as_of cannot be in the future",
        )
    service = ExternalCashDeclarationService(db)
    try:
        row = await service.declare(request, admin, now)
        payload = row.model_dump(mode="json")
        payload["notice"] = NO_AUTO_ADD_NOTICE
        return payload
    except ExternalCashConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": exc.error,
                "message": str(exc),
                "notice": NO_AUTO_ADD_NOTICE,
                "current_head": (
                    None
                    if exc.current_head is None
                    else exc.current_head.model_dump(mode="json")
                ),
            },
        ) from exc
    except ExternalCashAmbiguousHeadError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ambiguous_head",
                "message": str(exc),
                "notice": NO_AUTO_ADD_NOTICE,
                "current_head": None,
            },
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
