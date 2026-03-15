"""Authentication middleware for protecting web routes."""

from contextlib import AbstractAsyncContextManager
from typing import ClassVar, cast

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.web_router import get_current_user_from_session
from app.core.config import settings
from app.core.db import AsyncSessionLocal


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to protect web routes requiring authentication.

    Public routes (no authentication required):
    - /web-auth/* (login, register, logout)
    - /auth/* (API authentication endpoints)
    - /health
    - /docs, /redoc, /openapi.json (only if DOCS_ENABLED=True)

    All other routes require authentication unless explicitly whitelisted.
    """

    # Base public paths (always accessible)
    BASE_PUBLIC_PATHS: ClassVar[list[str]] = [
        "/web-auth/login",
        "/web-auth/register",
        "/web-auth/logout",
        "/auth/",
        "/health",
    ]

    # Documentation paths (conditionally accessible)
    DOCS_PATHS: ClassVar[list[str]] = [
        "/docs",
        "/redoc",
        "/openapi.json",
    ]

    # Public API paths (explicit whitelist)
    PUBLIC_API_PATHS: ClassVar[list[str]] = [
        "/api/v1/openclaw/callback",
        "/api/screener/callback",
        "/api/n8n/pending-orders",
    ]
    LEGACY_DEPRECATED_PREFIXES: ClassVar[list[str]] = [
        "/manual-holdings",
        "/kis-domestic-trading",
        "/kis-overseas-trading",
        "/upbit-trading",
    ]

    def __init__(self, app):
        """Initialize middleware with dynamic public paths."""
        super().__init__(app)
        # Build public paths list based on DOCS_ENABLED setting
        self.public_paths = self.BASE_PUBLIC_PATHS.copy()
        if settings.DOCS_ENABLED:
            self.public_paths.extend(self.DOCS_PATHS)
        # Build API public paths list from settings override
        self.public_api_paths = self.PUBLIC_API_PATHS.copy()
        if settings.PUBLIC_API_PATHS:
            self.public_api_paths.extend(settings.PUBLIC_API_PATHS)

    def _is_public_path(self, path: str) -> bool:
        """Check if path is in public (non-authenticated) list."""
        return any(path.startswith(public_path) for public_path in self.public_paths)

    def _is_public_api_path(self, path: str) -> bool:
        """Check if API path is explicitly public."""
        return any(
            path.startswith(public_api_path)
            for public_api_path in self.public_api_paths
        )

    def _is_legacy_deprecated_path(self, path: str) -> bool:
        """Check if path is under deprecated legacy prefixes."""
        return any(
            path.startswith(legacy_prefix)
            for legacy_prefix in self.LEGACY_DEPRECATED_PREFIXES
        )

    @staticmethod
    async def _load_user(request: Request):
        session_manager = cast(
            AbstractAsyncContextManager[AsyncSession],
            cast(object, AsyncSessionLocal()),
        )
        async with session_manager as db:
            return await get_current_user_from_session(request, db)

    @staticmethod
    def _is_api_request_path(path: str) -> bool:
        """Treat both '/api/*' and '*/api/*' paths as API requests."""
        return path.startswith("/api/") or "/api/" in path or path.endswith("/api")

    async def dispatch(self, request: Request, call_next):
        """Check authentication for protected routes."""
        path = request.url.path
        is_api_request = self._is_api_request_path(path)

        # Legacy paths must always pass through to deprecated router handlers.
        if self._is_legacy_deprecated_path(path):
            return await call_next(request)

        # Allow public paths
        if self._is_public_path(path):
            return await call_next(request)

        # Allow explicitly public API paths without extra DB work
        if is_api_request and self._is_public_api_path(path):
            return await call_next(request)

        # Handle API endpoints with explicit public allowlist
        if is_api_request:
            user = await self._load_user(request)

            if not user:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required for this endpoint."},
                )

            request.state.user = user
            return await call_next(request)

        # For HTML pages, check if user is authenticated
        # This is a simple heuristic: if it's a GET request
        # and might return HTML
        if request.method == "GET":
            user = await self._load_user(request)

            # If not authenticated, redirect to login
            if not user:
                # Save the original URL to redirect back after login
                next_url = str(request.url)
                return RedirectResponse(
                    url=f"/web-auth/login?next={next_url}",
                    status_code=303,
                )

            # Store user in request state for use in route handlers
            request.state.user = user

        return await call_next(request)
