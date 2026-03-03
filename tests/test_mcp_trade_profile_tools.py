from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling.trade_profile_tools import (
    _apply_profile_rules,
    _auto_detect_for_create,
    get_asset_profile,
    set_asset_profile,
)
from app.models.trading import InstrumentType


def _build_session_cm(session: AsyncMock) -> AsyncMock:
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    return session_cm


@pytest.mark.asyncio
async def test_get_asset_profile_returns_empty_when_no_profiles() -> None:
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = await get_asset_profile()

    assert result == {"success": True, "data": [], "count": 0}


@pytest.mark.asyncio
async def test_get_asset_profile_filters_by_market_type() -> None:
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = await get_asset_profile(market_type="kr")

    assert result == {"success": True, "data": [], "count": 0}
    stmt = mock_session.execute.await_args.args[0]
    compiled = stmt.compile()
    assert InstrumentType.equity_kr in compiled.params.values()


@pytest.mark.asyncio
async def test_set_asset_profile_create_requires_tier_and_profile() -> None:
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    mock_session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        missing_tier = await set_asset_profile(symbol="AAPL")
        missing_profile = await set_asset_profile(symbol="AAPL", tier=2)

    assert missing_tier == {
        "success": False,
        "error": "tier is required for new profile",
    }
    assert missing_profile == {
        "success": False,
        "error": "profile is required for new profile",
    }


def test_set_asset_profile_exit_forces_buy_allowed_false() -> None:
    buy_allowed, sell_mode = _apply_profile_rules(
        profile_value="exit",
        buy_allowed_value=True,
        sell_mode_value="any",
        requested_buy_allowed=None,
        requested_sell_mode=None,
    )

    assert buy_allowed is False
    assert sell_mode == "any"


def test_set_asset_profile_exit_rejects_buy_allowed_true() -> None:
    with pytest.raises(ValueError, match="profile=exit requires buy_allowed=False"):
        _apply_profile_rules(
            profile_value="exit",
            buy_allowed_value=True,
            sell_mode_value="any",
            requested_buy_allowed=True,
            requested_sell_mode=None,
        )


def test_set_asset_profile_hold_only_forces_sell_mode_rebalance() -> None:
    buy_allowed, sell_mode = _apply_profile_rules(
        profile_value="hold_only",
        buy_allowed_value=True,
        sell_mode_value="any",
        requested_buy_allowed=None,
        requested_sell_mode=None,
    )

    assert buy_allowed is True
    assert sell_mode == "rebalance_only"


def test_set_asset_profile_hold_only_rejects_invalid_sell_mode() -> None:
    with pytest.raises(
        ValueError,
        match="profile=hold_only requires sell_mode='rebalance_only'",
    ):
        _apply_profile_rules(
            profile_value="hold_only",
            buy_allowed_value=True,
            sell_mode_value="any",
            requested_buy_allowed=None,
            requested_sell_mode="any",
        )


def test_set_asset_profile_auto_detect_kr_symbol() -> None:
    instrument_type, normalized_symbol = _auto_detect_for_create("5930")

    assert instrument_type == InstrumentType.equity_kr
    assert normalized_symbol == "005930"


def test_set_asset_profile_auto_detect_crypto_symbol() -> None:
    instrument_type, normalized_symbol = _auto_detect_for_create("KRW-BTC")

    assert instrument_type == InstrumentType.crypto
    assert normalized_symbol == "KRW-BTC"
