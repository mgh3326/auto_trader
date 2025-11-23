"""Admin router for user management."""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.auth.token_repository import revoke_all_refresh_tokens
from app.auth.web_router import get_current_user_from_session, invalidate_user_cache
from app.core.db import get_db
from app.core.session_blacklist import get_session_blacklist
from app.core.templates import templates
from app.models.trading import User, UserRole

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


def _security_log_extra(request: Request, **kwargs) -> dict:
    """Structured metadata for security logs."""
    return {
        "client_ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        **kwargs,
    }


class RoleUpdateRequest(BaseModel):
    """Request model for role update."""
    role: UserRole


async def require_admin(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Dependency to require admin role."""
    user = await get_current_user_from_session(request, db)
    if not user:
        logger.warning(
            "Admin access denied: unauthenticated",
            extra=_security_log_extra(request, event="admin_access_denied"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
        )

    if not has_min_role(user.role, UserRole.admin):
        logger.warning(
            "Admin access denied: insufficient role",
            extra=_security_log_extra(
                request,
                user_id=user.id,
                user_role=user.role.value if user else None,
                event="admin_access_denied",
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )

    return user


@router.get("/users", response_class=HTMLResponse)
async def users_management_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
):
    """사용자 관리 페이지."""
    # Get all users
    result = await db.execute(
        select(User).order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "user": admin_user,
            "users": users,
        },
    )


@router.get("/users/api")
async def get_all_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
    skip: int = 0,
    limit: int = 100,
):
    """
    Get all users with pagination (API endpoint).

    Args:
        skip: Number of records to skip (offset)
        limit: Maximum number of records to return (max 1000)
    """
    # Limit maximum to prevent abuse
    if limit > 1000:
        limit = 1000

    # Get total count
    from sqlalchemy import func
    count_result = await db.execute(select(func.count(User.id)))
    total = count_result.scalar()

    # Get paginated users
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).offset(skip).limit(limit)
    )
    users = result.scalars().all()

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "users": [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role.value,
                "is_active": user.is_active,
                "created_at": str(user.created_at) if user.created_at else None,
            }
            for user in users
        ],
    }


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    role_data: RoleUpdateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
):
    """Update user role."""
    # Prevent admin from changing their own role
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자신의 권한은 변경할 수 없습니다.",
        )

    # Get target user
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )

    # Update role
    try:
        user.role = role_data.role
        revoked_count = await revoke_all_refresh_tokens(db, user.id)
        await db.commit()
        await db.refresh(user)
    except Exception as err:
        await db.rollback()
        logger.error(
            "Failed to update user role",
            exc_info=True,
            extra=_security_log_extra(
                request,
                admin_id=admin_user.id if admin_user else None,
                target_user_id=user.id,
                event="admin_update_role_error",
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="역할을 변경하는 중 오류가 발생했습니다.",
        ) from err

    logger.info(
        "User role updated by admin",
        extra=_security_log_extra(
            request,
            admin_id=admin_user.id if admin_user else None,
            target_user_id=user.id,
            new_role=user.role.value,
            revoked_tokens=revoked_count,
            event="admin_update_role",
        ),
    )

    # Invalidate cached sessions to enforce new role
    try:
        await invalidate_user_cache(user.id)
    except Exception:
        logger.warning("Failed to invalidate cache for user_id=%s", user.id, exc_info=True)

    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "message": f"권한이 {role_data.role.value}(으)로 변경되었습니다.",
    }


@router.put("/users/{user_id}/toggle")
async def toggle_user_active(
    user_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
):
    """Toggle user active status."""
    # Prevent admin from deactivating themselves
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자신의 계정은 비활성화할 수 없습니다.",
        )

    # Get target user
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="사용자를 찾을 수 없습니다.",
        )

    # Toggle active status
    try:
        user.is_active = not user.is_active
        revoked_count = await revoke_all_refresh_tokens(db, user.id)
        await db.commit()
        await db.refresh(user)
    except Exception as err:
        await db.rollback()
        logger.error(
            "Failed to toggle user active status",
            exc_info=True,
            extra=_security_log_extra(
                request,
                admin_id=admin_user.id if admin_user else None,
                target_user_id=user.id,
                event="admin_toggle_user_error",
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 상태를 변경할 수 없습니다.",
        ) from err

    blacklist = get_session_blacklist()
    try:
        if user.is_active:
            await blacklist.remove_from_blacklist(user.id)
        else:
            await blacklist.blacklist_user(user.id)
    except Exception:
        logger.warning(
            "Failed to update session blacklist for user_id=%s", user.id, exc_info=True
        )

    try:
        await invalidate_user_cache(user.id)
    except Exception:
        logger.warning("Failed to invalidate cache for user_id=%s", user.id, exc_info=True)

    logger.info(
        "User active status toggled by admin",
        extra=_security_log_extra(
            request,
            admin_id=admin_user.id if admin_user else None,
            target_user_id=user.id,
            is_active=user.is_active,
            revoked_tokens=revoked_count,
            event="admin_toggle_user",
        ),
    )

    return {
        "id": user.id,
        "username": user.username,
        "is_active": user.is_active,
        "message": f"사용자가 {'활성화' if user.is_active else '비활성화'}되었습니다.",
    }


@router.get("/stats")
async def get_admin_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
):
    """Get admin statistics."""
    # Get all users
    result = await db.execute(select(User))
    users = result.scalars().all()

    # Calculate stats
    total_users = len(users)
    active_users = sum(1 for u in users if u.is_active)
    inactive_users = total_users - active_users

    role_counts = {
        "admin": sum(1 for u in users if u.role == UserRole.admin),
        "trader": sum(1 for u in users if u.role == UserRole.trader),
        "viewer": sum(1 for u in users if u.role == UserRole.viewer),
    }

    return {
        "total_users": total_users,
        "active_users": active_users,
        "inactive_users": inactive_users,
        "role_counts": role_counts,
    }
