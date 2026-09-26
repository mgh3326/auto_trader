"""The gate sits on the first TLS application write, after connection setup."""

from __future__ import annotations

from typing import Any

import httpcore
import httpcore._backends.anyio as anyio_backend_module
import httpx
import pytest

from app.services.nhplug_mock.transport import (
    FirstWriteDeadlineExceeded,
    GatedTransport,
    _GatedStream,
)

pytestmark = pytest.mark.unit


class RecordingNetworkStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> RecordingNetworkStream:
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


@pytest.mark.asyncio
async def test_context_enters_then_first_application_write_checks_current_clock() -> (
    None
):
    current = [1.0]
    gate = GatedTransport(clock=lambda: current[0])
    gate.arm(2.0)
    network = RecordingNetworkStream()
    before_tls = _GatedStream(network, gate, tls_ready=False)
    await before_tls.write(b"TLS handshake")
    application = await before_tls.start_tls(None)
    current[0] = 2.1
    with pytest.raises(
        FirstWriteDeadlineExceeded, match="first_write_deadline_exceeded_not_sent"
    ):
        await application.write(b"POST /krstock/order/v1/cashBuy")
    assert network.writes == [b"TLS handshake"]
    await gate.aclose()


@pytest.mark.asyncio
async def test_on_time_application_write_occurs_once() -> None:
    gate = GatedTransport(clock=lambda: 1.0)
    gate.arm(2.0)
    network = RecordingNetworkStream()
    application = _GatedStream(network, gate, tls_ready=True)
    await application.write(b"POST /krstock/order/v1/cashBuy")
    assert network.writes == [b"POST /krstock/order/v1/cashBuy"]
    await gate.aclose()


class RecordingHttpcoreStream(httpcore.AsyncNetworkStream):
    """A socket-free peer for the real httpx to httpcore transport path."""

    _response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    def __init__(self, writes: list[tuple[str, bytes]], *, tls: bool = False) -> None:
        self.writes = writes
        self.tls = tls
        self._respond = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._respond:
            self._respond = False
            return self._response
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(("application" if self.tls else "plain", buffer))
        self._respond = True

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> RecordingHttpcoreStream:
        self.writes.append(("handshake", b""))
        return RecordingHttpcoreStream(self.writes, tls=True)

    def get_extra_info(self, info: str) -> Any:
        return None


def _socket_free_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, bytes]]:
    writes: list[tuple[str, bytes]] = []

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self, host: str, port: int, **kwargs: Any
        ) -> RecordingHttpcoreStream:
            writes.append(("connect", f"{host}:{port}".encode()))
            return RecordingHttpcoreStream(writes)

        async def connect_unix_socket(
            self, path: str, **kwargs: Any
        ) -> RecordingHttpcoreStream:
            raise AssertionError("unix socket is not part of this test")

        async def sleep(self, seconds: float) -> None:
            return None

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    monkeypatch.setattr(anyio_backend_module, "AnyIOBackend", Backend)
    return writes


@pytest.mark.asyncio
async def test_expired_gate_blocks_real_httpx_pipeline_application_write(
    monkeypatch: pytest.MonkeyPatch, allow_external_http: None
) -> None:
    writes = _socket_free_backend(monkeypatch)
    now = [10.0]
    transport = GatedTransport(clock=lambda: now[0])
    transport.arm(12.0)
    now[0] = 12.5
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        with pytest.raises(FirstWriteDeadlineExceeded):
            await http.post(
                "https://nhplug-gate-probe.invalid:8443/krstock/order/v1/cashBuy",
                json={"Input_0": {}},
            )
    assert [kind for kind, _ in writes if kind == "application"] == []


@pytest.mark.asyncio
async def test_live_gate_allows_one_real_httpx_pipeline_application_write(
    monkeypatch: pytest.MonkeyPatch, allow_external_http: None
) -> None:
    writes = _socket_free_backend(monkeypatch)
    transport = GatedTransport(clock=lambda: 10.0)
    transport.arm(12.0)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        response = await http.post(
            "https://nhplug-gate-probe.invalid:8443/krstock/order/v1/cashBuy",
            json={"Input_0": {}},
        )
    assert response.status_code == 200
    assert (
        len(
            [
                buffer
                for kind, buffer in writes
                if kind == "application" and buffer.startswith(b"POST ")
            ]
        )
        == 1
    )
