"""Fake broker wires preserve isolated 1x, reduceOnly, account and fill proof."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.mock_strategy.crypto_brokers import (
    AlpacaCryptoPaper,
    BinanceCryptoDemo,
    _binance_evidence,
    build_broker,
)

pytestmark = pytest.mark.unit


def intent(side="short"):
    return SimpleNamespace(
        symbol="BTCUSDT",
        side=side,
        qty=Decimal("2"),
        client_order_id="r1474c-e-fixture",
    )


def body(order, **changes):
    result = {
        "symbol": order.symbol,
        "side": "BUY" if order.side in {"buy", "cover"} else "SELL",
        "type": "MARKET",
        "clientOrderId": order.client_order_id,
        "orderId": 42,
        "origQty": str(order.qty),
        "executedQty": str(order.qty),
        "avgPrice": "1000",
        "cummulativeQuoteQty": "2000",
        "status": "FILLED",
        "positionSide": "BOTH",
        "reduceOnly": order.side == "cover",
    }
    result.update(changes)
    return result


class Client:
    credential_fingerprint = "fixture-fingerprint"

    def __init__(self):
        self.calls = []
        self.margin = "isolated"
        self.leverage = 1

    async def get_symbol_info(self, **kwargs):
        return {
            "status": "TRADING",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                    "maxQty": "1000",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        }

    async def get_ticker_price(self, **kwargs):
        return Decimal("1000")

    async def get_position_mode(self):
        return SimpleNamespace(is_hedge_mode=False)

    async def get_position(self, *, symbol):
        return SimpleNamespace(
            symbol=symbol,
            require_position_side=lambda: "BOTH",
            position_amt=Decimal("-4"),
            leverage=self.leverage,
            margin_type=self.margin,
        )

    async def submit_order(self, **kwargs):
        self.calls.append(kwargs)
        order = intent("cover" if kwargs["reduce_only"] else "short")
        return SimpleNamespace(raw_response_redacted=body(order))


@pytest.mark.asyncio
async def test_futures_short_then_reduce_only_cover_above_old_cap(monkeypatch):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    client = Client()
    broker = BinanceCryptoDemo(
        client, futures=True, expected_fingerprint="fixture-fingerprint"
    )
    await broker.submit(intent(), confirm=True)
    await broker.closeable(intent(), Decimal("2"))
    await broker.submit(intent("cover"), confirm=True)
    assert [c["side"] for c in client.calls] == ["SELL", "BUY"]
    assert [c["reduce_only"] for c in client.calls] == [False, True]
    assert all(
        c["position_side"] == "BOTH" and c["confirm"] is True for c in client.calls
    )


@pytest.mark.parametrize("margin,leverage", [("cross", 1), (None, 1), ("isolated", 2)])
@pytest.mark.asyncio
async def test_futures_margin_or_leverage_mismatch_never_submits(
    monkeypatch, margin, leverage
):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    client = Client()
    client.margin, client.leverage = margin, leverage
    broker = BinanceCryptoDemo(
        client, futures=True, expected_fingerprint="fixture-fingerprint"
    )
    with pytest.raises(ValueError, match="isolated 1x"):
        await broker.submit(intent(), confirm=True)
    assert client.calls == []


def test_wrong_demo_identity_is_rejected():
    with pytest.raises(ValueError, match="identity mismatch"):
        BinanceCryptoDemo(
            Client(), futures=True, expected_fingerprint="another-account"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETHUSDT"},
        {"clientOrderId": "other"},
        {"origQty": "3"},
        {"executedQty": "0"},
        {"positionSide": None},
        {"reduceOnly": True},
    ],
)
def test_fill_echo_mismatch_refused(changes):
    with pytest.raises(ValueError):
        _binance_evidence(body(intent(), **changes), intent(), futures=True)


@pytest.mark.parametrize(
    "client_type,url",
    [
        (BinanceSpotDemoExecutionClient, "https://api.binance.com"),
        (BinanceFuturesDemoExecutionClient, "https://fapi.binance.com"),
    ],
)
def test_live_endpoint_still_refused_without_transport(client_type, url):
    with pytest.raises(Exception, match="[Ll][Ii][Vv][Ee]"):
        client_type(api_key="fake", api_secret="fake", base_url=url)


@pytest.mark.asyncio
async def test_missing_alpaca_asset_is_explicit_value_error():
    class Alpaca:
        async def list_assets(self, **kwargs):
            return []

    broker = AlpacaCryptoPaper(Alpaca(), None, account_ref="fixture")
    with pytest.raises(ValueError, match="symbol unavailable"):
        await broker.rules("BTCUSDT")


@pytest.mark.asyncio
async def test_spot_base_commission_is_subtracted_only_from_own_trades():
    class Spot(Client):
        async def get_order_trades(self, **kwargs):
            assert kwargs == {"symbol": "BTCUSDT", "order_id": "42"}
            return [
                {
                    "symbol": "BTCUSDT",
                    "orderId": 42,
                    "isBuyer": True,
                    "qty": "2",
                    "commission": "0.002",
                    "commissionAsset": "BTC",
                }
            ]

    broker = BinanceCryptoDemo(
        Spot(), futures=False, expected_fingerprint="fixture-fingerprint"
    )
    proof = await broker._evidence_with_commissions(body(intent("buy")), intent("buy"))
    assert proof.filled_qty == 2
    assert proof.held_qty == Decimal("1.998")


@pytest.mark.asyncio
async def test_default_disabled_before_client_construction(monkeypatch):
    monkeypatch.delenv("MOCK_STRATEGY_ENABLED", raising=False)
    with pytest.raises(ValueError, match="MOCK_STRATEGY_ENABLED"):
        await build_broker("binance_spot_demo", confirm=True)


@pytest.mark.asyncio
async def test_demo_http_or_path_override_refused_before_credentials(monkeypatch):
    monkeypatch.setenv("MOCK_STRATEGY_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_BASE_URL", "http://demo-api.binance.com")
    with pytest.raises(ValueError, match="exact demo HTTPS"):
        await build_broker("binance_spot_demo", confirm=True)
