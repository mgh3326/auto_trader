import asyncio
import ssl

import pytest

from app.services.upbit_market_websocket import UpbitPublicWebSocketClient

pytestmark = pytest.mark.unit


def test_public_websocket_ssl_context_verifies_by_default() -> None:
    client = UpbitPublicWebSocketClient()

    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_public_websocket_rejects_explicit_insecure_mode() -> None:
    with pytest.raises(ValueError, match="verify_ssl=False is no longer supported"):
        UpbitPublicWebSocketClient(verify_ssl=False)


@pytest.mark.asyncio
async def test_recovery_success_is_emitted_after_subscription_before_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import websockets

    import app.services.upbit_market_websocket as websocket_module

    events: list[tuple[str, float | None]] = []

    class FakeSocket:
        async def send(self, _message: str) -> None:
            events.append(("send", None))

    async def fake_connect(*_args, **_kwargs) -> FakeSocket:
        return FakeSocket()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(websocket_module.time, "perf_counter", lambda: 14.5)
    monkeypatch.setattr(
        websocket_module,
        "record_reconnect_recovery",
        lambda **kwargs: events.append(("metric", kwargs["duration_seconds"])),
    )
    client = UpbitPublicWebSocketClient()
    client.current_attempt = 1
    client._recovery_started_at = 10.0

    async def fake_listen() -> None:
        events.append(("listen", None))

    monkeypatch.setattr(client, "_listen_for_messages", fake_listen)
    await client._connect_and_subscribe_internal()

    assert [event[0] for event in events] == ["send", "metric", "listen"]
    assert events[1] == ("metric", 4.5)
    assert client._recovery_started_at is None


@pytest.mark.asyncio
async def test_each_failed_recovery_gets_a_fresh_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.upbit_market_websocket as websocket_module

    ticks = iter([100.0, 100.5, 200.0, 201.0])
    durations: list[float] = []
    client = UpbitPublicWebSocketClient()
    client.max_reconnect_attempts = 1
    client.reconnect_delay = 0
    monkeypatch.setattr(websocket_module.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(
        websocket_module,
        "record_reconnect_recovery",
        lambda **kwargs: durations.append(kwargs["duration_seconds"]),
    )

    async def fail_connect() -> None:
        raise RuntimeError("fake websocket failure")

    monkeypatch.setattr(client, "_connect_and_subscribe_internal", fail_connect)
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: None)
    for _ in range(2):
        client.is_running = True
        client.current_attempt = 0
        await client.connect_and_subscribe()
        assert client._recovery_started_at is None

    assert durations == [0.5, 1.0]
