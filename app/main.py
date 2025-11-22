import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import settings
from app.middleware.monitoring import MonitoringMiddleware
from app.monitoring.error_reporter import get_error_reporter
from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.telemetry import (
    instrument_fastapi,
    setup_telemetry,
    shutdown_telemetry,
)
from app.auth.router import router as auth_router
from app.routers import analysis_json, dashboard, health, stock_latest, test, upbit_trading

logger = logging.getLogger(__name__)

# Module-level Redis client for proper cleanup
_redis_client: Optional[Redis] = None


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Handle startup/shutdown lifecycle without deprecated hooks."""
        # Initialize monitoring (sets up telemetry providers)
        await setup_monitoring()

        try:
            yield
        finally:
            await cleanup_monitoring()

    app = FastAPI(
        title="KIS Auto Screener",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DOCS_ENABLED else None,
        redoc_url="/redoc" if settings.DOCS_ENABLED else None,
    )

    # Add global exception handler for detailed error logging
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Log all unhandled exceptions with full traceback"""
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path}: {str(exc)}",
            exc_info=True,
            extra={
                "method": request.method,
                "url": str(request.url),
                "client": request.client.host if request.client else None,
            }
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)}
        )

    # Include routers
    app.include_router(auth_router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)
    app.include_router(stock_latest.router)
    app.include_router(upbit_trading.router)
    if settings.EXPOSE_MONITORING_TEST_ROUTES:
        app.include_router(test.router)
    else:
        logger.debug("Monitoring test routes are disabled")

    # Add monitoring middleware (must be added after startup event)
    app.add_middleware(MonitoringMiddleware)

    # Instrument FastAPI for telemetry (after all routes/middleware are added)
    # Note: This is safe because instrument_fastapi() only registers the app
    # for instrumentation. Actual telemetry setup happens in lifespan's setup_monitoring()
    if settings.OTEL_ENABLED:
        try:
            instrument_fastapi(app)
            logger.debug("FastAPI registered for telemetry instrumentation")
        except Exception as e:
            logger.error(
                f"Failed to register FastAPI for instrumentation: {e}",
                exc_info=True,
            )

    return app


async def setup_monitoring() -> None:
    """
    Setup monitoring and observability for the application.

    This includes:
    - OpenTelemetry / Grafana stack integration (Tempo, Loki, Prometheus)
    - Telegram error reporting with Redis deduplication
    """
    # 1. Initialize OpenTelemetry
    if settings.OTEL_ENABLED:
        try:
            setup_telemetry(
                service_name=settings.OTEL_SERVICE_NAME,
                service_version=settings.OTEL_SERVICE_VERSION,
                environment=settings.OTEL_ENVIRONMENT,
                otlp_endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
                enabled=True,
                insecure=settings.OTEL_INSECURE,
            )
            logger.info(
                f"Telemetry initialized: {settings.OTEL_SERVICE_NAME} "
                f"v{settings.OTEL_SERVICE_VERSION} ({settings.OTEL_ENVIRONMENT})"
            )
        except Exception as e:
            logger.error(f"Failed to initialize telemetry: {e}", exc_info=True)
    else:
        logger.info("Telemetry is disabled")

    # 2. Initialize Telegram error reporter
    if settings.ERROR_REPORTING_ENABLED:
        try:
            global _redis_client

            # Get Redis client for error deduplication
            _redis_client = Redis.from_url(
                settings.get_redis_url(),
                decode_responses=True,
                max_connections=settings.redis_max_connections,
            )

            # Configure error reporter
            error_reporter = get_error_reporter()
            error_reporter.configure(
                bot_token=settings.telegram_token,
                chat_id=settings.ERROR_REPORTING_CHAT_ID or (
                    settings.telegram_chat_ids[0] if settings.telegram_chat_ids else ""
                ),
                redis_client=_redis_client,
                enabled=True,
                duplicate_window=settings.ERROR_DUPLICATE_WINDOW,
            )

            logger.info(
                f"Error reporting initialized: "
                f"chat_id={settings.ERROR_REPORTING_CHAT_ID}, "
                f"duplicate_window={settings.ERROR_DUPLICATE_WINDOW}s"
            )

            # Test connection (optional)
            # await error_reporter.test_connection()

        except Exception as e:
            logger.error(f"Failed to initialize error reporting: {e}", exc_info=True)
    else:
        logger.info("Error reporting is disabled")

    # 3. Initialize Telegram trade notifier
    if settings.telegram_token and settings.telegram_chat_id:
        try:
            # Configure trade notifier
            trade_notifier = get_trade_notifier()
            trade_notifier.configure(
                bot_token=settings.telegram_token,
                chat_ids=settings.telegram_chat_ids,
                enabled=True,
            )

            logger.info(
                f"Trade notifier initialized: chat_id={settings.telegram_chat_id}"
            )

            # Test connection (optional)
            # await trade_notifier.test_connection()

        except Exception as e:
            logger.error(f"Failed to initialize trade notifier: {e}", exc_info=True)
    else:
        logger.info("Trade notifier is disabled (missing token or chat ID)")


async def cleanup_monitoring() -> None:
    """Cleanup monitoring resources."""
    global _redis_client

    # Shutdown telemetry
    try:
        shutdown_telemetry()
        logger.info("Telemetry shutdown complete")
    except Exception as e:
        logger.error(f"Error during telemetry shutdown: {e}", exc_info=True)

    # Shutdown error reporter
    try:
        error_reporter = get_error_reporter()
        await error_reporter.shutdown()
        logger.info("Error reporter shutdown complete")
    except Exception as e:
        logger.error(f"Error during error reporter shutdown: {e}", exc_info=True)

    # Shutdown trade notifier
    try:
        trade_notifier = get_trade_notifier()
        await trade_notifier.shutdown()
        logger.info("Trade notifier shutdown complete")
    except Exception as e:
        logger.error(f"Error during trade notifier shutdown: {e}", exc_info=True)

    # Explicitly close Redis client (backup safety measure)
    if _redis_client:
        try:
            await _redis_client.aclose()
            _redis_client = None
            logger.info("Redis client closed")
        except Exception as e:
            logger.error(f"Error closing Redis client: {e}", exc_info=True)


# Create app instance
api = create_app()
