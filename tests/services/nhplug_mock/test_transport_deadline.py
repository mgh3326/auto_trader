"""The gate sits on the first TLS application write, after connection setup."""

from __future__ import annotations

from typing import Any

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
