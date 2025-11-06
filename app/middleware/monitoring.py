"""
Monitoring middleware for FastAPI.

Provides:
- Global exception handler with Telegram error reporting
- Request/response timing metrics
- Custom error handling and logging
"""

import logging
import time
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.monitoring.error_reporter import get_error_reporter
from app.monitoring.telemetry import get_telemetry_manager

logger = logging.getLogger(__name__)


class MonitoringMiddleware(BaseHTTPMiddleware):
    """Middleware for monitoring and error reporting."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request with monitoring and error handling.

        Args:
            request: FastAPI request
            call_next: Next middleware/route handler

        Returns:
            Response from the handler or error response
        """
        start_time = time.time()
        request_id = request.headers.get("X-Request-ID", "unknown")

        # Get telemetry manager
        telemetry = get_telemetry_manager()

        try:
            # Add request context to current span
            if telemetry:
                telemetry.add_span_attribute("http.request_id", request_id)
                telemetry.add_span_attribute("http.client_ip", request.client.host)

            # Process request
            response = await call_next(request)

            # Record metrics
            duration = time.time() - start_time
            if telemetry:
                telemetry.record_histogram(
                    "http.server.request.duration",
                    duration,
                    {
                        "http.method": request.method,
                        "http.route": request.url.path,
                        "http.status_code": response.status_code,
                    },
                )

                # Count requests
                telemetry.record_counter(
                    "http.server.request.count",
                    1,
                    {
                        "http.method": request.method,
                        "http.route": request.url.path,
                        "http.status_code": response.status_code,
                    },
                )

            # Add custom headers
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = str(duration)

            return response

        except Exception as exc:
            # Calculate duration
            duration = time.time() - start_time

            # Log error
            logger.error(
                f"Request failed: {request.method} {request.url.path}",
                exc_info=exc,
                extra={
                    "request_id": request_id,
                    "duration": duration,
                    "client_ip": request.client.host if request.client else "unknown",
                },
            )

            # Report to Telegram
            error_reporter = get_error_reporter()
            if error_reporter:
                await error_reporter.report_error(
                    exc,
                    level=logging.ERROR,
                    request=request,
                    additional_context={"request_id": request_id, "duration": duration},
                )

            # Record error metrics
            if telemetry:
                telemetry.record_counter(
                    "http.server.request.errors",
                    1,
                    {
                        "http.method": request.method,
                        "http.route": request.url.path,
                        "error.type": type(exc).__name__,
                    },
                )

            # Return error response
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
                headers={
                    "X-Request-ID": request_id,
                    "X-Process-Time": str(duration),
                },
            )


async def setup_exception_handlers(app) -> None:
    """
    Setup global exception handlers for the FastAPI app.

    Args:
        app: FastAPI application instance
    """

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """
        Global exception handler for all unhandled exceptions.

        Args:
            request: FastAPI request
            exc: Exception that was raised

        Returns:
            JSON response with error details
        """
        request_id = request.headers.get("X-Request-ID", "unknown")

        # Log error
        logger.error(
            f"Unhandled exception in {request.method} {request.url.path}",
            exc_info=exc,
            extra={
                "request_id": request_id,
                "client_ip": request.client.host if request.client else "unknown",
            },
        )

        # Report to Telegram
        error_reporter = get_error_reporter()
        if error_reporter:
            await error_reporter.report_error(
                exc,
                level=logging.ERROR,
                request=request,
                additional_context={"request_id": request_id},
            )

        # Record error metric
        telemetry = get_telemetry_manager()
        if telemetry:
            telemetry.record_counter(
                "http.server.unhandled_exceptions",
                1,
                {
                    "http.method": request.method,
                    "http.route": request.url.path,
                    "error.type": type(exc).__name__,
                },
            )

        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    logger.info("Global exception handlers configured")
