"""Fresh connection with a synchronous first application-write deadline."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any

import httpcore
import httpx


class FirstWriteDeadlineExceeded(RuntimeError):
    pass


def clock_boottime() -> float:
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class Stage2Timing:
    claim_window_seconds: int = 30
    first_write_seconds: int = 2
    lease_seconds: int = 120
    send_seconds: int = 30
    close_seconds: int = 5
    lock_timeout_ms: int = 5000
    intent_stale_seconds: int = 600

    @classmethod
    def from_env(cls) -> Stage2Timing:
        names = (
            "CLAIM_WINDOW_SECONDS",
            "FIRST_WRITE_SECONDS",
            "LEASE_SECONDS",
            "SEND_SECONDS",
            "CLOSE_SECONDS",
            "LOCK_TIMEOUT_MS",
            "INTENT_STALE_SECONDS",
        )
        defaults = cls()
        values = []
        for name, definition in zip(names, fields(defaults), strict=True):
            default = getattr(defaults, definition.name)
            raw = os.getenv("NHPLUG_STAGE2_" + name)
            if raw is None:
                values.append(default)
            elif raw.isascii() and raw.isdecimal() and int(raw) > 0:
                values.append(int(raw))
            else:
                raise ValueError("invalid NHPLUG Stage 2 timing configuration")
        timing = cls(*values)
        timing.validate()
        return timing

    def validate(self) -> None:
        if (
            type(self) is not Stage2Timing
            or any(
                type(value) is not int or value <= 0
                for value in (
                    self.claim_window_seconds,
                    self.first_write_seconds,
                    self.lease_seconds,
                    self.send_seconds,
                    self.close_seconds,
                    self.lock_timeout_ms,
                    self.intent_stale_seconds,
                )
            )
            or self.first_write_seconds >= self.lease_seconds
        ):
            raise ValueError("unsafe NHPLUG Stage 2 timing")


class _GatedStream(httpcore.AsyncNetworkStream):
    def __init__(
        self,
        stream: httpcore.AsyncNetworkStream,
        gate: GatedTransport,
        *,
        tls_ready: bool,
    ) -> None:
        self._stream = stream
        self._gate = gate
        self._tls_ready = tls_ready
        self._first_write = True

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout=timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self._tls_ready and self._first_write:
            # This check is synchronous and immediately precedes the delegated write.
            # There is no await between the check and the call into the stream.
            if self._gate.deadline is None or self._gate.clock() >= self._gate.deadline:
                raise FirstWriteDeadlineExceeded(
                    "first_write_deadline_exceeded_not_sent"
                )
            self._first_write = False
        await self._stream.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._stream.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        return _GatedStream(stream, self._gate, tls_ready=True)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _GatedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, gate: GatedTransport) -> None:
        self._backend = httpcore.AnyIOBackend()
        self._gate = gate

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _GatedStream(stream, self._gate, tls_ready=False)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        raise FirstWriteDeadlineExceeded("unix_socket_forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class GatedTransport(httpx.AsyncHTTPTransport):
    """One order, one connection pool, zero lower-layer retries."""

    def __init__(self, *, clock: Callable[[], float] = clock_boottime) -> None:
        super().__init__(
            retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            http2=False,
        )
        self.clock = clock
        self.deadline: float | None = None
        self._pool._network_backend = _GatedBackend(self)

    def arm(self, first_write_deadline: float) -> None:
        if self.deadline is not None or type(first_write_deadline) not in {int, float}:
            raise ValueError("NHPLUG first-write gate cannot be rearmed")
        self.deadline = float(first_write_deadline)

    async def hard_close(self, timeout: float) -> None:
        await asyncio.wait_for(self.aclose(), timeout=timeout)
