from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.mcp_server.tooling.trade_profile_tools as trade_profile_tools
from app.mcp_server.tooling.trade_profile_tools import (
    _apply_profile_rules,
    get_asset_profile,
    set_asset_profile,
)
from app.models.trade_profile import ProfileChangeLog
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


def _fake_asset_profile(**overrides: object) -> SimpleNamespace:
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
    return SimpleNamespace(**defaults)


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
    change_logs = [
        cast(ProfileChangeLog, o) for o in added if hasattr(o, "change_type")
    ]
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
    change_logs = [
        cast(ProfileChangeLog, o) for o in added if hasattr(o, "change_type")
    ]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "asset_profile"


def _fake_tier_rule(**overrides: object) -> SimpleNamespace:
    now = datetime.now(tz=UTC)
    defaults: dict[str, object] = {
        "id": 10,
        "user_id": 1,
        "instrument_type": InstrumentType.equity_us,
        "tier": 2,
        "profile": "balanced",
        "param_type": "buy",
        "params": {"position_size_pct": 0.25},
        "version": 1,
        "updated_by": "mcp",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _fake_market_filter(**overrides: object) -> SimpleNamespace:
    now = datetime.now(tz=UTC)
    defaults: dict[str, object] = {
        "id": 20,
        "user_id": 1,
        "instrument_type": InstrumentType.crypto,
        "filter_name": "kill_switch",
        "params": {"enabled": False},
        "enabled": True,
        "updated_by": "mcp",
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_get_tier_rule_params_supports_market_aliases() -> None:
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    session_factory = MagicMock(return_value=_build_session_cm(mock_session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = await trade_profile_tools.get_tier_rule_params(instrument_type="us")

    assert result == {"success": True, "data": [], "count": 0}
    stmt = mock_session.execute.await_args.args[0]
    compiled = stmt.compile()
    assert InstrumentType.equity_us in compiled.params.values()


@pytest.mark.asyncio
async def test_set_tier_rule_params_create_returns_created_contract_and_logs_snapshot() -> (
    None
):
    added: list[object] = []
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    created_rule = _fake_tier_rule(
        instrument_type=InstrumentType.equity_kr,
        tier=1,
        profile="aggressive",
        param_type="buy",
        params={"position_size_pct": 0.15},
        version=1,
    )

    async def _fake_refresh(obj: object) -> None:
        for attr in (
            "id",
            "instrument_type",
            "tier",
            "profile",
            "param_type",
            "params",
            "version",
            "updated_by",
            "created_at",
            "updated_at",
        ):
            setattr(obj, attr, getattr(created_rule, attr))

    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=_fake_refresh)
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = cast(
            dict[str, Any],
            await trade_profile_tools.set_tier_rule_params(
                instrument_type="kr",
                tier=1,
                profile="aggressive",
                param_type="buy",
                params={"position_size_pct": 0.15},
                reason="initial rule",
            ),
        )

    assert result["success"] is True
    assert result["action"] == "created"
    assert result["warning"] == "no active signal predicates"
    assert result["data"]["instrument_type"] == "equity_kr"
    assert result["data"]["version"] == 1

    change_logs = [obj for obj in added if isinstance(obj, ProfileChangeLog)]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "tier_rule_param"
    assert change_logs[0].target == "tier_rule:equity_kr:1:aggressive:buy"
    assert change_logs[0].old_value is None
    assert change_logs[0].new_value == {
        "instrument_type": "equity_kr",
        "tier": 1,
        "profile": "aggressive",
        "param_type": "buy",
        "params": {"position_size_pct": 0.15},
        "version": 1,
        "updated_by": "mcp",
    }


@pytest.mark.asyncio
async def test_set_tier_rule_params_update_increments_version_and_logs_old_new_values() -> (
    None
):
    added: list[object] = []
    existing = _fake_tier_rule(
        instrument_type=InstrumentType.crypto,
        tier=2,
        profile="balanced",
        param_type="sell",
        params={"take_profit_pct": 0.08},
        version=2,
    )
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = cast(
            dict[str, Any],
            await trade_profile_tools.set_tier_rule_params(
                instrument_type="crypto",
                tier=2,
                profile="balanced",
                param_type="sell",
                params={"take_profit_pct": 0.1},
                reason="raise sell threshold",
            ),
        )

    assert result["success"] is True
    assert result["action"] == "updated"
    assert result["data"]["version"] == 3
    assert "warning" not in result

    change_logs = [obj for obj in added if isinstance(obj, ProfileChangeLog)]
    assert len(change_logs) == 1
    assert change_logs[0].old_value == {
        "instrument_type": "crypto",
        "tier": 2,
        "profile": "balanced",
        "param_type": "sell",
        "params": {"take_profit_pct": 0.08},
        "version": 2,
        "updated_by": "mcp",
    }
    assert change_logs[0].new_value == {
        "instrument_type": "crypto",
        "tier": 2,
        "profile": "balanced",
        "param_type": "sell",
        "params": {"take_profit_pct": 0.1},
        "version": 3,
        "updated_by": "mcp",
    }


@pytest.mark.asyncio
async def test_set_tier_rule_params_buy_false_signal_flag_returns_warning() -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    created_rule = _fake_tier_rule(
        instrument_type=InstrumentType.crypto,
        tier=1,
        profile="balanced",
        param_type="buy",
        params={"macd_cross_required": False, "position_size_pct": 0.15},
        version=1,
    )

    async def _fake_refresh(obj: object) -> None:
        for attr in (
            "id",
            "instrument_type",
            "tier",
            "profile",
            "param_type",
            "params",
            "version",
            "updated_by",
            "created_at",
            "updated_at",
        ):
            setattr(obj, attr, getattr(created_rule, attr))

    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=_fake_refresh)
    session.add = MagicMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = cast(
            dict[str, Any],
            await trade_profile_tools.set_tier_rule_params(
                instrument_type="crypto",
                tier=1,
                profile="balanced",
                param_type="buy",
                params={"macd_cross_required": False, "position_size_pct": 0.15},
            ),
        )

    assert result["success"] is True
    assert result["warning"] == "no active signal predicates"


@pytest.mark.asyncio
async def test_set_tier_rule_params_buy_active_predicate_has_no_warning() -> None:
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
    )

    created_rule = _fake_tier_rule(
        instrument_type=InstrumentType.equity_kr,
        tier=1,
        profile="aggressive",
        param_type="buy",
        params={"rsi14_max": 30, "position_size_pct": 0.15},
        version=1,
    )

    async def _fake_refresh(obj: object) -> None:
        for attr in (
            "id",
            "instrument_type",
            "tier",
            "profile",
            "param_type",
            "params",
            "version",
            "updated_by",
            "created_at",
            "updated_at",
        ):
            setattr(obj, attr, getattr(created_rule, attr))

    session.flush = AsyncMock()
    session.refresh = AsyncMock(side_effect=_fake_refresh)
    session.add = MagicMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = cast(
            dict[str, Any],
            await trade_profile_tools.set_tier_rule_params(
                instrument_type="kr",
                tier=1,
                profile="aggressive",
                param_type="buy",
                params={"rsi14_max": 30, "position_size_pct": 0.15},
            ),
        )

    assert result["success"] is True
    assert "warning" not in result


@pytest.mark.asyncio
async def test_set_market_filter_update_toggles_enabled_and_logs_snapshots() -> None:
    added: list[object] = []
    existing = _fake_market_filter(
        instrument_type=InstrumentType.equity_us,
        filter_name="fear_greed",
        params={"max_value": 75},
        enabled=True,
    )
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = await trade_profile_tools.set_market_filter(
            instrument_type="us",
            filter_name="fear_greed",
            params={"max_value": 65},
            enabled=False,
            reason="risk off",
        )

    assert result == {
        "success": True,
        "action": "updated",
        "data": {
            "id": 20,
            "instrument_type": "equity_us",
            "filter_name": "fear_greed",
            "params": {"max_value": 65},
            "enabled": False,
            "updated_by": "mcp",
            "created_at": existing.created_at.isoformat(),
            "updated_at": existing.updated_at.isoformat(),
        },
    }

    change_logs = [obj for obj in added if isinstance(obj, ProfileChangeLog)]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "market_filter"
    assert change_logs[0].target == "filter:equity_us:fear_greed"
    assert change_logs[0].old_value == {
        "instrument_type": "equity_us",
        "filter_name": "fear_greed",
        "params": {"max_value": 75},
        "enabled": True,
        "updated_by": "mcp",
    }
    assert change_logs[0].new_value == {
        "instrument_type": "equity_us",
        "filter_name": "fear_greed",
        "params": {"max_value": 65},
        "enabled": False,
        "updated_by": "mcp",
    }


@pytest.mark.asyncio
async def test_delete_asset_profile_returns_deleted_action_and_logs_deleted_snapshot() -> (
    None
):
    added: list[object] = []
    session = MagicMock()
    existing = _fake_asset_profile(
        instrument_type=InstrumentType.equity_kr,
        symbol="005930",
        tier=3,
        profile="balanced",
    )
    session.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: existing)
    )
    session.delete = AsyncMock()
    session.add = MagicMock(side_effect=lambda obj: added.append(obj))
    tx_cm = AsyncMock()
    tx_cm.__aenter__.return_value = None
    tx_cm.__aexit__.return_value = None
    session.begin = MagicMock(return_value=tx_cm)

    session_factory = MagicMock(return_value=_build_session_cm(session))
    with patch(
        "app.mcp_server.tooling.trade_profile_tools._session_factory",
        return_value=session_factory,
    ):
        result = cast(
            dict[str, Any],
            await trade_profile_tools.delete_asset_profile(
                symbol="5930",
                market_type="kr",
                reason="remove profile",
            ),
        )

    assert result["success"] is True
    assert result["action"] == "deleted"
    assert result["data"]["symbol"] == "005930"
    session.delete.assert_awaited_once_with(existing)

    change_logs = [obj for obj in added if isinstance(obj, ProfileChangeLog)]
    assert len(change_logs) == 1
    assert change_logs[0].change_type == "asset_profile"
    assert change_logs[0].old_value == {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "tier": 3,
        "profile": "balanced",
        "sector": None,
        "tags": None,
        "max_position_pct": None,
        "buy_allowed": True,
        "sell_mode": "any",
        "note": None,
        "updated_by": "mcp",
    }
    assert change_logs[0].new_value == {"deleted": True}


@pytest.mark.asyncio
async def test_prepare_trade_draft_wrapper_returns_error_payload_instead_of_raising() -> (
    None
):
    with patch(
        "app.mcp_server.tooling.trade_profile_draft_engine.prepare_trade_draft_impl",
        new=AsyncMock(side_effect=RuntimeError("draft loader boom")),
    ):
        result = await trade_profile_tools.prepare_trade_draft(instrument_type="crypto")

    assert result == {
        "success": False,
        "error": "prepare_trade_draft failed: draft loader boom",
    }
