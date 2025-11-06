import logging

from fastapi import FastAPI

from app.core.config import settings
from app.middleware.monitoring import MonitoringMiddleware, setup_exception_handlers
from app.monitoring.error_reporter import TelegramErrorReporter, set_error_reporter
from app.monitoring.telemetry import (
    TelemetryConfig,
    TelemetryManager,
    set_telemetry_manager,
)
from app.routers import analysis_json, dashboard, health, stock_latest, upbit_trading

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="KIS Auto Screener", version="0.1.0")

    # Initialize monitoring components
    setup_monitoring(app)

    # Include routers
    # app.include_router(telegram.router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)
    app.include_router(stock_latest.router)
    app.include_router(upbit_trading.router)

    return app


def setup_monitoring(app: FastAPI) -> None:
    """
    Setup monitoring and observability for the application.

    This includes:
    - OpenTelemetry / SigNoz integration
    - Telegram error reporting
    - Monitoring middleware
    """
    # 1. Initialize OpenTelemetry / SigNoz
    if settings.telemetry_enabled:
        telemetry_config = TelemetryConfig(
            service_name=settings.service_name,
            otlp_endpoint=settings.otlp_endpoint,
            environment=settings.environment,
            enabled=True,
        )
        telemetry_manager = TelemetryManager(telemetry_config)
        telemetry_manager.initialize()
        telemetry_manager.instrument_fastapi(app)
        set_telemetry_manager(telemetry_manager)
        logger.info("Telemetry enabled and configured")
    else:
        logger.info("Telemetry is disabled")

    # 2. Initialize Telegram error reporter
    if settings.telegram_error_reporting_enabled:
        error_reporter = TelegramErrorReporter(
            bot_token=settings.telegram_token,
            chat_ids=settings.telegram_chat_ids,
            enabled=True,
            dedup_window_minutes=settings.telegram_error_dedup_minutes,
        )
        set_error_reporter(error_reporter)
        logger.info("Telegram error reporting enabled")
    else:
        logger.info("Telegram error reporting is disabled")

    # 3. Add monitoring middleware
    app.add_middleware(MonitoringMiddleware)
    logger.info("Monitoring middleware added")

    # 4. Setup startup and shutdown events
    @app.on_event("startup")
    async def on_startup():
        """Initialize async components on startup."""
        error_reporter = settings.telegram_error_reporting_enabled
        if error_reporter:
            from app.monitoring.error_reporter import get_error_reporter

            reporter = get_error_reporter()
            if reporter:
                await reporter.initialize()
                logger.info("Error reporter initialized")

    @app.on_event("shutdown")
    async def on_shutdown():
        """Cleanup resources on shutdown."""
        # Shutdown telemetry
        from app.monitoring.telemetry import get_telemetry_manager

        telemetry = get_telemetry_manager()
        if telemetry:
            telemetry.shutdown()
            logger.info("Telemetry shutdown complete")

        # Shutdown error reporter
        from app.monitoring.error_reporter import get_error_reporter

        reporter = get_error_reporter()
        if reporter:
            await reporter.shutdown()
            logger.info("Error reporter shutdown complete")

    # 5. Setup exception handlers
    setup_exception_handlers(app)


api = create_app()
