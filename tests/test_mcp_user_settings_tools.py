from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling.user_settings_tools import (
    get_user_setting,
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
