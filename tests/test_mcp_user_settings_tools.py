from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling.user_settings_tools import (
    get_user_setting,
    set_user_setting,
)


def _build_session_cm(session: AsyncMock) -> AsyncMock:
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    return session_cm


@pytest.mark.asyncio
async def test_get_user_setting_returns_none_for_missing_key() -> None:
    """When no setting exists for the key, return None."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.user_settings_tools._session_factory",
        return_value=session_factory,
    ):
        result = await get_user_setting(key="nonexistent_key")

    assert result is None


@pytest.mark.asyncio
async def test_set_user_setting_upserts_and_serializes_updated_at() -> None:
    """Upsert should create or update a setting and return serialized result."""
    mock_session = MagicMock()

    # Create a fake UserSetting row for the second query (after upsert)
    fake_row = MagicMock()
    fake_row.key = "manual_cash"
    fake_row.value = {"amount": 15000000}
    fake_row.updated_at = datetime(2026, 4, 1, 8, 0, 0, tzinfo=UTC)

    execute_results = [
        SimpleNamespace(),
        SimpleNamespace(scalar_one=lambda: fake_row),
    ]
    mock_session.execute = AsyncMock(side_effect=execute_results)

    async def _fake_flush() -> None:
        pass

    async def _fake_refresh(obj: object) -> None:
        # Simulate DB assigning id and updated_at after flush
        obj.id = 1
        obj.updated_at = datetime(2026, 4, 1, 8, 0, 0, tzinfo=UTC)

    mock_session.flush = AsyncMock(side_effect=_fake_flush)
    mock_session.refresh = AsyncMock(side_effect=_fake_refresh)
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    mock_session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.user_settings_tools._session_factory",
        return_value=session_factory,
    ):
        result = await set_user_setting(
            key="manual_cash",
            value={"amount": 15000000},
        )

    assert result == {
        "key": "manual_cash",
        "value": {"amount": 15000000},
        "updated_at": "2026-04-01T08:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_get_user_setting_returns_json_value() -> None:
    """When a setting exists, return its JSON value."""
    # Create a fake UserSetting row
    fake_row = MagicMock()
    fake_row.key = "manual_cash"
    fake_row.value = {"amount": 10000000}
    fake_row.updated_at = datetime(2026, 3, 28, 10, 0, 0, tzinfo=UTC)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: fake_row)
    )

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.user_settings_tools._session_factory",
        return_value=session_factory,
    ):
        result = await get_user_setting(key="manual_cash")

    assert result == {"amount": 10000000}


def test_user_settings_tool_names_are_registered() -> None:
    """Verify that user_settings tools are properly registered."""
    from app.mcp_server.tooling.user_settings_registration import (
        USER_SETTINGS_TOOL_NAMES,
    )

    assert USER_SETTINGS_TOOL_NAMES == {"get_user_setting", "set_user_setting"}
