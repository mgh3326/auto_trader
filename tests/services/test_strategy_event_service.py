from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def _make_refresh_effect(row_id: int = 1):
    """Return a side_effect that simulates DB populating server-generated fields."""

    async def _refresh(row):
        row.id = row_id
        if not row.event_uuid:
            row.event_uuid = uuid4()
        if not row.created_at:
            row.created_at = datetime.now(UTC)

    return _refresh


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_strategy_event_persists_and_returns_detail(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.refresh = AsyncMock(side_effect=_make_refresh_effect())

    # No session linkage path (session_uuid is None)
    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="Fed surprise hike 25bps",
        affected_markets=["us"],
        affected_themes=["rates", "macro"],
        affected_symbols=["AAPL", "QQQ"],
        severity=4,
        confidence=80,
        metadata={"note": "wire"},
    )

    detail = await strategy_event_service.create_strategy_event(
        db,
        request=req,
        user_id=7,
    )

    assert db.add.call_count == 1
    added = db.add.call_args.args[0]
    assert added.source_text == "Fed surprise hike 25bps"
    assert added.affected_themes == ["rates", "macro"]
    assert added.affected_symbols == ["AAPL", "QQQ"]
    assert added.session_id is None
    assert added.created_by_user_id == 7
    assert added.event_metadata == {"note": "wire"}

    # detail mirrors request fields, includes uuid + None session_uuid
    assert detail.session_uuid is None
    assert detail.affected_themes == ["rates", "macro"]
    assert detail.event_type == "operator_market_event"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_strategy_event_links_session_by_uuid(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    target_uuid = uuid4()

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock(side_effect=_make_refresh_effect())

    # `_resolve_session_id_for_uuid` should hit db.execute and unwrap scalar.
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: 42))

    req = StrategyEventCreateRequest(
        event_type="risk_veto",
        source_text="halt new buys due to gap risk",
        session_uuid=target_uuid,
    )
    detail = await strategy_event_service.create_strategy_event(
        db, request=req, user_id=7
    )
    assert db.execute.await_count == 1
    added = db.add.call_args.args[0]
    assert added.session_id == 42
    assert detail.session_uuid == target_uuid


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_strategy_event_unknown_session_uuid_raises(monkeypatch):
    from app.schemas.strategy_events import StrategyEventCreateRequest
    from app.services import strategy_event_service

    db = SimpleNamespace()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock(side_effect=_make_refresh_effect())
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="x",
        session_uuid=uuid4(),
    )
    with pytest.raises(strategy_event_service.UnknownSessionUUIDError):
        await strategy_event_service.create_strategy_event(db, request=req, user_id=7)
    db.add.assert_not_called()
