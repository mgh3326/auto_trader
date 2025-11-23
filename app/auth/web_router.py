"""Web authentication router with HTML pages and session management."""
from typing import Annotated, Optional, Union

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import get_password_hash, verify_password
from app.core.config import settings
from app.core.db import get_db
from app.core.templates import templates
from app.models.trading import User, UserRole

router = APIRouter(prefix="/web-auth", tags=["web-authentication"])

# Session serializer for secure cookie-based sessions
session_serializer = URLSafeTimedSerializer(
    settings.SECRET_KEY, salt="session-cookie"
)

# Session cookie settings
SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def create_session_token(user_id: int) -> str:
    """Create a secure session token for the user."""
    return session_serializer.dumps({"user_id": user_id})


def verify_session_token(token: str, max_age: int = SESSION_MAX_AGE) -> Optional[int]:
    """Verify session token and return user_id if valid."""
    try:
        data = session_serializer.loads(token, max_age=max_age)
        return data.get("user_id")
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user_from_session(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> Optional[User]:
    """Get current user from session cookie."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None

    user_id = verify_session_token(session_token)
    if not user_id:
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        return user
    return None


async def require_login(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> Union[User, Response]:
    """Dependency to require login for routes."""
    user = await get_current_user_from_session(request, db)
    if not user:
        # Redirect to login page with next parameter
        next_url = str(request.url)
        return RedirectResponse(
            url=f"/web-auth/login?next={next_url}", status_code=status.HTTP_303_SEE_OTHER
        )
    return user


async def require_role(
    min_role: UserRole,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Union[User, Response]:
    """Dependency to require specific role for routes."""
    user = await get_current_user_from_session(request, db)
    if not user:
        return RedirectResponse(
            url="/web-auth/login", status_code=status.HTTP_303_SEE_OTHER
        )

    role_hierarchy = {UserRole.viewer: 0, UserRole.trader: 1, UserRole.admin: 2}

    if role_hierarchy.get(user.role, 0) < role_hierarchy.get(min_role, 0):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "error": "권한이 부족합니다.",
                "message": f"이 페이지에 접근하려면 {min_role.value} 이상의 권한이 필요합니다.",
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return user


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: Optional[str] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Display login page."""
    # Check if already logged in
    if db:
        user = await get_current_user_from_session(request, db)
        if user:
            redirect_url = next or "/"
            return RedirectResponse(
                url=redirect_url, status_code=status.HTTP_303_SEE_OTHER
            )

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": next,
        },
    )


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Optional[str] = Form(None),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Handle login form submission."""
    # Get user by username
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    # Verify credentials
    if not user or not user.hashed_password:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "사용자명 또는 비밀번호가 올바르지 않습니다.",
                "next": next,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "사용자명 또는 비밀번호가 올바르지 않습니다.",
                "next": next,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "비활성화된 계정입니다.",
                "next": next,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Create session token
    session_token = create_session_token(user.id)

    # Redirect to next page or home
    redirect_url = next or "/"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
    )

    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Display registration page."""
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={},
    )


@router.post("/register")
async def register(
    request: Request,
    email: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Handle registration form submission."""
    # Validate password confirmation
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호가 일치하지 않습니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Check if username already exists
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "이미 사용 중인 사용자명입니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "이미 사용 중인 이메일입니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Create new user
    hashed_password = get_password_hash(password)
    db_user = User(
        email=email,
        username=username,
        hashed_password=hashed_password,
        role=UserRole.viewer,  # Default role
        is_active=True,
    )

    try:
        db.add(db_user)
        await db.commit()
        await db.refresh(db_user)
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "계정을 생성할 수 없습니다. 다시 시도해주세요.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Show success message and redirect to login
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "success": "회원가입이 완료되었습니다! 로그인해주세요.",
        },
        status_code=status.HTTP_201_CREATED,
        headers={"Refresh": "2; url=/web-auth/login"},
    )


@router.get("/logout")
async def logout(request: Request):
    """Handle logout."""
    response = RedirectResponse(
        url="/web-auth/login", status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
