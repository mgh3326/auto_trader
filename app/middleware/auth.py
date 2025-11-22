"""Authentication middleware for protecting web routes."""
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.web_router import get_current_user_from_session
from app.core.db import AsyncSessionLocal


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to protect web routes requiring authentication.

    Public routes (no authentication required):
    - /web-auth/* (login, register, logout)
    - /auth/* (API authentication endpoints)
    - /health
    - /docs, /redoc, /openapi.json

    All other HTML routes require authentication.
    """

    PUBLIC_PATHS = [
        "/web-auth/login",
        "/web-auth/register",
        "/web-auth/logout",
        "/auth/",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    ]

    async def dispatch(self, request: Request, call_next):
        """Check authentication for protected routes."""
        path = request.url.path

        # Allow public paths
        if any(path.startswith(public_path) for public_path in self.PUBLIC_PATHS):
            return await call_next(request)

        # Allow API endpoints (JSON responses) - they have their own auth
        if path.startswith("/api/") or "/api/" in path:
            return await call_next(request)

        # For HTML pages, check if user is authenticated
        # This is a simple heuristic: if it's a GET request and might return HTML
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
