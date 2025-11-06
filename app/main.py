import logging

from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.config import settings
from app.middleware.monitoring import MonitoringMiddleware
from app.monitoring.error_reporter import get_error_reporter
from app.monitoring.telemetry import (
    instrument_fastapi,
    setup_telemetry,
    shutdown_telemetry,
)
from app.routers import analysis_json, dashboard, health, stock_latest, test, upbit_trading

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")

    # Setup monitoring on startup
    @app.on_event("startup")
    async def on_startup():
        """Initialize monitoring components on startup."""
        await setup_monitoring()

    # Cleanup on shutdown
    @app.on_event("shutdown")
    async def on_shutdown():
        """Cleanup monitoring resources on shutdown."""
        await cleanup_monitoring()

    # Include routers
    # app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)
    app.include_router(stock_latest.router)
    app.include_router(upbit_trading.router)
    app.include_router(test.router)  # Test endpoints for monitoring

    # Add monitoring middleware (must be added after startup event)
    app.add_middleware(MonitoringMiddleware)

    return app


async def setup_monitoring() -> None:
    """
    Setup monitoring and observability for the application.

    This includes:
    - OpenTelemetry / SigNoz integration
    - Telegram error reporting with Redis deduplication
    """
    # 1. Initialize OpenTelemetry / SigNoz
    if settings.SIGNOZ_ENABLED:
        try:
            setup_telemetry(
                service_name=settings.OTEL_SERVICE_NAME,
                service_version=settings.OTEL_SERVICE_VERSION,
                environment=settings.OTEL_ENVIRONMENT,
                otlp_endpoint=settings.SIGNOZ_ENDPOINT,
                enabled=True,
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
            # Get Redis client for error deduplication
            redis_client = Redis.from_url(
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
                redis_client=redis_client,
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


async def cleanup_monitoring() -> None:
    """Cleanup monitoring resources."""
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


# Create app instance
api = create_app()

# Instrument FastAPI for telemetry (after app creation, before startup)
if settings.SIGNOZ_ENABLED:
    try:
        # Note: This will be called after setup_telemetry() in startup event
        # We need to instrument after telemetry is initialized
        # So we do it in a separate startup event that runs after
        @api.on_event("startup")
        async def instrument_app():
            """Instrument FastAPI app for telemetry."""
            instrument_fastapi(api)
            logger.info("FastAPI instrumented for telemetry")
    except Exception as e:
        logger.error(f"Failed to setup FastAPI instrumentation: {e}", exc_info=True)
