from __future__ import annotations

import pytest

from app.services.mock_trading_instance_registry import (
    MOCK_TRADING_INSTANCES,
    BrokerBackend,
    MarketScope,
    MockTradingInstanceError,
    get_mock_trading_instance,
)


def test_paper_001_registry_entry_is_broker_agnostic_kis_mock_mapping() -> None:
    instance = get_mock_trading_instance("paper_001")

    assert instance.slug == "paper_001"
    assert instance.display_name == "모의투자1"
    assert instance.broker_backend is BrokerBackend.KIS_MOCK
    assert instance.broker_backend.value == "kis_mock"
    assert instance.market_scope is MarketScope.KR
    assert instance.strategy_profile == "balanced_kr_mock"
    assert instance.persona_profile == "paper_001"


def test_paper_001_account_ref_is_env_backed_not_literal_account_value() -> None:
    instance = get_mock_trading_instance("paper_001")

    assert instance.broker_account_ref == "env:KIS_MOCK_ACCOUNT_NO"
    assert instance.broker_account_ref.startswith("env:")
    assert not any(ch.isdigit() for ch in instance.broker_account_ref)


def test_paper_001_never_resolves_to_live_backend() -> None:
    instance = get_mock_trading_instance("paper_001")

    assert not instance.is_live_backend
    assert "live" not in instance.broker_backend.value


def test_unknown_instance_lookup_fails_closed() -> None:
    with pytest.raises(MockTradingInstanceError, match="Unknown mock trading instance"):
        get_mock_trading_instance("paper_999")


def test_blank_instance_lookup_fails_closed() -> None:
    with pytest.raises(MockTradingInstanceError, match="slug is required"):
        get_mock_trading_instance("   ")


def test_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        MOCK_TRADING_INSTANCES["paper_999"] = get_mock_trading_instance("paper_001")  # type: ignore[index]
