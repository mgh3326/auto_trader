import pytest

from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.host_allowlist import (
    PUBLIC_FUTURES_STREAM_HOSTS,
    PUBLIC_HOSTS,
)
from app.services.brokers.binance.spot_demo.host_allowlist import (
    _DEPRECATED_TESTNET_HOSTS,
    SPOT_DEMO_HOSTS,
)
from app.services.brokers.binance.spot_demo_streams.host_allowlist import (
    DEFAULT_MARKET_STREAM_URL_BASE,
    DEFAULT_WS_API_URL,
    DENIED_STREAM_HOSTS,
    SPOT_DEMO_STREAM_HOSTS,
    assert_spot_demo_stream_url,
)


def test_exact_demo_only_hosts() -> None:
    assert SPOT_DEMO_STREAM_HOSTS == frozenset(
        {"demo-stream.binance.com", "demo-ws-api.binance.com"}
    )


def test_disjoint_from_every_other_binance_allowlist() -> None:
    for other in (
        SPOT_DEMO_HOSTS,
        PUBLIC_HOSTS,
        PUBLIC_FUTURES_STREAM_HOSTS,
        _DEPRECATED_TESTNET_HOSTS,
    ):
        assert SPOT_DEMO_STREAM_HOSTS.isdisjoint(other)
    assert SPOT_DEMO_STREAM_HOSTS.isdisjoint(DENIED_STREAM_HOSTS)


def test_the_rest_allowlist_did_not_grow() -> None:
    assert SPOT_DEMO_HOSTS == frozenset({"demo-api.binance.com"})


def test_defaults_pass() -> None:
    assert_spot_demo_stream_url(
        DEFAULT_MARKET_STREAM_URL_BASE + "?streams=btcusdt@depth20", kind="market"
    )
    assert_spot_demo_stream_url(DEFAULT_WS_API_URL, kind="user")
    assert_spot_demo_stream_url("wss://demo-stream.binance.com/ws", kind="market")
    assert_spot_demo_stream_url(
        "wss://demo-stream.binance.com:443/stream", kind="market"
    )


@pytest.mark.parametrize(
    "url",
    [
        "wss://stream.binance.com:9443/stream",  # mainnet spot
        "wss://data-stream.binance.vision/stream",
        "wss://fstream.binance.com/stream",
        "wss://stream.testnet.binance.vision/stream",
        "wss://demo-stream.binance.com.evil.example/stream",  # suffix spoof
        "wss://demo-ws-api.binance.com/stream",  # wrong kind for host
        "ws://demo-stream.binance.com/stream",  # plain ws
        "https://demo-stream.binance.com/stream",
        "wss://user:pw@demo-stream.binance.com/stream",
        "wss://demo-stream.binance.com:8443/stream",
        "wss://demo-stream.binance.com/other",
        "wss://127.0.0.1/stream",
        "wss://demo-stream.binance.com/stream#x",
        "",
    ],
)
def test_market_url_refused(url: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_spot_demo_stream_url(url, kind="market")


@pytest.mark.parametrize(
    "url",
    [
        "wss://ws-api.binance.com/ws-api/v3",  # mainnet ws-api
        "wss://ws-api.testnet.binance.vision/ws-api/v3",
        "wss://demo-stream.binance.com/ws-api/v3",  # market host on user kind
        "wss://demo-ws-api.binance.com/ws",
        "wss://demo-api.binance.com/ws-api/v3",  # signed REST host
    ],
)
def test_user_url_refused(url: str) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        assert_spot_demo_stream_url(url, kind="user")
