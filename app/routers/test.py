"""
Test endpoints for monitoring and observability.

These endpoints are used to test:
- Error reporting to Telegram
- OpenTelemetry tracing
- Custom span creation
"""

import asyncio
import logging
import time
from typing import Dict

from fastapi import APIRouter, HTTPException

from app.monitoring.telemetry import get_tracer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/test", tags=["test"])


@router.get("/error")
async def test_error() -> Dict[str, str]:
    """
    Test endpoint that raises a general error.

    This will trigger:
    - Error reporting to Telegram (if enabled)
    - Error span recording (if telemetry enabled)
    - Error metric increment

    Returns:
        Never returns - always raises an exception

    Raises:
        ValueError: Test error for monitoring
    """
    logger.info("Test error endpoint called")
    raise ValueError("This is a test error for monitoring system")


@router.get("/critical")
async def test_critical_error() -> Dict[str, str]:
    """
    Test endpoint that raises a critical error.

    This simulates a critical system failure that should be
    immediately reported to Telegram.

    Returns:
        Never returns - always raises an exception

    Raises:
        RuntimeError: Critical test error
    """
    logger.critical("Critical test error endpoint called")
    raise RuntimeError(
        "CRITICAL: This is a critical test error - immediate attention required!"
    )


@router.get("/trace")
async def test_trace() -> Dict[str, str]:
    """
    Test endpoint for custom span creation and tracing.

    This endpoint demonstrates:
    - Creating custom spans
    - Adding span attributes
    - Nested span operations
    - Span timing

    Returns:
        Dict with operation results and timing information
    """
    tracer = get_tracer(__name__)

    with tracer.start_as_current_span("test_trace_operation") as span:
        # Add custom attributes
        span.set_attribute("test.type", "trace")
        span.set_attribute("test.endpoint", "/api/test/trace")

        # Simulate some work
        start_time = time.time()

        # Nested span 1: Database query simulation
        with tracer.start_as_current_span("simulate_db_query") as db_span:
            db_span.set_attribute("db.operation", "SELECT")
            db_span.set_attribute("db.table", "test_table")
            await asyncio.sleep(0.05)  # 50ms
            db_span.set_attribute("db.rows_returned", 42)

        # Nested span 2: External API call simulation
        with tracer.start_as_current_span("simulate_api_call") as api_span:
            api_span.set_attribute("http.method", "GET")
            api_span.set_attribute("http.url", "https://api.example.com/data")
            await asyncio.sleep(0.1)  # 100ms
            api_span.set_attribute("http.status_code", 200)

        # Nested span 3: Data processing simulation
        with tracer.start_as_current_span("simulate_data_processing") as process_span:
            process_span.set_attribute("processing.items", 100)
            await asyncio.sleep(0.03)  # 30ms
            process_span.set_attribute("processing.completed", True)

        total_time = time.time() - start_time

        # Add final attributes to parent span
        span.set_attribute("operation.duration_ms", total_time * 1000)
        span.set_attribute("operation.status", "success")

        return {
            "status": "success",
            "message": "Trace test completed successfully",
            "total_duration_ms": f"{total_time * 1000:.2f}",
            "operations": {
                "db_query": "50ms",
                "api_call": "100ms",
                "data_processing": "30ms",
            },
            "info": "Check SigNoz dashboard to see the trace spans",
        }


@router.get("/http-error")
async def test_http_error() -> Dict[str, str]:
    """
    Test endpoint that raises an HTTP exception.

    This tests how HTTP errors (4xx, 5xx) are handled by the
    monitoring system.

    Returns:
        Never returns - always raises an exception

    Raises:
        HTTPException: 503 Service Unavailable
    """
    logger.warning("HTTP error test endpoint called")
    raise HTTPException(
        status_code=503,
        detail="Service temporarily unavailable - this is a test error",
    )


@router.get("/slow")
async def test_slow_endpoint() -> Dict[str, str]:
    """
    Test endpoint that simulates a slow operation.

    This is useful for testing:
    - Request duration metrics
    - Span timing
    - Performance monitoring

    Returns:
        Dict with timing information
    """
    tracer = get_tracer(__name__)

    with tracer.start_as_current_span("slow_operation") as span:
        span.set_attribute("operation.type", "slow")

        # Simulate slow operation (2 seconds)
        await asyncio.sleep(2.0)

        span.set_attribute("operation.duration_ms", 2000)

        return {
            "status": "success",
            "message": "Slow operation completed",
            "duration_ms": 2000,
            "info": "Check metrics for request duration histogram",
        }


@router.get("/health-check")
async def test_health_check() -> Dict[str, str]:
    """
    Simple health check endpoint for monitoring.

    This endpoint always succeeds and can be used for:
    - Uptime monitoring
    - Basic connectivity testing
    - Success metric baseline

    Returns:
        Dict with health status
    """
    return {
        "status": "healthy",
        "message": "Monitoring test endpoints are working",
        "endpoints": {
            "error": "/api/test/error",
            "critical": "/api/test/critical",
            "trace": "/api/test/trace",
            "http-error": "/api/test/http-error",
            "slow": "/api/test/slow",
        },
    }
