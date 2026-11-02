"""Operator no-caps decision applies only behind the paper boundary."""

from decimal import Decimal

import pytest

from app.mcp_server.tooling.alpaca_paper_orders import (
    _manual_ceiling,
    alpaca_paper_submit_order,
)
from app.mcp_server.tooling.alpaca_paper_preview import PreviewOrderInput
from app.services.brokers.alpaca.config import AlpacaPaperSettings
from app.services.brokers.alpaca.exceptions import AlpacaPaperEndpointError
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "asset_class,symbol", [("us_equity", "AAPL"), ("crypto", "BTC/USD")]
)
@pytest.mark.parametrize(
    "size", [{"qty": Decimal("1000001")}, {"notional": Decimal("10000001")}]
)
@pytest.mark.asyncio
async def test_large_sizes_keep_exact_packet_and_confirm_gate(
    asset_class, symbol, size
):
    args = {
        "symbol": symbol,
        "side": "buy",
        "type": "limit",
        "limit_price": Decimal("50000"),
        "asset_class": asset_class,
    }
    if asset_class == "us_equity" and "notional" in size:
        args.update(type="market", limit_price=None)
    validated = PreviewOrderInput(**args, **size)
    assert _manual_ceiling(validated) == (size.get("notional"), size.get("qty"))
    outcome = await alpaca_paper_submit_order(**args, **size, confirm=False)
    assert outcome["submitted"] is False
    assert outcome["blocked_reason"] == "confirmation_required"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["qty", "notional", "limit_price"])
def test_invalid_numbers_still_refused(value, field):
    args = {
        "symbol": "BTC/USD",
        "side": "buy",
        "type": "limit",
        "qty": Decimal("1"),
        "limit_price": Decimal("100"),
        "asset_class": "crypto",
    }
    if field == "notional":
        args.pop("qty")
    args[field] = Decimal(value)
    with pytest.raises(ValueError):
        PreviewOrderInput(**args)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.invalid",
    ],
)
def test_large_order_support_does_not_allow_live_endpoints(url):
    with pytest.raises(AlpacaPaperEndpointError):
        AlpacaPaperBrokerService(
            settings=AlpacaPaperSettings(
                api_key="fake", api_secret="fake", base_url=url
            )
        )
