"""
Common router dependencies and constants.
"""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import AUTH_REQUIRED_MESSAGE
from app.auth.role_hierarchy import has_min_role
from app.auth.web_router import get_current_user_from_session
from app.core.db import get_db
from app.models.trading import User, UserRole


async def get_authenticated_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """Return authenticated user from request state or session."""
    user = getattr(request.state, "user", None)
    if user:
        return user

    session_user = await get_current_user_from_session(request, db)
    if session_user:
        return session_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=AUTH_REQUIRED_MESSAGE,
    )


def require_min_role_user(min_role: UserRole) -> Callable:
    """Factory that creates a dependency requiring a minimum role.

    Returns a dependency that:
    1. Gets the authenticated user
    2. Checks if the user has at least the required role
    3. Returns the user if authorized, raises HTTPException(403) otherwise

    Usage:
        require_trader_user = require_min_role_user(UserRole.trader)

        @router.get("/protected")
        async def protected_route(user: User = Depends(require_trader_user)):
            ...
    """

    async def _dependency(request: Request, db: AsyncSession = Depends(get_db)) -> User:
        user = await get_authenticated_user(request, db)
        if not has_min_role(user.role, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"이 기능에 접근하려면 '{min_role.value}' 이상의 권한이 필요합니다.",
            )
        return user

    return _dependency


# Pre-configured dependencies for common role requirements
require_trader_user = require_min_role_user(UserRole.trader)
