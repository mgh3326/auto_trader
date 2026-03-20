"""
Test endpoints for monitoring and observability.

These endpoints are used to test:
- Error reporting
- Health checks
"""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/test", tags=["test"])


@router.get("/error")
async def test_error() -> dict[str, str]:
    """
    Test endpoint that raises a general error.

    This will trigger:
    - Error logging with full traceback

    Returns:
        Never returns - always raises an exception

    Raises:
        ValueError: Test error for monitoring
    """
    logger.info("Test error endpoint called")
    raise ValueError("This is a test error for monitoring system")


@router.get("/critical")
async def test_critical_error() -> dict[str, str]:
    """
    Test endpoint that raises a critical error.

    This simulates a critical system failure.

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
async def test_trace() -> dict[str, Any]:
    """
    Test endpoint for simulating traced operations.

    This endpoint demonstrates:
    - Nested operation timing
    - Multi-step workflow

    Returns:
        Dict with operation results and timing information
    """
    start_time = time.time()

    # Nested operation 1: Database query simulation
    await asyncio.sleep(0.05)  # 50ms

    # Nested operation 2: External API call simulation
    await asyncio.sleep(0.1)  # 100ms

    # Nested operation 3: Data processing simulation
    await asyncio.sleep(0.03)  # 30ms

    total_time = time.time() - start_time

    return {
        "status": "success",
        "message": "Trace test completed successfully",
        "total_duration_ms": f"{total_time * 1000:.2f}",
        "operations": {
            "db_query": "50ms",
            "api_call": "100ms",
            "data_processing": "30ms",
        },
    }


@router.get("/http-error")
async def test_http_error() -> dict[str, str]:
    """
    Test endpoint that raises an HTTP exception.

    This tests how HTTP errors (4xx, 5xx) are handled.

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
async def test_slow_endpoint() -> dict[str, Any]:
    """
    Test endpoint that simulates a slow operation.

    This is useful for testing:
    - Request duration
    - Performance monitoring

    Returns:
        Dict with timing information
    """
    # Simulate slow operation (2 seconds)
    await asyncio.sleep(2.0)

    return {
        "status": "success",
        "message": "Slow operation completed",
        "duration_ms": 2000,
    }


@router.get("/health-check")
async def test_health_check() -> dict[str, Any]:
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
