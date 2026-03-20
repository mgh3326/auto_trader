from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_server.tooling.trade_profile_tools import (
    _apply_profile_rules,
    get_asset_profile,
    set_asset_profile,
    set_tier_rule_params,
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
async def test_set_asset_profile_create_requires_market_type() -> None:
    """Without market_type and no existing row, creation must fail."""
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
        result = await set_asset_profile(symbol="AAPL")

    assert result == {
        "success": False,
        "error": "market_type is required for new profile",
    }


@pytest.mark.asyncio
async def test_set_asset_profile_create_requires_tier_and_profile() -> None:
    """With market_type but no existing row, tier and profile are required."""
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
        missing_tier = await set_asset_profile(symbol="AAPL", market_type="us")
        missing_profile = await set_asset_profile(
            symbol="AAPL", market_type="us", tier=2
        )

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


@pytest.mark.asyncio
async def test_get_asset_profile_rejects_invalid_market_type() -> None:
    result = await get_asset_profile(market_type="bond")

    assert result["success"] is False
    assert "market_type must be one of" in str(result["error"])


@pytest.mark.asyncio
async def test_set_asset_profile_rejects_invalid_market_type() -> None:
    result = await set_asset_profile(symbol="AAPL", market_type="bond")

    assert result["success"] is False
    assert "market_type must be one of" in str(result["error"])


# --- tier validation ---


@pytest.mark.asyncio
async def test_get_asset_profile_rejects_invalid_tier() -> None:
    result = await get_asset_profile(tier=5)

    assert result["success"] is False
    assert result["error"] == "tier must be 1-4"


@pytest.mark.asyncio
async def test_set_asset_profile_rejects_invalid_tier() -> None:
    result = await set_asset_profile(symbol="AAPL", market_type="us", tier=5)

    assert result["success"] is False
    assert result["error"] == "tier must be 1-4"


@pytest.mark.asyncio
async def test_get_asset_profile_rejects_zero_tier() -> None:
    result = await get_asset_profile(tier=0)

    assert result["success"] is False
    assert result["error"] == "tier must be 1-4"


# --- profile validation ---


@pytest.mark.asyncio
async def test_get_asset_profile_rejects_invalid_profile() -> None:
    result = await get_asset_profile(profile="invalid")

    assert result["success"] is False
    assert "Invalid profile" in str(result["error"])


@pytest.mark.asyncio
async def test_set_asset_profile_rejects_invalid_profile() -> None:
    result = await set_asset_profile(
        symbol="AAPL", market_type="us", tier=2, profile="invalid"
    )

    assert result["success"] is False
    assert "Invalid profile" in str(result["error"])


# --- change_type = 'asset_profile' ---


def _fake_asset_profile(**overrides: object) -> MagicMock:
    """Build a MagicMock that quacks like an AssetProfile row."""
    now = datetime.now(tz=UTC)
    defaults: dict[str, object] = {
        "id": 1,
        "user_id": 1,
        "symbol": "AAPL",
        "instrument_type": InstrumentType.equity_us,
        "tier": 2,
        "profile": "balanced",
        "sector": None,
        "tags": None,
        "max_position_pct": None,
        "buy_allowed": True,
        "sell_mode": "any",
        "note": None,
        "updated_by": "mcp",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _build_create_session() -> tuple[MagicMock, list[object]]:
    """Session mock for the *create* path (no existing row)."""
    added: list[object] = []
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    fake_row = _fake_asset_profile()

    async def _fake_flush() -> None:
        pass

    async def _fake_refresh(_obj: object) -> None:
        # After refresh the handler reads timestamps/id from the instance.
        # We swap the object's attrs so _serialize_profile works.
        for attr in (
            "id",
            "symbol",
            "instrument_type",
            "tier",
            "profile",
            "sector",
            "tags",
            "max_position_pct",
            "buy_allowed",
            "sell_mode",
            "note",
            "updated_by",
            "created_at",
            "updated_at",
        ):
            setattr(_obj, attr, getattr(fake_row, attr))

    session.flush = AsyncMock(side_effect=_fake_flush)
    session.refresh = AsyncMock(side_effect=_fake_refresh)
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    return session, added


def _build_update_session() -> tuple[MagicMock, list[object]]:
    """Session mock for the *update* path (existing row found)."""
    added: list[object] = []
    session = MagicMock()
    existing = _fake_asset_profile()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)
    )

    async def _fake_flush() -> None:
        pass

    async def _fake_refresh(_obj: object) -> None:
        pass

    session.flush = AsyncMock(side_effect=_fake_flush)
    session.refresh = AsyncMock(side_effect=_fake_refresh)
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))

    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    return session, added


@pytest.mark.asyncio
async def test_set_asset_profile_create_logs_change_type_asset_profile() -> None:
    session, added = _build_create_session()
    factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=factory,
    ):
        result = await set_asset_profile(
            symbol="AAPL",
            market_type="us",
            tier=2,
            profile="balanced",
        )

    assert result["success"] is True
    assert result["action"] == "created"
    change_logs = [o for o in added if hasattr(o, "change_type")]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "asset_profile"


@pytest.mark.asyncio
async def test_set_asset_profile_update_logs_change_type_asset_profile() -> None:
    session, added = _build_update_session()
    factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=factory,
    ):
        result = await set_asset_profile(
            symbol="AAPL",
            market_type="us",
            note="updated note",
        )

    assert result["success"] is True
    assert result["action"] == "updated"
    change_logs = [o for o in added if hasattr(o, "change_type")]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "asset_profile"


@pytest.mark.asyncio
async def test_set_tier_rule_params_invalid_tier() -> None:
    result = await set_tier_rule_params(
        instrument_type="us",
        tier=0,
        profile="balanced",
        param_type="buy",
        params={"size": 0.2},
    )

    assert result == {"success": False, "error": "tier must be 1-4"}


@pytest.mark.asyncio
async def test_set_tier_rule_params_invalid_profile() -> None:
    result = await set_tier_rule_params(
        instrument_type="us",
        tier=2,
        profile="unknown",
        param_type="buy",
        params={"size": 0.2},
    )

    assert result["success"] is False
    assert "Invalid profile" in str(result["error"])


@pytest.mark.asyncio
async def test_set_tier_rule_params_invalid_param_type() -> None:
    result = await set_tier_rule_params(
        instrument_type="us",
        tier=2,
        profile="balanced",
        param_type="invalid",
        params={"size": 0.2},
    )

    assert result == {
        "success": False,
        "error": "param_type must be one of: buy, sell, stop, rebalance, common",
    }
