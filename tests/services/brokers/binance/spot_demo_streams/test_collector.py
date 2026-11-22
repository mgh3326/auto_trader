import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest

from app.services.brokers.binance.demo.credentials import ResolvedDemoCredential
from app.services.brokers.binance.demo.errors import BinanceDemoMissingCredentials
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo_streams import collector as col
from app.services.brokers.binance.spot_demo_streams.collector import (
    FILLS_KEY,
    JsonlRecorder,
    SpotDemoStreamCollector,
    SpotDemoStreamsDisabled,
    StreamSettings,
    UserStreamSubscribeError,
    build_user_subscribe_request,
    market_book_url,
    parse_symbols,
    sign_ws_api_params,
)
from tests.services.brokers.binance.spot_demo_streams import fixtures as fx

CRED = ResolvedDemoCredential(
    api_key="KEY123", api_secret="SECRET456", credential_source="spot_demo_env"
)


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.fail = fail

    async def xadd(self, key, fields, **kw):
        if self.fail:
            raise ConnectionError("down")
        self.calls.append((key, fields, kw))


class FakeWS:
    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        return self.frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def close(self) -> None:
        self.closed = True


def _settings(**kw) -> StreamSettings:
    return StreamSettings(symbols=("BTCUSDT",), credential=CRED, **kw)


def _collector(settings, redis, connect, **kw) -> SpotDemoStreamCollector:
    async def no_sleep(_s: float) -> None:
        return None

    return SpotDemoStreamCollector(
        settings, redis, connect=connect, clock_ms=lambda: 1_000, sleep=no_sleep, **kw
    )


# ---- gate ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "false", "True", "1", "yes", " true"])
def test_gate_off_unless_exactly_true(value) -> None:
    env = {"BINANCE_SPOT_DEMO_API_KEY": "k", "BINANCE_SPOT_DEMO_API_SECRET": "s"}
    if value is not None:
        env[col.ENABLE_ENV] = value
    with pytest.raises(SpotDemoStreamsDisabled):
        StreamSettings.from_env(env, symbols="BTCUSDT")


def test_gate_on_resolves_existing_spot_demo_credential_names() -> None:
    env = {
        col.ENABLE_ENV: "true",
        "BINANCE_SPOT_DEMO_API_KEY": "k",
        "BINANCE_SPOT_DEMO_API_SECRET": "s",
    }
    s = StreamSettings.from_env(env, symbols="btcusdt, ethusdt")
    assert s.symbols == ("BTCUSDT", "ETHUSDT")
    assert (
        s.credential is not None and s.credential.credential_source == "spot_demo_env"
    )
    assert "secret" not in repr(s).lower().replace("credential", "")


def test_gate_on_without_credentials_fails_closed_but_market_only_works() -> None:
    env = {col.ENABLE_ENV: "true"}
    with pytest.raises(BinanceDemoMissingCredentials):
        StreamSettings.from_env(env, symbols="BTCUSDT")
    s = StreamSettings.from_env(env, symbols="BTCUSDT", enable_user_stream=False)
    assert s.credential is None


def test_symbols_validation() -> None:
    assert parse_symbols("btcusdt,BTCUSDT") == ("BTCUSDT",)
    for bad in ("", "BTC/USDT", "x", ",".join(f"AAA{i:02d}USDT" for i in range(21))):
        with pytest.raises(ValueError):
            parse_symbols(bad)


def test_market_url_shape() -> None:
    url = market_book_url(("BTCUSDT",))
    assert url == (
        "wss://demo-stream.binance.com:9443/stream?streams="
        "btcusdt@depth20/btcusdt@bookTicker/btcusdt@aggTrade"
    )


# ---- host allowlist before connect ---------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"market_url": "wss://stream.binance.com:9443/stream?streams=btcusdt@depth20"},
        {"api_url": "wss://ws-api.binance.com/ws-api/v3"},
        {"api_url": "wss://ws-api.testnet.binance.vision/ws-api/v3"},
    ],
)
def test_live_or_testnet_urls_refused_at_construction(kwargs) -> None:
    with pytest.raises(BinanceLiveHostBlocked):
        StreamSettings(symbols=("BTCUSDT",), **kwargs)


def test_dial_checks_host_before_any_connect() -> None:
    connected: list[str] = []

    async def spy(url: str, kind: str):
        connected.append(url)
        return FakeWS([])

    c = _collector(_settings(), FakeRedis(), spy)
    object.__setattr__(c._s, "market_url", "wss://stream.binance.com:9443/stream")
    with pytest.raises(BinanceLiveHostBlocked):
        asyncio.run(c._dial(c._s.market_url, "market"))
    assert connected == []


class _Req:
    def __init__(self, host: str, path: str) -> None:
        self.headers = {"Host": host}
        self.path = path


class _Dialed(FakeWS):
    def __init__(self, host: str, path: str) -> None:
        super().__init__([])
        self.request = _Req(host, path)


def test_open_socket_refuses_redirect_and_disables_proxy(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(url, **kw):
        seen["kw"] = kw
        return _Dialed("stream.binance.com:9443", "/stream")  # as if redirected

    monkeypatch.setattr(col.websockets, "connect", fake_connect)
    with pytest.raises(BinanceLiveHostBlocked):
        asyncio.run(col.open_socket(col.DEFAULT_WS_API_URL, "user"))
    assert seen["kw"]["proxy"] is None


def test_open_socket_accepts_same_host(monkeypatch) -> None:
    async def fake_connect(url, **kw):
        return _Dialed("demo-ws-api.binance.com", "/ws-api/v3")

    monkeypatch.setattr(col.websockets, "connect", fake_connect)
    ws = asyncio.run(col.open_socket(col.DEFAULT_WS_API_URL, "user"))
    assert ws.request.path == "/ws-api/v3"


def test_open_socket_refuses_before_connect_for_live_host(monkeypatch) -> None:
    called = False

    async def fake_connect(url, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(col.websockets, "connect", fake_connect)
    with pytest.raises(BinanceLiveHostBlocked):
        asyncio.run(col.open_socket("wss://stream.binance.com:9443/stream", "market"))
    assert called is False


# ---- market path -----------------------------------------------------------


def test_market_frames_land_in_book_stream(tmp_path) -> None:
    redis = FakeRedis()
    rec = JsonlRecorder(tmp_path)
    c = _collector(_settings(), redis, None, recorder=rec)

    async def go() -> None:
        for raw in (fx.DEPTH, fx.BOOK_TICKER, fx.AGG_TRADE, "garbage"):
            await c.handle_market_message(raw)

    asyncio.run(go())
    assert [k for k, _, _ in redis.calls] == ["book:binance_spot_demo:BTCUSDT"] * 3
    key, fields, kw = redis.calls[0]
    assert fields["kind"] == "depth" and fields["ts_received_ms"] == "1000"
    assert json.loads(fields["json"])["bids"][0][0] == "67000.01000000"
    assert kw == {"maxlen": 1000, "approximate": True}
    assert c.stats["market_frames"] == 3 and c.stats["dropped_malformed"] == 1
    lines = (
        (tmp_path / "binance_spot_demo_streams-19700101.jsonl").read_text().splitlines()
    )
    assert (
        len(lines) == 3 and json.loads(lines[0])["data"]["stream"] == "btcusdt@depth20"
    )


def test_unconfigured_symbol_dropped() -> None:
    redis = FakeRedis()
    c = _collector(StreamSettings(symbols=("ETHUSDT",), credential=CRED), redis, None)
    asyncio.run(c.handle_market_message(fx.DEPTH))
    assert redis.calls == [] and c.stats["dropped_malformed"] == 1


def test_redis_outage_does_not_stop_the_stream() -> None:
    c = _collector(_settings(), FakeRedis(fail=True), None)
    asyncio.run(c.handle_market_message(fx.DEPTH))
    assert c.stats["redis_errors"] == 1 and c.stats["market_frames"] == 1


# ---- user stream ------------------------------------------------------------


def test_signature_is_hmac_over_sorted_params() -> None:
    params = {"timestamp": 1, "apiKey": "KEY123", "recvWindow": 5000}
    expected = hmac.new(
        b"SECRET456", b"apiKey=KEY123&recvWindow=5000&timestamp=1", hashlib.sha256
    ).hexdigest()
    assert sign_ws_api_params(params, "SECRET456") == expected
    req = build_user_subscribe_request(CRED, request_id="r", timestamp_ms=1)
    assert req["method"] == "userDataStream.subscribe.signature"
    assert req["params"]["signature"] == expected and req["id"] == "r"


def test_user_session_subscribes_then_forwards_fills_only() -> None:
    redis = FakeRedis()
    ws = FakeWS(
        [
            fx.SUBSCRIBE_OK,
            fx.EXECUTION_REPORT,
            fx.ACCOUNT_POSITION,
            fx.LIST_STATUS,
            "garbage",
        ]
    )

    async def connect(url: str, kind: str):
        assert kind == "user"
        return ws

    c = _collector(_settings(), redis, connect)
    asyncio.run(c._user_session())
    assert len(ws.sent) == 1  # the subscribe request is the only frame ever sent
    assert json.loads(ws.sent[0])["method"] == "userDataStream.subscribe.signature"
    assert ws.closed
    assert [k for k, _, _ in redis.calls] == [FILLS_KEY, FILLS_KEY]
    assert json.loads(redis.calls[0][1]["json"])["order_status"] == "PARTIALLY_FILLED"
    assert c.stats["user_events"] == 2 and c.stats["ignored_user_events"] == 1
    assert c.stats["dropped_malformed"] == 1


def test_subscribe_rejection_raises_without_leaking_secrets(caplog) -> None:
    ws = FakeWS([fx.SUBSCRIBE_REJECTED])

    async def connect(url: str, kind: str):
        return ws

    c = _collector(_settings(), FakeRedis(), connect)
    with pytest.raises(UserStreamSubscribeError) as exc:
        asyncio.run(c._user_session())
    assert "SECRET456" not in str(exc.value) and "KEY123" not in str(exc.value)
    assert c.stats["subscribe_errors"] == 1


def test_server_shutdown_ends_session_for_reconnect() -> None:
    ws = FakeWS([fx.SUBSCRIBE_OK, fx.SERVER_SHUTDOWN, fx.EXECUTION_REPORT])

    async def connect(url: str, kind: str):
        return ws

    redis = FakeRedis()
    c = _collector(_settings(), redis, connect)
    asyncio.run(c._user_session())
    assert redis.calls == []  # frame after shutdown not consumed


def test_supervisor_reconnects_and_never_retries_host_violation() -> None:
    attempts = {"n": 0}

    async def connect(url: str, kind: str):
        attempts["n"] += 1
        if attempts["n"] >= 3:
            c.stop.set()
        raise ConnectionError("boom")

    c = _collector(_settings(), FakeRedis(), connect)
    asyncio.run(c._supervise("market", c._market_session))
    assert attempts["n"] == 3 and c.stats["reconnects"] >= 2

    async def blocked(url: str, kind: str):
        raise BinanceLiveHostBlocked("x")

    c2 = _collector(_settings(), FakeRedis(), blocked)
    with pytest.raises(BinanceLiveHostBlocked):
        asyncio.run(c2._supervise("market", c2._market_session))


def test_market_only_run_never_touches_user_host() -> None:
    kinds: list[str] = []

    async def connect(url: str, kind: str):
        kinds.append(kind)
        c.stop.set()
        return FakeWS([])

    c = _collector(
        StreamSettings(symbols=("BTCUSDT",), enable_user_stream=False),
        FakeRedis(),
        connect,
    )
    asyncio.run(c.run())
    assert kinds == ["market"]
