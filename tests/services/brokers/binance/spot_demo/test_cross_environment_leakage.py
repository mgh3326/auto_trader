"""ROB-296 — Cross-environment leakage tests.

Per Hermes review §4: explicitly prove that
  * the existing testnet adapter rejects ``demo-api.binance.com``;
  * the new Spot Demo adapter rejects ``testnet.binance.vision``;
  * both adapters reject live/prod Binance hosts.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo.errors import (
    BinanceSpotDemoCrossAllowlistViolation,
)
from app.services.brokers.binance.spot_demo.transport import (
    _on_request as spot_demo_on_request,
)
from app.services.brokers.binance.spot_demo.transport import (
    build_spot_demo_client,
)
from app.services.brokers.binance.testnet.transport import (
    _on_request as testnet_on_request,
)
from app.services.brokers.binance.testnet.transport import (
    build_testnet_client,
)

# -----------------------------------------------------------------------------
# Testnet adapter must NOT accept Spot Demo or live hosts
# -----------------------------------------------------------------------------


def test_testnet_factory_rejects_spot_demo_base_url() -> None:
    """Spot Testnet adapter must reject the Spot Demo base URL."""
    with pytest.raises(BinanceLiveHostBlocked):
        build_testnet_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://demo-api.binance.com",
        )


def test_testnet_factory_rejects_live_base_url() -> None:
    """Spot Testnet adapter must reject the live mainnet base URL."""
    # The testnet transport raises BinanceLiveHostBlocked for any non-testnet
    # host; PUBLIC_HOSTS membership escalates the message but the exception
    # class hierarchy still includes BinanceLiveHostBlocked.
    with pytest.raises(BinanceLiveHostBlocked):
        build_testnet_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://api.binance.com",
        )


@pytest.mark.asyncio
async def test_testnet_request_hook_rejects_spot_demo_host() -> None:
    """Per-request testnet hook refuses a Spot Demo host."""
    request = httpx.Request("GET", "https://demo-api.binance.com/api/v3/account")
    with pytest.raises(BinanceLiveHostBlocked):
        await testnet_on_request(request)


# -----------------------------------------------------------------------------
# Spot Demo adapter must NOT accept testnet or live hosts
# -----------------------------------------------------------------------------


def test_spot_demo_factory_rejects_testnet_base_url() -> None:
    """Spot Demo adapter must reject the Spot Testnet base URL."""
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://testnet.binance.vision",
        )


def test_spot_demo_factory_rejects_live_base_url() -> None:
    """Spot Demo adapter must reject the live mainnet base URL."""
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        build_spot_demo_client(
            api_key="testkey",
            api_secret="testsecret",
            base_url="https://api.binance.com",
        )


@pytest.mark.asyncio
async def test_spot_demo_request_hook_rejects_testnet_host() -> None:
    """Per-request Spot Demo hook refuses a Spot Testnet host."""
    request = httpx.Request("GET", "https://testnet.binance.vision/api/v3/account")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        await spot_demo_on_request(request)


@pytest.mark.asyncio
async def test_spot_demo_request_hook_rejects_live_host() -> None:
    """Per-request Spot Demo hook refuses a live/mainnet host."""
    request = httpx.Request("GET", "https://api.binance.com/api/v3/account")
    with pytest.raises(BinanceSpotDemoCrossAllowlistViolation):
        await spot_demo_on_request(request)


@pytest.mark.parametrize(
    "host",
    [
        "api.binance.com",
        "fapi.binance.com",
        "stream.binance.com",
        "data-api.binance.vision",
    ],
)
@pytest.mark.asyncio
async def test_spot_demo_rejects_all_live_hosts(host: str) -> None:
    """Spot Demo rejects every documented live host (PUBLIC_HOSTS members)."""
    request = httpx.Request("GET", f"https://{host}/api/v3/account")
    # The exception is BinanceSpotDemoCrossAllowlistViolation for PUBLIC_HOSTS
    # members and BinanceLiveHostBlocked for non-allowlisted hosts.
    with pytest.raises((BinanceSpotDemoCrossAllowlistViolation, BinanceLiveHostBlocked)):
        await spot_demo_on_request(request)
