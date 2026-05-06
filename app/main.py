import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import taskiq_fastapi
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.responses import Response

from app.auth.admin_router import router as admin_router
from app.auth.router import router as auth_router
from app.auth.web_router import limiter
from app.auth.web_router import router as web_auth_router
from app.core.config import settings
from app.core.taskiq_broker import broker
from app.middleware.auth import AuthMiddleware
from app.middleware.csrf import TemplateFormCSRFMiddleware
from app.monitoring.sentry import capture_exception, init_sentry
from app.monitoring.trade_notifier import get_trade_notifier
from app.routers import (
    ai_markdown,
    alpaca_paper_ledger,
    candidate_discovery,
    deprecated_pages,
    health,
    kospi200,
    n8n,
    n8n_scan,
    news_analysis,
    news_radar,
    openclaw_callback,
    order_estimation,
    order_previews,
    pending_orders,
    portfolio,
    portfolio_actions,
    preopen,
    research_pipeline,
    research_retrospective,
    research_run_decision_sessions,
    screener,
    strategy_events,
    symbol_settings,
    test,
    trade_journals,
    trading,
    trading_decisions,
    trading_decisions_spa,
    user_defaults,
    watch_order_intent_ledger,
    websocket,
)
from app.services.error_serialization import (
    domain_error_status_code,
    is_domain_error,
    serialize_domain_error,
)

logger = logging.getLogger(__name__)


def _typed_rate_limit_exceeded_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Rate limit exceeded"},
    )


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
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Add slowapi state for rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _typed_rate_limit_exceeded_handler)

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

        if is_domain_error(exc):
            return JSONResponse(
                status_code=domain_error_status_code(exc),
                content=serialize_domain_error(exc),
            )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc)},
        )

    # Include routers
    app.include_router(auth_router)
    app.include_router(web_auth_router)
    app.include_router(admin_router)
    app.include_router(screener.router)
    app.include_router(pending_orders.router)
    app.include_router(health.router)
    app.include_router(news_analysis.router)
    app.include_router(n8n.router)
    app.include_router(n8n_scan.router)
    app.include_router(openclaw_callback.router)
    app.include_router(user_defaults.router)
    app.include_router(order_estimation.router)
    app.include_router(order_previews.router)
    app.include_router(symbol_settings.router)
    app.include_router(portfolio.router)
    app.include_router(ai_markdown.router)
    app.include_router(deprecated_pages.router)
    app.include_router(trading.router)
    app.include_router(trading_decisions.router)
    app.include_router(trading_decisions_spa.router)
    app.include_router(trade_journals.router)
    app.include_router(research_retrospective.router)
    app.include_router(research_pipeline.router)
    app.include_router(portfolio_actions.router)
    app.include_router(candidate_discovery.router)
    app.include_router(research_run_decision_sessions.router)
    app.include_router(preopen.router)
    app.include_router(news_radar.router)
    app.include_router(alpaca_paper_ledger.router)
    app.include_router(watch_order_intent_ledger.router)
    app.include_router(strategy_events.router)
    app.include_router(kospi200.router)
    app.include_router(websocket.router)
    if settings.EXPOSE_MONITORING_TEST_ROUTES:
        app.include_router(test.router)
    else:
        logger.debug("Monitoring test routes are disabled")

    # Add middlewares (order matters: last added = first executed)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        TemplateFormCSRFMiddleware,
        secret=settings.SECRET_KEY,
        exempt_urls=[
            re.compile(r"^/api/"),
            re.compile(r"^/auth/"),
            re.compile(r"^/admin/"),
            re.compile(r"^/n8n/"),
            re.compile(r"^/openclaw/"),
            re.compile(r"^/ws/"),
            re.compile(r"^/kis/"),
            re.compile(r"^/upbit/"),
            re.compile(r"^/screener/"),
            re.compile(r"^/analysis/"),
            re.compile(r"^/portfolio/"),
            re.compile(r"^/news/"),
            re.compile(r"^/stock/"),
            re.compile(r"^/symbol/"),
            re.compile(r"^/trading/"),
            re.compile(r"^/trade-journals/"),
            re.compile(r"^/kospi200/"),
            *deprecated_pages.legacy_exempt_url_patterns(),
        ],
    )

    taskiq_fastapi.init(broker, "app.main:api")

    return app


async def setup_monitoring() -> None:
    """
    Setup monitoring and observability for the application.

    This includes:
    - Discord webhook trade notifier (primary)
    - Telegram trade notifier (fallback)
    """
    # Check if any notification system is configured
    has_discord = any(
        [
            settings.discord_webhook_us,
            settings.discord_webhook_kr,
            settings.discord_webhook_crypto,
            settings.discord_webhook_alerts,
        ]
    )
    has_telegram = settings.telegram_token and settings.telegram_chat_id

    if not has_discord and not has_telegram:
        logger.info("Trade notifier is disabled (no Discord or Telegram configured)")
        return

    try:
        # Configure trade notifier with Discord and/or Telegram
        trade_notifier = get_trade_notifier()

        # Telegram is optional - use empty string if not configured
        bot_token = settings.telegram_token or ""
        chat_ids = settings.telegram_chat_ids if has_telegram else []

        trade_notifier.configure(
            bot_token=bot_token,
            chat_ids=chat_ids,
            enabled=True,
            discord_webhook_us=settings.discord_webhook_us,
            discord_webhook_kr=settings.discord_webhook_kr,
            discord_webhook_crypto=settings.discord_webhook_crypto,
            discord_webhook_alerts=settings.discord_webhook_alerts,
        )

        # Log what was configured
        configured_systems = []
        if has_discord:
            webhook_count = sum(
                [
                    bool(settings.discord_webhook_us),
                    bool(settings.discord_webhook_kr),
                    bool(settings.discord_webhook_crypto),
                    bool(settings.discord_webhook_alerts),
                ]
            )
            configured_systems.append(f"Discord ({webhook_count} webhook(s))")
        if has_telegram:
            configured_systems.append(f"Telegram (chat_id={settings.telegram_chat_id})")

        logger.info(f"Trade notifier initialized: {', '.join(configured_systems)}")

    except Exception as e:
        logger.error(f"Failed to initialize trade notifier: {e}", exc_info=True)


async def cleanup_monitoring() -> None:
    """Cleanup monitoring resources."""
    # Shutdown trade notifier
    try:
        trade_notifier = get_trade_notifier()
        await trade_notifier.shutdown()
        logger.info("Trade notifier shutdown complete")
    except Exception as e:
        logger.error(f"Error during trade notifier shutdown: {e}", exc_info=True)

    # Close KRX session
    try:
        from app.services.krx import _krx_session

        await _krx_session.close()
        logger.info("KRX session cleanup complete")
    except Exception as e:
        logger.error(f"Error during KRX session cleanup: {e}", exc_info=True)

    # Close KIS HTTP client
    try:
        from app.services.brokers.kis.client import kis

        await kis.close()
        logger.info("KIS client cleanup complete")
    except Exception as e:
        logger.error(f"Error during KIS client cleanup: {e}", exc_info=True)


# Create app instance
api = create_app()
app = api
