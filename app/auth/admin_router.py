"""Admin router for user management."""
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.web_router import get_current_user_from_session
from app.core.db import get_db
from app.models.trading import User, UserRole

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
        )

    if user.role != UserRole.admin:
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


@router.get("/users/api", response_model=List[dict])
async def get_all_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin_user: Annotated[User, Depends(require_admin)] = None,
):
    """Get all users (API endpoint)."""
    result = await db.execute(
        select(User).order_by(User.created_at.desc())
    )
    users = result.scalars().all()

    return [
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.value,
            "is_active": user.is_active,
            "created_at": str(user.created_at) if user.created_at else None,
        }
        for user in users
    ]


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    role_data: RoleUpdateRequest,
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
    user.role = role_data.role
    await db.commit()
    await db.refresh(user)

    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "message": f"권한이 {role_data.role.value}(으)로 변경되었습니다.",
    }


@router.put("/users/{user_id}/toggle")
async def toggle_user_active(
    user_id: int,
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
    user.is_active = not user.is_active
    await db.commit()
    await db.refresh(user)

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
