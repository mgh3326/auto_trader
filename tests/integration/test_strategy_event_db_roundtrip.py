from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def async_db_session():
    """Real async Postgres session using DATABASE_URL from env/settings."""
    try:
        from app.core.config import settings

        db_url = settings.DATABASE_URL
    except Exception:
        pytest.skip("DATABASE_URL not configured — skipping integration test")

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def integration_user_id(async_db_session):
    """Create a dedicated FK target user for this integration test."""
    from app.models.trading import User

    suffix = uuid4().hex
    user = User(
        email=f"rob41-itest-{suffix}@example.com",
        username=f"rob41-itest-{suffix}",
        hashed_password="fakehash",  # noqa: S105 -- NOSONAR: Test fixture, not a real credential.
    )
    async_db_session.add(user)
    await async_db_session.flush()
    await async_db_session.refresh(user)
    return user.id


@pytest.mark.asyncio
async def test_strategy_event_round_trip(async_db_session, integration_user_id):
    """Themes/symbols/markets/sectors persist as structured JSON; FK linkage works."""
    from app.models.trading_decision import (
        SessionStatus,
        TradingDecisionSession,
    )
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    # 1) create a session row to link against
    session_row = TradingDecisionSession(
        session_uuid=uuid4(),
        user_id=integration_user_id,
        source_profile="rob41-itest",
        status=SessionStatus.open.value,
        generated_at=datetime.datetime.now(datetime.UTC),
    )
    async_db_session.add(session_row)
    await async_db_session.flush()

    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="round trip",
        session_uuid=session_row.session_uuid,
        affected_markets=["kr", "us"],
        affected_sectors=["semis"],
        affected_themes=["ai", "rates"],
        affected_symbols=["005930", "AAPL"],
        severity=3,
        confidence=70,
        metadata={"x": 1},
    )
    detail = await strategy_event_service.create_strategy_event(
        async_db_session, request=req, user_id=integration_user_id
    )
    await async_db_session.commit()

    fetched = await strategy_event_service.get_strategy_event_by_uuid(
        async_db_session, event_uuid=detail.event_uuid
    )
    assert fetched is not None
    assert fetched.session_uuid == session_row.session_uuid
    assert fetched.affected_markets == ["kr", "us"]
    assert fetched.affected_themes == ["ai", "rates"]
    assert fetched.affected_symbols == ["005930", "AAPL"]
    assert fetched.metadata == {"x": 1}

    listing = await strategy_event_service.list_strategy_events(
        async_db_session, session_uuid=session_row.session_uuid
    )
    assert listing.total == 1
    assert listing.events[0].event_uuid == detail.event_uuid
