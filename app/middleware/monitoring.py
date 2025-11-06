"""
Monitoring middleware for FastAPI.

Provides:
- Custom span creation for request tracing
- Response time and status code metrics collection
- Error reporting to Telegram via ErrorReporter
"""

import logging
import time
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.monitoring.error_reporter import get_error_reporter
from app.monitoring.telemetry import get_meter, get_tracer, is_telemetry_initialized

logger = logging.getLogger(__name__)


class MonitoringMiddleware(BaseHTTPMiddleware):
    """
    Middleware for monitoring requests with OpenTelemetry tracing and metrics.
    """

    def __init__(self, app):
        super().__init__(app)

        # Initialize tracer and meter if telemetry is enabled
        if is_telemetry_initialized():
            self._tracer = get_tracer(__name__)
            self._meter = get_meter(__name__)

            # Create metrics
            self._request_duration_histogram = self._meter.create_histogram(
                name="http.server.request.duration",
                description="HTTP request duration in milliseconds",
                unit="ms",
            )

            self._request_counter = self._meter.create_counter(
                name="http.server.request.count",
                description="Total HTTP requests",
                unit="1",
            )

            self._error_counter = self._meter.create_counter(
                name="http.server.error.count",
                description="Total HTTP errors",
                unit="1",
            )

            logger.debug("MonitoringMiddleware initialized with telemetry")
        else:
            self._tracer = None
            self._meter = None
            logger.debug("MonitoringMiddleware initialized without telemetry")

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

        # Start custom span if telemetry is enabled
        if self._tracer:
            with self._tracer.start_as_current_span(
                f"{request.method} {request.url.path}"
            ) as span:
                return await self._process_request_with_span(
                    request, call_next, start_time, request_id, span
                )
        else:
            return await self._process_request_without_span(
                request, call_next, start_time, request_id
            )

    async def _process_request_with_span(
        self, request: Request, call_next: Callable, start_time: float, request_id: str, span
    ) -> Response:
        """Process request with OpenTelemetry span."""
        try:
            # Add span attributes
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.url", str(request.url))
            span.set_attribute("http.route", request.url.path)
            span.set_attribute("http.request_id", request_id)
            if request.client:
                span.set_attribute("http.client_host", request.client.host)
                span.set_attribute("http.client_port", request.client.port)

            # Process request
            response = await call_next(request)

            # Add response attributes
            span.set_attribute("http.status_code", response.status_code)

            # Record metrics
            duration_ms = (time.time() - start_time) * 1000
            self._record_metrics(request, response.status_code, duration_ms)

            # Add custom headers
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"

            return response

        except Exception as exc:
            # Record exception in span
            span.record_exception(exc)
            span.set_attribute("error", True)

            # Handle error
            return await self._handle_error(
                request, exc, start_time, request_id
            )

    async def _process_request_without_span(
        self, request: Request, call_next: Callable, start_time: float, request_id: str
    ) -> Response:
        """Process request without OpenTelemetry span."""
        try:
            # Process request
            response = await call_next(request)

            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000

            # Add custom headers
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"

            return response

        except Exception as exc:
            # Handle error
            return await self._handle_error(
                request, exc, start_time, request_id
            )

    def _record_metrics(
        self, request: Request, status_code: int, duration_ms: float
    ) -> None:
        """
        Record request metrics.

        Args:
            request: FastAPI request
            status_code: HTTP status code
            duration_ms: Request duration in milliseconds
        """
        if not self._meter:
            return

        attributes = {
            "http.method": request.method,
            "http.route": request.url.path,
            "http.status_code": str(status_code),
        }

        # Record duration histogram
        self._request_duration_histogram.record(duration_ms, attributes)

        # Increment request counter
        self._request_counter.add(1, attributes)

    async def _handle_error(
        self, request: Request, exc: Exception, start_time: float, request_id: str
    ) -> JSONResponse:
        """
        Handle error during request processing.

        Args:
            request: FastAPI request
            exc: Exception that occurred
            start_time: Request start time
            request_id: Request ID

        Returns:
            JSON error response
        """
        duration_ms = (time.time() - start_time) * 1000

        # Log error
        logger.error(
            f"Request failed: {request.method} {request.url.path}",
            exc_info=exc,
            extra={
                "request_id": request_id,
                "duration_ms": duration_ms,
                "client_ip": request.client.host if request.client else "unknown",
            },
        )

        # Report to Telegram
        error_reporter = get_error_reporter()
        try:
            await error_reporter.send_error_to_telegram(
                exc,
                request=request,
                additional_context={
                    "request_id": request_id,
                    "duration_ms": f"{duration_ms:.2f}",
                },
            )
        except Exception as e:
            logger.warning(f"Failed to send error to Telegram: {e}")

        # Record error metric
        if self._meter:
            attributes = {
                "http.method": request.method,
                "http.route": request.url.path,
                "error.type": type(exc).__name__,
            }
            self._error_counter.add(1, attributes)

        # Return error response
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "request_id": request_id,
            },
            headers={
                "X-Request-ID": request_id,
                "X-Process-Time": f"{duration_ms:.2f}ms",
            },
        )
