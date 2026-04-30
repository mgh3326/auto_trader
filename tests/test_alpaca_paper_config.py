"""Unit tests for Alpaca paper-trading settings configuration (ROB-57)."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError


def _make_settings(**overrides: str):
    """Instantiate a fresh Settings object with overrides applied via env."""
    base_env = {
        "ALPACA_PAPER_API_KEY": "test-key",
        "ALPACA_PAPER_API_SECRET": "test-secret",
        "ALPACA_PAPER_BASE_URL": PAPER_TRADING_BASE_URL,
    }
    base_env.update(overrides)
    with patch.dict(os.environ, base_env, clear=False):
        from app.core.config import Settings

        return Settings()


@pytest.mark.unit
def test_settings_default_paper_base_url_is_paper_api():
    """Default alpaca_paper_base_url must be the paper endpoint."""
    settings = _make_settings()
    assert str(settings.alpaca_paper_base_url) == PAPER_TRADING_BASE_URL


@pytest.mark.unit
def test_settings_rejects_live_trading_base_url():
    """Setting ALPACA_PAPER_BASE_URL to the live endpoint is rejected at settings load time.

    The validator raises ValueError which Pydantic wraps in ValidationError. The live URL
    cannot be used even at config level, satisfying invariant I2 at the earliest possible
    point in the stack.
    """
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(ALPACA_PAPER_BASE_URL="https://api.alpaca.markets")
    assert "forbidden URL" in str(exc_info.value) or "api.alpaca.markets" in str(
        exc_info.value
    )


@pytest.mark.unit
def test_settings_rejects_data_endpoint_as_trading_base_url():
    """Setting ALPACA_PAPER_BASE_URL to the data endpoint is rejected at settings load time."""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(ALPACA_PAPER_BASE_URL="https://data.alpaca.markets")
    assert "forbidden URL" in str(exc_info.value) or "data.alpaca.markets" in str(
        exc_info.value
    )


@pytest.mark.unit
def test_settings_requires_credentials():
    """Missing API key or secret raises AlpacaPaperConfigurationError when service is built."""
    from app.services.brokers.alpaca.config import AlpacaPaperSettings
    from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

    settings_no_key = AlpacaPaperSettings(
        api_key="",
        api_secret="test-secret",
        base_url=PAPER_TRADING_BASE_URL,
    )
    with pytest.raises(AlpacaPaperConfigurationError):
        AlpacaPaperBrokerService(settings=settings_no_key)

    settings_no_secret = AlpacaPaperSettings(
        api_key="test-key",
        api_secret="",
        base_url=PAPER_TRADING_BASE_URL,
    )
    with pytest.raises(AlpacaPaperConfigurationError):
        AlpacaPaperBrokerService(settings=settings_no_secret)
