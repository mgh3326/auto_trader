"""Authentication router for FastAPI."""
import hashlib
import logging
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_active_user
from app.auth.schemas import (
    RefreshTokenRequest,
    Token,
    UserCreate,
    UserResponse,
)
from app.auth.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from app.auth.token_repository import (
    get_valid_refresh_token,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    save_refresh_token,
)
from app.core.config import settings
from app.core.db import get_db
from app.models.trading import User, UserRole

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)


def _security_log_extra(request: Request, **kwargs) -> dict:
    """Build structured security log metadata."""
    return {
        "client_ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        **kwargs,
    }


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    """
    Register a new user.

    Args:
        user_data: User registration data (email, username, password)
        db: Database session

    Returns:
        Created user data (without password)

    Raises:
        HTTPException: If username or email already exists
    """
    # Check if username already exists
    result = await db.execute(select(User).where(User.username == user_data.username))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered",
        )

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Create new user
    hashed_password = get_password_hash(user_data.password)
    db_user = User(
        email=user_data.email,
        username=user_data.username,
        role=UserRole.viewer,
        hashed_password=hashed_password,
        is_active=True,
    )

    try:
        db.add(db_user)
        await db.commit()
        await db.refresh(db_user)
    except IntegrityError as err:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User could not be created",
        ) from err

    return UserResponse.model_validate(db_user)


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    """
    Login with username and password to get JWT tokens.

    Args:
        form_data: OAuth2 password flow (username, password)
        db: Database session

    Returns:
        Access and refresh JWT tokens

    Raises:
        HTTPException: If credentials are invalid
    """
    username_hash = hashlib.sha256(form_data.username.encode()).hexdigest()[:16]

    # Get user by username
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalar_one_or_none()

    if not user or not user.hashed_password:
        logger.warning(
            "Login failed: unknown user or missing password",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="login_failure"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify password
    if not verify_password(form_data.password, user.hashed_password):
        logger.warning(
            "Login failed: invalid password",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="login_failure"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if user is active
    if not user.is_active:
        logger.warning(
            "Login failed: inactive user",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="login_failure"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )

    try:
        # Revoke any existing refresh tokens to prevent reuse
        await revoke_all_refresh_tokens(db, user.id)

        # Create access and refresh tokens
        access_token = create_access_token(data={"sub": user.username})
        refresh_token = create_refresh_token(data={"sub": user.username})
        await save_refresh_token(db, user.id, refresh_token)
        await db.commit()
    except Exception as err:
        await db.rollback()
        logger.error(
            "Login failed during token persistence",
            exc_info=True,
            extra=_security_log_extra(
                request, username_hash=username_hash, event="login_error"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create login session",
        ) from err

    logger.info(
        "Login succeeded",
        extra=_security_log_extra(
            request, username_hash=username_hash, event="login_success"
        ),
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
    )


@router.post("/refresh", response_model=Token)
async def refresh_token(
    request: Request,
    refresh_data: RefreshTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    """
    Refresh access token using a valid refresh token.

    Args:
        refresh_data: Refresh token
        db: Database session

    Returns:
        New access and refresh tokens

    Raises:
        HTTPException: If refresh token is invalid or expired
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            refresh_data.refresh_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        username: str = payload.get("sub")
        token_type: str = payload.get("type")

        if username is None or token_type != "refresh":
            raise credentials_exception
    except jwt.ExpiredSignatureError as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from err
    except jwt.InvalidTokenError as err:
        raise credentials_exception from err

    username_hash = hashlib.sha256(username.encode()).hexdigest()[:16]

    # Verify user exists and is active
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise credentials_exception

    # Verify refresh token exists and is active
    stored_token = await get_valid_refresh_token(
        db, user_id=user.id, refresh_token=refresh_data.refresh_token
    )
    if not stored_token:
        logger.warning(
            "Refresh token reuse or invalid token detected",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="refresh_failure"
            ),
        )
        raise credentials_exception

    try:
        async with db.begin_nested():
            await revoke_refresh_token(db, stored_token)
            access_token = create_access_token(data={"sub": user.username})
            new_refresh_token = create_refresh_token(data={"sub": user.username})
            await save_refresh_token(db, user.id, new_refresh_token)
        await db.commit()
    except Exception as err:
        await db.rollback()
        logger.error(
            "Failed to rotate refresh token",
            exc_info=True,
            extra=_security_log_extra(
                request, username_hash=username_hash, event="refresh_error"
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not refresh token",
        ) from err

    logger.info(
        "Refresh token rotated successfully",
        extra=_security_log_extra(
            request, username_hash=username_hash, event="refresh_success"
        ),
    )

    return Token(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    refresh_data: RefreshTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Revoke a refresh token (logout)."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            refresh_data.refresh_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        username: str = payload.get("sub")
        token_type: str = payload.get("type")

        if username is None or token_type != "refresh":
            raise credentials_exception
    except jwt.ExpiredSignatureError as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from err
    except jwt.InvalidTokenError as err:
        raise credentials_exception from err

    username_hash = hashlib.sha256(username.encode()).hexdigest()[:16]

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user:
        raise credentials_exception

    token_record = await get_valid_refresh_token(
        db, user_id=user.id, refresh_token=refresh_data.refresh_token
    )
    if not token_record:
        logger.warning(
            "Logout failed: token not found or already revoked",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="logout_failure"
            ),
        )
        raise credentials_exception

    await revoke_refresh_token(db, token_record)
    await db.commit()

    logger.info(
        "User logged out (refresh token revoked)",
        extra=_security_log_extra(
            request, username_hash=username_hash, event="logout_success"
        ),
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> UserResponse:
    """
    Get current authenticated user information.

    Args:
        current_user: Current authenticated user from JWT

    Returns:
        Current user data
    """
    return UserResponse.model_validate(current_user)
