"""Shared market-events test helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from sqlalchemy import delete, select

MARKET_EVENTS_TEST_LOCK_ID = 128_534


@asynccontextmanager
async def market_events_test_lock():
    """Serialize DB-backed market_events tests that share the same tables."""
    from sqlalchemy import text

    from app.core.db import engine

    async with engine.connect() as guard:
        await guard.execute(
            text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"),
            {"lock_id": MARKET_EVENTS_TEST_LOCK_ID},
        )
        try:
            yield
        finally:
            await guard.execute(
                text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"),
                {"lock_id": MARKET_EVENTS_TEST_LOCK_ID},
            )


async def clean_non_tradingview_market_events(db_session) -> None:
    """Remove mutable market-events fixture rows while preserving TradingView seed data."""
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    non_tradingview_events = select(MarketEvent.id).where(
        MarketEvent.source != "tradingview"
    )
    await db_session.execute(
        delete(MarketEventValue).where(
            MarketEventValue.event_id.in_(non_tradingview_events)
        )
    )
    await db_session.execute(
        delete(MarketEvent).where(MarketEvent.source != "tradingview")
    )
    await db_session.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.source != "tradingview"
        )
    )
    await db_session.commit()


def build_market_events_app(*, authenticated: bool = True, user_id: int = 7) -> FastAPI:
    """Build a FastAPI app with the market-events router and optional auth override."""
    from app.core.db import AsyncSessionLocal, get_db
    from app.routers import market_events
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(market_events.router)

    if authenticated:
        app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(
            id=user_id
        )

    async def _override_get_db():
        async with AsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app
