"""Web authentication router with HTML pages and session management."""
import hashlib
import hmac
import json
import logging
import string
from typing import Annotated, Optional, Union

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.auth.security import get_password_hash, verify_password
from app.core.config import settings
from app.core.db import get_db
from app.core.session_blacklist import get_session_blacklist
from app.core.templates import templates
from app.models.trading import User, UserRole

router = APIRouter(prefix="/web-auth", tags=["web-authentication"])
logger = logging.getLogger(__name__)

# Rate limiter for brute-force protection
limiter = Limiter(key_func=get_remote_address)

# Session serializer for secure cookie-based sessions
session_serializer = URLSafeTimedSerializer(
    settings.SECRET_KEY, salt="session-cookie"
)

# Session cookie settings
SESSION_COOKIE_NAME = "session"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days
USER_CACHE_TTL = 300  # 5 minutes
SESSION_HASH_KEY_PREFIX = "user_session"
USER_CACHE_KEY_PREFIX = "user_cache"


def _session_hash_key(user_id: int) -> str:
    return f"{SESSION_HASH_KEY_PREFIX}:{user_id}"


def _user_cache_key(user_id: int) -> str:
    return f"{USER_CACHE_KEY_PREFIX}:{user_id}"


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session_token(user_id: int) -> str:
    """Create a secure session token for the user."""
    return session_serializer.dumps({"user_id": user_id})


def verify_session_token(token: str, max_age: int = SESSION_TTL) -> Optional[int]:
    """Verify session token and return user_id if valid."""
    try:
        data = session_serializer.loads(token, max_age=max_age)
        return data.get("user_id")
    except (BadSignature, SignatureExpired):
        return None


async def invalidate_user_cache(user_id: int) -> None:
    """Remove cached session data for the given user."""
    import redis.asyncio as redis

    redis_client = None
    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        await redis_client.delete(
            _session_hash_key(user_id),
            _user_cache_key(user_id),
        )
    finally:
        if redis_client:
            await redis_client.aclose()


def _security_log_extra(request: Request, **kwargs) -> dict:
    """Structured metadata for auth security logs."""
    return {
        "client_ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        **kwargs,
    }


async def get_current_user_from_session(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> Optional[User]:
    """Get current user from session cookie with Redis caching."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return None

    user_id = verify_session_token(session_token)
    if not user_id:
        return None

    # Check if user is blacklisted (session invalidated)
    blacklist = get_session_blacklist()
    if await blacklist.is_blacklisted(user_id):
        return None

    session_hash = _hash_session_token(session_token)

    # Try to validate session and get user from cache
    import redis.asyncio as redis

    redis_client = None
    session_hash_key = _session_hash_key(user_id)
    user_cache_key = _user_cache_key(user_id)
    session_hash_verified = False

    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        stored_session_hash = await redis_client.get(session_hash_key)
        if not stored_session_hash or not hmac.compare_digest(
            stored_session_hash, session_hash
        ):
            return None

        session_hash_verified = True

        cached_user = await redis_client.get(user_cache_key)

        if cached_user:
            user_data = json.loads(cached_user)
            user = User(
                id=user_data["id"],
                username=user_data["username"],
                email=user_data["email"],
                role=UserRole[user_data["role"]],
                is_active=user_data["is_active"],
                hashed_password=user_data.get("hashed_password"),
            )
            if user.is_active:
                return user
            return None
    except Exception:
        logger.warning(
            "Session cache lookup failed for user_id=%s", user_id, exc_info=True
        )
        return None
    finally:
        if redis_client:
            await redis_client.aclose()

    if not session_hash_verified:
        return None

    # Cache miss - query database
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        redis_client = None
        # Store in cache for 5 minutes
        try:
            redis_client = redis.from_url(
                settings.get_redis_url(),
                decode_responses=True,
            )
            user_data = {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role.name,
                "is_active": user.is_active,
                "hashed_password": user.hashed_password,
            }
            await redis_client.set(
                user_cache_key, json.dumps(user_data), ex=USER_CACHE_TTL
            )
            await redis_client.set(
                session_hash_key, session_hash, ex=SESSION_TTL
            )
        except Exception:
            logger.warning(
                "Failed to refresh session cache for user_id=%s",
                user_id,
                exc_info=True,
            )
        finally:
            if redis_client:
                await redis_client.aclose()

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

    if not has_min_role(user.role, min_role):
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
@limiter.limit("5/minute")
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Optional[str] = Form(None),
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Handle login form submission with rate limiting (5 attempts/minute)."""
    username_hash = hashlib.sha256(username.encode()).hexdigest()[:16]

    # Get user by username
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    # Verify credentials
    if not user or not user.hashed_password:
        logger.warning(
            "Web login failed: user not found or password missing",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="web_login_failure"
            ),
        )
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
        logger.warning(
            "Web login failed: invalid password",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="web_login_failure"
            ),
        )
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
        logger.warning(
            "Web login failed: inactive user",
            extra=_security_log_extra(
                request, username_hash=username_hash, event="web_login_failure"
            ),
        )
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
    session_hash = _hash_session_token(session_token)

    import redis.asyncio as redis

    redis_client = None
    try:
        redis_client = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
        )
        user_data = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role.name,
            "is_active": user.is_active,
            "hashed_password": user.hashed_password,
        }
        await redis_client.set(
            _session_hash_key(user.id), session_hash, ex=SESSION_TTL
        )
        await redis_client.set(
            _user_cache_key(user.id), json.dumps(user_data), ex=USER_CACHE_TTL
        )
    except Exception:
        logger.error(
            "Web login failed to persist session cache",
            exc_info=True,
            extra=_security_log_extra(
                request, username_hash=username_hash, event="web_login_error"
            ),
        )
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "로그인 세션을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                "next": next,
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    finally:
        if redis_client:
            await redis_client.aclose()

    # Redirect to next page or home
    redirect_url = next or "/"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
    )

    logger.info(
        "Web login succeeded",
        extra=_security_log_extra(
            request, username_hash=username_hash, event="web_login_success"
        ),
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
    # Validate password strength
    if len(password) < 8:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호는 최소 8자 이상이어야 합니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not any(c.isupper() for c in password):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호에 대문자가 최소 1개 이상 포함되어야 합니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not any(c.isdigit() for c in password):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호에 숫자가 최소 1개 이상 포함되어야 합니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not any(c in string.punctuation for c in password):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "비밀번호에 특수문자가 최소 1개 이상 포함되어야 합니다.",
                "email": email,
                "username": username,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

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
    session_token = request.cookies.get(SESSION_COOKIE_NAME)

    if session_token:
        user_id = verify_session_token(session_token)
        if user_id:
            import redis.asyncio as redis

            redis_client = None
            try:
                redis_client = redis.from_url(
                    settings.get_redis_url(),
                    decode_responses=True,
                )
                await redis_client.delete(
                    _session_hash_key(user_id),
                    _user_cache_key(user_id),
                )
            except Exception:
                logger.warning(
                    "Failed to invalidate session cache during logout for user_id=%s",
                    user_id,
                    exc_info=True,
                )
            finally:
                if redis_client:
                    await redis_client.aclose()

    response = RedirectResponse(
        url="/web-auth/login", status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
