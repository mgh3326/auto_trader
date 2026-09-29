"""/invest settings: the operator-entered ``user_settings.manual_cash`` (#671).

Reads need an authenticated session; the write reuses the admin gate that the
only other /invest cash write (``invest_funding.declare_external_cash``)
already uses, and ``/invest/api/*`` stays CSRF-protected (it is not in the
``TemplateFormCSRFMiddleware`` exempt list in ``app/main.py``).

The row written is the one the capital read consumes — ``MCP_USER_ID``'s
``manual_cash`` (``user_settings_tools.get_manual_cash_setting``) — not the
logged-in user's own row, otherwise a save would be invisible to the
deployment cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin_router import require_admin
from app.auth.role_hierarchy import has_min_role
from app.core.db import get_db
from app.mcp_server.tooling import user_settings_tools
from app.models.trading import User, UserRole
from app.routers.dependencies import get_authenticated_user
from app.services.manual_cash_settings import (
    MANUAL_CASH_JUMP_CONFIRM_RATIO,
    MANUAL_CASH_MAX_ACCOUNTS,
    MANUAL_CASH_MAX_KRW,
    MANUAL_CASH_NAME_MAX_LEN,
    ManualCashConflictError,
    ManualCashValidationError,
    load_manual_cash_row,
    save_manual_cash,
    serialize_manual_cash,
)

router = APIRouter(prefix="/invest/api/settings", tags=["invest-settings"])


class ManualCashSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Row shape is validated by the service (strict int amounts, names);
    # pydantic must not coerce "1e3" or 1.0 into an int first.
    accounts: list[Any]
    expected_updated_at: str | None
    confirm_large_change: StrictBool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _owner_user_id() -> int:
    # Read at call time so the write and the capital read always agree.
    return user_settings_tools.MCP_USER_ID


def _limits() -> dict[str, Any]:
    return {
        "max_amount_krw": MANUAL_CASH_MAX_KRW,
        "max_accounts": MANUAL_CASH_MAX_ACCOUNTS,
        "name_max_len": MANUAL_CASH_NAME_MAX_LEN,
        "confirm_change_ratio": format(MANUAL_CASH_JUMP_CONFIRM_RATIO, "f"),
    }


@router.get("/manual-cash")
async def get_manual_cash(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await load_manual_cash_row(db, owner_user_id=_owner_user_id())
    return {
        "manual_cash": serialize_manual_cash(row, now=_now()),
        "limits": _limits(),
        "can_edit": has_min_role(user.role, UserRole.admin),
    }


@router.put("/manual-cash")
async def put_manual_cash(
    request: ManualCashSaveRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    try:
        saved = await save_manual_cash(
            db,
            owner_user_id=_owner_user_id(),
            actor_user_id=admin.id,
            raw_accounts=request.accounts,
            expected_updated_at=request.expected_updated_at,
            confirm_large_change=request.confirm_large_change,
            now=_now(),
        )
    except ManualCashValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "invalid_manual_cash", "message": str(exc)},
        ) from exc
    except ManualCashConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": exc.error, "message": str(exc), **exc.context},
        ) from exc
    return {"manual_cash": saved, "limits": _limits(), "can_edit": True}
