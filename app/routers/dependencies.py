"""
Common router dependencies and constants.
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import AUTH_REQUIRED_MESSAGE
from app.auth.web_router import get_current_user_from_session
from app.core.db import get_db
from app.models.trading import User


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
