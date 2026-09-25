"""Operator-only, evidence-backed protected-position declarations (#728).

The underlying protection head is a global scope/market/symbol singleton in
``review.protected_positions``.  It is therefore owned by the same fixed
operator context as manual cash, while each revision continues to identify
the authenticated administrator who made the change.  This router is the only
web mutation surface; it does not expose a CLI or MCP mutation path.

Most importantly, a confirmation preview is not a save.  The real save gives
``ProtectedQuantityService`` a lazy provider so that its fresh broker read is
performed only after the service acquired its transaction-scoped advisory
lock.  A browser preview is deliberately re-read at that point.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin_router import require_admin
from app.auth.role_hierarchy import has_min_role
from app.core.db import get_db
from app.mcp_server.tooling import user_settings_tools
from app.models.trading import User, UserRole
from app.routers.dependencies import get_authenticated_user
from app.services.protected_position_settings import (
    BrokerObservationUnavailable,
    fresh_broker_observation,
    protection_change_preview,
    read_protected_position_history,
    read_protected_position_settings,
)
from app.services.protected_quantity_service import (
    ProtectedQuantityConflictError,
    ProtectedQuantityService,
    ProtectedQuantityValidationError,
    ProtectionStateUnavailable,
    normalize_protection_key,
    parse_operator_quantity,
)

router = APIRouter(prefix="/invest/api/settings", tags=["invest-settings"])


class ProtectedPositionSaveRequest(BaseModel):
    """Strict wire format for one declaration mutation or preview."""

    model_config = ConfigDict(extra="forbid")

    protected_quantity: StrictStr
    reason: StrictStr
    # ``null`` is the only valid first-declaration token.  It is deliberately
    # required rather than silently defaulted, so stale form handling remains
    # explicit at the API boundary.
    expected_revision: StrictInt | None
    idempotency_key: StrictStr
    confirm_protection_change: StrictBool
    confirm_symbol: StrictStr | None = None
    # The service has a distinct append-only reconfirm revision action.  It is
    # explicit here rather than treating an unchanged quantity as a no-op.
    reconfirm: StrictBool = False


def _owner_user_id() -> int:
    """Resolve the fixed global owner context used by manual-cash settings."""

    return user_settings_tools.MCP_USER_ID


def _assert_fixed_owner_context() -> int:
    """Reject a malformed owner setting instead of silently using an actor row.

    The #728 head schema has one global key and no per-user column, unlike the
    JSONB manual-cash row.  Resolving the fixed MCP user here prevents this UI
    from acquiring an accidental logged-in-user ownership interpretation.
    """

    owner_user_id = _owner_user_id()
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "protected_position_owner_unavailable"},
        )
    return owner_user_id


def _unprocessable(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": error, "message": message},
    )


def _conflict(error: str, message: str, **context: Any) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": error, "message": message, **context},
    )


async def _preview_or_reject(
    *,
    db: AsyncSession,
    account_scope: str,
    market: str,
    symbol: str,
    new_quantity: Decimal,
) -> dict[str, Any]:
    """Freshly render a non-mutating confirmation preview.

    This is intentionally outside the save lock because it writes nothing and
    cannot authorize a later save.  The confirmed request supplies a new lazy
    observation provider to the service, which repeats the broker read after
    taking its advisory transaction lock.
    """

    key = normalize_protection_key(
        account_scope=account_scope,
        market=market,
        symbol=symbol,
    )
    observation = await fresh_broker_observation(key=key)
    if new_quantity > observation.held:
        raise _unprocessable(
            "protected_quantity_exceeds_held",
            "protected_quantity must not exceed fresh broker held quantity",
        )
    current = await ProtectedQuantityService(db).get(key=key)
    previous_quantity = (
        current.protected_quantity if current is not None else Decimal("0")
    )
    return protection_change_preview(
        key=key,
        previous_quantity=previous_quantity,
        new_quantity=new_quantity,
        observation=observation,
    )


@router.get("/protected-positions")
async def get_protected_positions(
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """List every declared or currently declarable live holding.

    A broker source failure is retained as an unverified row by the read model;
    this endpoint never substitutes a zero quantity for unavailable evidence.
    """

    _assert_fixed_owner_context()
    return {
        "can_edit": has_min_role(user.role, UserRole.admin),
        "positions": await read_protected_position_settings(db),
    }


@router.get("/protected-positions/{account_scope}/{market}/{symbol}/history")
async def get_protected_position_history(
    account_scope: str,
    market: str,
    symbol: str,
    user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Return immutable revision evidence for one normalized declaration key."""

    del user  # Session authentication is the only permission needed to read.
    _assert_fixed_owner_context()
    try:
        key = normalize_protection_key(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
        )
    except ProtectedQuantityValidationError as exc:
        raise _unprocessable("invalid_protected_position", str(exc)) from exc
    return {
        "account_scope": key.account_scope,
        "market": key.market,
        "symbol": key.symbol,
        "history": await read_protected_position_history(db, key=key),
    }


@router.put("/protected-positions/{account_scope}/{market}/{symbol}")
async def put_protected_position(
    account_scope: str,
    market: str,
    symbol: str,
    request: ProtectedPositionSaveRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Preview or append a single protection declaration revision.

    The confirmed branch deliberately does not pre-read broker state.  Its
    provider is a closure that service ``save`` calls after its advisory lock;
    this ordering is a safety property covered by a DB-backed route test.
    """

    _assert_fixed_owner_context()
    try:
        key = normalize_protection_key(
            account_scope=account_scope,
            market=market,
            symbol=symbol,
        )
        new_quantity = parse_operator_quantity(
            request.protected_quantity,
            field="protected_quantity",
        )
    except ProtectedQuantityValidationError as exc:
        raise _unprocessable("invalid_protected_quantity", str(exc)) from exc

    if not request.reason.strip():
        raise _unprocessable("invalid_protected_quantity", "reason is required")
    if not request.idempotency_key.strip():
        raise _unprocessable(
            "invalid_protected_quantity", "idempotency_key is required"
        )
    if request.confirm_protection_change is not True:
        try:
            preview = await _preview_or_reject(
                db=db,
                account_scope=key.account_scope,
                market=key.market,
                symbol=key.symbol,
                new_quantity=new_quantity,
            )
        except BrokerObservationUnavailable as exc:
            raise _unprocessable("broker_read_failed", str(exc)) from exc
        except ProtectedQuantityValidationError as exc:
            raise _unprocessable("invalid_protected_quantity", str(exc)) from exc
        raise _conflict(
            "confirm_required",
            "protected quantity changes require explicit confirmation",
            preview=preview,
        )

    async def observation_provider():
        return await fresh_broker_observation(key=key)

    try:
        result = await ProtectedQuantityService(db).save(
            account_scope=key.account_scope,
            market=key.market,
            symbol=key.symbol,
            protected_quantity=request.protected_quantity,
            expected_revision=request.expected_revision,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
            actor_user_id=admin.id,
            origin="invest_ui",
            observation_provider=observation_provider,
            reconfirm=request.reconfirm,
            confirm_protection_change=True,
            confirm_symbol=request.confirm_symbol,
        )
    except BrokerObservationUnavailable as exc:
        raise _unprocessable("broker_read_failed", str(exc)) from exc
    except ProtectedQuantityValidationError as exc:
        raise _unprocessable("invalid_protected_quantity", str(exc)) from exc
    except ProtectedQuantityConflictError as exc:
        raise _conflict(exc.error, str(exc), **exc.context) from exc
    except ProtectionStateUnavailable as exc:
        raise _unprocessable("protection_state_unavailable", str(exc)) from exc

    return {
        "position": {
            "account_scope": result.head.key.account_scope,
            "market": result.head.key.market,
            "symbol": result.head.key.symbol,
            "protected_quantity": format(result.head.protected_quantity, "f"),
            "revision": result.revision,
            "action": result.action,
        },
        "idempotent_replay": result.idempotent_replay,
    }


__all__ = ["ProtectedPositionSaveRequest", "router"]
