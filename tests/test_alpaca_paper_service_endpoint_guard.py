"""Unit tests for AlpacaPaperBrokerService endpoint guard invariants (ROB-57)."""

from unittest.mock import AsyncMock

import pytest

from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.endpoints import (
    DATA_BASE_URL,
    LIVE_TRADING_BASE_URL,
    PAPER_TRADING_BASE_URL,
)
from app.services.brokers.alpaca.exceptions import (
    AlpacaPaperEndpointError,
)
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.brokers.alpaca.transport import HTTPTransport


def _mock_transport() -> HTTPTransport:
    transport = AsyncMock(spec=HTTPTransport)
    return transport  # type: ignore[return-value]


def _paper_settings(**kwargs) -> AlpacaPaperSettings:
    return AlpacaPaperSettings(
        api_key=kwargs.get("api_key", "pk-test"),
        api_secret=kwargs.get("api_secret", "sk-test"),
        base_url=kwargs.get("base_url", PAPER_TRADING_BASE_URL),
    )


@pytest.mark.unit
def test_service_init_accepts_paper_endpoint():
    """Service initialises successfully with the paper endpoint."""
    svc = AlpacaPaperBrokerService(
        transport=_mock_transport(),
        settings=_paper_settings(),
    )
    assert svc is not None


@pytest.mark.unit
def test_service_init_rejects_live_endpoint():
    """Build with live trading URL raises AlpacaPaperEndpointError (invariant I2)."""
    with pytest.raises(AlpacaPaperEndpointError):
        AlpacaPaperBrokerService(
            transport=_mock_transport(),
            settings=_paper_settings(base_url=LIVE_TRADING_BASE_URL),
        )


@pytest.mark.unit
def test_service_init_rejects_data_endpoint_as_trading_base():
    """Build with data URL raises AlpacaPaperEndpointError (invariant I3)."""
    with pytest.raises(AlpacaPaperEndpointError):
        AlpacaPaperBrokerService(
            transport=_mock_transport(),
            settings=_paper_settings(base_url=DATA_BASE_URL),
        )


@pytest.mark.unit
def test_service_has_no_live_fallback_attribute():
    """Service exposes no live_* or fallback_* attribute (invariant I4)."""
    svc = AlpacaPaperBrokerService(
        transport=_mock_transport(),
        settings=_paper_settings(),
    )
    for attr in dir(svc):
        assert not attr.startswith("live_"), f"Unexpected live_ attribute: {attr}"
        assert not attr.startswith("fallback_"), (
            f"Unexpected fallback_ attribute: {attr}"
        )
