import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import taskiq_fastapi
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.auth.admin_router import router as admin_router
from app.auth.router import router as auth_router
from app.auth.web_router import limiter
from app.auth.web_router import router as web_auth_router
from app.core.config import settings
from app.core.taskiq_broker import broker
from app.middleware.auth import AuthMiddleware
from app.monitoring.sentry import capture_exception, init_sentry
from app.monitoring.trade_notifier import get_trade_notifier
from app.routers import (
    analysis_json,
    dashboard,
    health,
    kis_domestic_trading,
    kis_overseas_trading,
    kospi200,
    manual_holdings,
    news_analysis,
    openclaw_callback,
    orderbook,
    portfolio,
    stock_latest,
    symbol_settings,
    test,
    trading,
    upbit_trading,
    websocket,
)

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure logging based on settings."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # uvicorn 로거도 동일 레벨로 설정
    logging.getLogger("uvicorn").setLevel(log_level)
    logging.getLogger("uvicorn.access").setLevel(log_level)


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    configure_logging()
    init_sentry(
        service_name="auto-trader-api",
        enable_fastapi=True,
        enable_sqlalchemy=True,
        enable_httpx=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Handle startup/shutdown lifecycle without deprecated hooks."""
        if not broker.is_worker_process:
            await broker.startup()

        await setup_monitoring()

        try:
            yield
        finally:
            await cleanup_monitoring()
            if not broker.is_worker_process:
                await broker.shutdown()

    app = FastAPI(
        title="KIS Auto Screener",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DOCS_ENABLED else None,
        redoc_url="/redoc" if settings.DOCS_ENABLED else None,
        openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
    )

    # Add slowapi state for rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Add global exception handler for detailed error logging
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Log all unhandled exceptions with full traceback"""
        capture_exception(
            exc,
            path=request.url.path,
            method=request.method,
            client=request.client.host if request.client else None,
        )
        logger.error(
            f"Unhandled exception on {request.method} {request.url.path}: {str(exc)}",
            exc_info=True,
            extra={
                "method": request.method,
                "url": str(request.url),
                "client": request.client.host if request.client else None,
            },
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    # Include routers
    app.include_router(auth_router)
    app.include_router(web_auth_router)
    app.include_router(admin_router)
    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(analysis_json.router)
    app.include_router(news_analysis.router)
    app.include_router(openclaw_callback.router)
    app.include_router(stock_latest.router)
    app.include_router(upbit_trading.router)
    app.include_router(kis_domestic_trading.router)
    app.include_router(kis_overseas_trading.router)
    app.include_router(symbol_settings.router)
    app.include_router(manual_holdings.router)
    app.include_router(orderbook.router)
    app.include_router(portfolio.router)
    app.include_router(trading.router)
    app.include_router(kospi200.router)
    app.include_router(websocket.router)
    if settings.EXPOSE_MONITORING_TEST_ROUTES:
        app.include_router(test.router)
    else:
        logger.debug("Monitoring test routes are disabled")

    # Add middlewares (order matters: last added = first executed)
    app.add_middleware(AuthMiddleware)

    taskiq_fastapi.init(broker, "app.main:api")

    return app


async def setup_monitoring() -> None:
    """
    Setup monitoring and observability for the application.

    This includes:
    - Telegram trade notifier
    """
    # Initialize Telegram trade notifier
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

        except Exception as e:
            logger.error(f"Failed to initialize trade notifier: {e}", exc_info=True)
    else:
        logger.info("Trade notifier is disabled (missing token or chat ID)")


async def cleanup_monitoring() -> None:
    """Cleanup monitoring resources."""
    # Shutdown trade notifier
    try:
        trade_notifier = get_trade_notifier()
        await trade_notifier.shutdown()
        logger.info("Trade notifier shutdown complete")
    except Exception as e:
        logger.error(f"Error during trade notifier shutdown: {e}", exc_info=True)


# Create app instance
api = create_app()
app = api
