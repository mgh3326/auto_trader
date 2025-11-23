"""Authentication middleware for protecting web routes."""
from typing import ClassVar

from fastapi import Request
from fastapi.responses import RedirectResponse
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

    All other HTML routes require authentication.
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

    def __init__(self, app):
        """Initialize middleware with dynamic public paths."""
        super().__init__(app)
        # Build public paths list based on DOCS_ENABLED setting
        self.public_paths = self.BASE_PUBLIC_PATHS.copy()
        if settings.DOCS_ENABLED:
            self.public_paths.extend(self.DOCS_PATHS)

    async def dispatch(self, request: Request, call_next):
        """Check authentication for protected routes."""
        path = request.url.path

        # Allow public paths
        if any(
            path.startswith(public_path) for public_path in self.public_paths
        ):
            return await call_next(request)

        # Allow API endpoints (JSON responses) - they have their own auth
        if path.startswith("/api/"):
            return await call_next(request)

        # For HTML pages, check if user is authenticated
        # This is a simple heuristic: if it's a GET request
        # and might return HTML
        if request.method == "GET":
            async with AsyncSessionLocal() as db:
                user = await get_current_user_from_session(request, db)

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
