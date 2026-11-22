"""Spot Demo read-only stream collector (hk 6067, 6033 K4).

Subscribes, for configured symbols, to ``@depth20``/``@bookTicker``/
``@aggTrade`` on ``demo-stream.binance.com`` and to the user data stream on
``demo-ws-api.binance.com`` (``userDataStream.subscribe.signature``), then
writes:

  * Redis stream ``book:binance_spot_demo:<SYM>`` — market frames
  * Redis stream ``fills:binance_spot_demo``      — own execution reports
  * optional append-only JSONL recording

Hard boundaries (pinned by ``test_static_guard.py`` / ``test_collector.py``):

  * default off — ``BINANCE_SPOT_DEMO_STREAMS_ENABLED`` must be exactly ``true``
  * the stream URL is checked against ``host_allowlist`` before every dial and
    the connected socket's host/path is re-checked afterwards (redirects are
    refused); no URL override exists in env or CLI
  * the only frames ever SENT are the user-stream subscribe request; no order,
    cancel, REST or ledger code is imported or called
  * the fill stream writes nothing to any ledger — Redis only
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import websockets

from app.services.brokers.binance.demo.credentials import (
    ResolvedDemoCredential,
    resolve_demo_credentials,
)
from app.services.brokers.binance.errors import BinanceLiveHostBlocked
from app.services.brokers.binance.spot_demo_streams.host_allowlist import (
    DEFAULT_MARKET_STREAM_URL_BASE,
    DEFAULT_WS_API_URL,
    StreamKind,
    assert_spot_demo_stream_url,
)
from app.services.brokers.binance.spot_demo_streams.parsing import (
    FrameError,
    decode_json,
    parse_market_frame,
    parse_user_push,
)

logger = logging.getLogger(__name__)

ENABLE_ENV: Final[str] = "BINANCE_SPOT_DEMO_STREAMS_ENABLED"
SYMBOLS_ENV: Final[str] = "BINANCE_SPOT_DEMO_STREAMS_SYMBOLS"
VENUE: Final[str] = "binance_spot_demo"
FILLS_KEY: Final[str] = f"fills:{VENUE}"
MARKET_CHANNELS: Final[tuple[str, ...]] = ("depth20", "bookTicker", "aggTrade")
USER_SUBSCRIBE_METHOD: Final[str] = "userDataStream.subscribe.signature"
MAX_SYMBOLS: Final[int] = 20
DEFAULT_STREAM_MAXLEN: Final[int] = 1000
RECV_WINDOW_MS: Final[int] = 5000
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")


class SpotDemoStreamsDisabled(RuntimeError):
    """``BINANCE_SPOT_DEMO_STREAMS_ENABLED`` is not exactly ``true``."""


class UserStreamSubscribeError(RuntimeError):
    """The user-stream subscribe request was not accepted (status only)."""


def book_key(symbol: str) -> str:
    return f"book:{VENUE}:{symbol}"


def market_book_url(
    symbols: tuple[str, ...], base: str = DEFAULT_MARKET_STREAM_URL_BASE
) -> str:
    streams = "/".join(f"{s.lower()}@{ch}" for s in symbols for ch in MARKET_CHANNELS)
    return f"{base}?streams={streams}"


def parse_symbols(raw: str) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(p.strip().upper() for p in raw.split(",") if p.strip())
    )
    if not symbols:
        raise ValueError("at least one symbol is required")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} symbols")
    for s in symbols:
        if not _SYMBOL_RE.match(s):
            raise ValueError(f"invalid symbol {s!r}")
    return symbols


def sign_ws_api_params(params: Mapping[str, Any], api_secret: str) -> str:
    """HMAC-SHA256 hex over the alphabetically sorted ``k=v&...`` params
    (WS API signing rule); ``signature`` itself is excluded."""
    payload = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def build_user_subscribe_request(
    cred: ResolvedDemoCredential, *, request_id: str, timestamp_ms: int
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "apiKey": cred.api_key,
        "recvWindow": RECV_WINDOW_MS,
        "timestamp": timestamp_ms,
    }
    params["signature"] = sign_ws_api_params(params, cred.api_secret)
    return {"id": request_id, "method": USER_SUBSCRIBE_METHOD, "params": params}


@dataclass(frozen=True, slots=True)
class StreamSettings:
    symbols: tuple[str, ...]
    market_url: str = ""
    api_url: str = DEFAULT_WS_API_URL
    stream_maxlen: int = DEFAULT_STREAM_MAXLEN
    enable_user_stream: bool = True
    credential: ResolvedDemoCredential | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.market_url:
            object.__setattr__(self, "market_url", market_book_url(self.symbols))
        # Fail at construction, not at first dial.
        assert_spot_demo_stream_url(self.market_url, kind="market")
        assert_spot_demo_stream_url(self.api_url, kind="user")
        if self.stream_maxlen <= 0:
            raise ValueError("stream_maxlen must be positive")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        symbols: str | None = None,
        enable_user_stream: bool = True,
    ) -> StreamSettings:
        if environ.get(ENABLE_ENV) != "true":
            raise SpotDemoStreamsDisabled(
                f"{ENABLE_ENV} is not exactly 'true'; the Spot Demo stream "
                "collector is default-off."
            )
        syms = parse_symbols(
            symbols if symbols is not None else environ.get(SYMBOLS_ENV, "")
        )
        cred = resolve_demo_credentials("spot", environ) if enable_user_stream else None
        return cls(symbols=syms, enable_user_stream=enable_user_stream, credential=cred)


class JsonlRecorder:
    """Append-only, line-flushed JSONL recording (one file per UTC day)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, channel: str, ts_ms: int, payload: Any) -> None:
        day = datetime.fromtimestamp(ts_ms / 1000, UTC).strftime("%Y%m%d")
        line = json.dumps(
            {
                "venue": VENUE,
                "channel": channel,
                "ts_received_ms": ts_ms,
                "data": payload,
            },
            separators=(",", ":"),
        )
        with (self._dir / f"{VENUE}_streams-{day}.jsonl").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(line + "\n")


async def open_socket(url: str, kind: StreamKind) -> Any:
    """Dial ``url`` after the allowlist check; refuse redirects and proxies.

    ``websockets`` 15 follows HTTP redirects on its own, so the connected
    socket's request host/path is compared with what was asked for.
    """
    assert_spot_demo_stream_url(url, kind=kind)
    asked = urlsplit(url)
    ws = await websockets.connect(
        url, proxy=None, max_size=2**20, open_timeout=10, ping_interval=20
    )
    request = getattr(ws, "request", None)
    host = (getattr(request, "headers", {}).get("Host") or "").split(":")[0]
    path = (getattr(request, "path", "") or "").split("?")[0]
    if host != asked.hostname or path != asked.path:
        await ws.close()
        raise BinanceLiveHostBlocked("stream redirect refused")
    return ws


Connector = Callable[[str, StreamKind], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


class SpotDemoStreamCollector:
    def __init__(
        self,
        settings: StreamSettings,
        redis: Any,
        *,
        connect: Connector = open_socket,
        recorder: JsonlRecorder | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Sleeper = asyncio.sleep,
        backoff_max_s: float = 30.0,
    ) -> None:
        self._s = settings
        self._redis = redis
        self._connect = connect
        self._recorder = recorder
        self._clock = clock_ms
        self._sleep = sleep
        self._backoff_max = backoff_max_s
        self.stop = asyncio.Event()
        self.stats: dict[str, int] = {
            "market_frames": 0,
            "user_events": 0,
            "ignored_user_events": 0,
            "dropped_malformed": 0,
            "redis_errors": 0,
            "reconnects": 0,
            "subscribe_errors": 0,
        }

    async def _dial(self, url: str, kind: StreamKind) -> Any:
        assert_spot_demo_stream_url(url, kind=kind)  # before any socket
        return await self._connect(url, kind)

    async def _xadd(self, key: str, record: dict[str, Any], ts_ms: int) -> None:
        try:
            await self._redis.xadd(
                key,
                {
                    "ts_received_ms": str(ts_ms),
                    "kind": record["kind"],
                    "json": json.dumps(record, separators=(",", ":")),
                },
                maxlen=self._s.stream_maxlen,
                approximate=True,
            )
        except Exception:  # noqa: BLE001 — a Redis outage must not stop the stream
            self.stats["redis_errors"] += 1
            logger.warning("spot demo stream redis write failed key=%s", key)

    def _record(self, channel: str, ts_ms: int, payload: Any) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.write(channel, ts_ms, payload)
        except OSError:
            logger.warning("spot demo stream recording failed")

    async def handle_market_message(self, raw: str | bytes) -> None:
        ts = self._clock()
        try:
            obj = decode_json(raw)
            symbol, record = parse_market_frame(obj)
        except FrameError:
            self.stats["dropped_malformed"] += 1
            return
        if symbol not in self._s.symbols:
            self.stats["dropped_malformed"] += 1
            return
        self.stats["market_frames"] += 1
        await self._xadd(book_key(symbol), record, ts)
        self._record("market", ts, obj)

    async def handle_user_message(self, raw: str | bytes) -> bool:
        """Returns False when the server announced a shutdown (reconnect)."""
        ts = self._clock()
        try:
            obj = decode_json(raw)
            if "event" not in obj:
                return True  # subscribe/ack envelope, handled by the caller
            event = obj["event"]
            if isinstance(event, dict) and event.get("e") == "serverShutdown":
                return False
            record = parse_user_push(obj)
        except FrameError:
            self.stats["dropped_malformed"] += 1
            return True
        if record is None:
            self.stats["ignored_user_events"] += 1
            return True
        self.stats["user_events"] += 1
        await self._xadd(FILLS_KEY, record, ts)
        self._record("user", ts, obj)
        return True

    async def _market_session(self) -> None:
        ws = await self._dial(self._s.market_url, "market")
        try:
            async for raw in ws:
                await self.handle_market_message(raw)
                if self.stop.is_set():
                    return
        finally:
            await ws.close()

    async def _user_session(self) -> None:
        cred = self._s.credential
        if cred is None:
            raise UserStreamSubscribeError("no credential")
        ws = await self._dial(self._s.api_url, "user")
        try:
            request = build_user_subscribe_request(
                cred, request_id=f"sub-{self._clock()}", timestamp_ms=self._clock()
            )
            await ws.send(json.dumps(request))
            ack = decode_json(await ws.recv())
            if ack.get("status") != 200:
                err = ack.get("error") if isinstance(ack.get("error"), dict) else {}
                self.stats["subscribe_errors"] += 1
                # status/code only: never the request, key or signature.
                raise UserStreamSubscribeError(
                    f"subscribe rejected status={ack.get('status')} code={err.get('code')}"
                )
            async for raw in ws:
                if not await self.handle_user_message(raw) or self.stop.is_set():
                    return
        finally:
            await ws.close()

    async def _supervise(
        self, name: str, session: Callable[[], Awaitable[None]]
    ) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                await session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except BinanceLiveHostBlocked:
                raise  # a host violation is not retried
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "spot demo %s stream error: %s", name, type(exc).__name__
                )
            if self.stop.is_set():
                return
            self.stats["reconnects"] += 1
            await self._sleep(
                min(delay, self._backoff_max) * (0.5 + random.random() / 2)
            )  # noqa: S311
            delay = min(delay * 2, self._backoff_max)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._supervise("market", self._market_session))]
        if self._s.enable_user_stream:
            tasks.append(
                asyncio.create_task(self._supervise("user", self._user_session))
            )
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
