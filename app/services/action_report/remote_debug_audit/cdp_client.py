"""Minimal Chrome DevTools Protocol client for the operator audit CLI.

``CdpSession`` is the seam every consumer depends on; ``CdpClient`` is the real
implementation (httpx discovery + websockets session), exercised only by the
operator against a local Chrome. ``FakeCdpSession`` backs the unit tests.

Read-only: opens a NEW tab per URL, evaluates one expression, closes the tab.
Never touches pre-existing operator tabs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from app.services.action_report.remote_debug_audit.host_allowlist import (
    assert_cdp_debug_host,
)

# Default cadence for the post-load readiness poll (overridable for tests).
_POLL_INTERVAL_S = 0.25

# An async CDP command channel: ``(method, params, session_id) -> response``.
CdpCommand = Callable[[str, dict[str, Any], str | None], Awaitable[dict[str, Any]]]


class CdpUnavailableError(RuntimeError):
    """Raised when the local Chrome remote-debug endpoint cannot be reached."""


class CdpSession(Protocol):
    async def fetch_rendered(
        self, url: str, js: str, *, timeout_s: float, ready_js: str | None = None
    ) -> Any: ...


class FakeCdpSession:
    """Test double: returns canned ``fetch_rendered`` results keyed by URL."""

    def __init__(self, *, results: dict[str, Any]) -> None:
        self._results = results

    async def fetch_rendered(
        self, url: str, js: str, *, timeout_s: float, ready_js: str | None = None
    ) -> Any:
        if url not in self._results:
            raise RuntimeError(f"no canned result for {url!r}")
        value = self._results[url]
        if isinstance(value, Exception):
            raise value
        return value


async def _evaluate(
    cmd: CdpCommand, expr: str, session_id: str | None, *, await_promise: bool
) -> Any:
    res = await cmd(
        "Runtime.evaluate",
        {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
        session_id,
    )
    return res.get("result", {}).get("result", {}).get("value")


async def await_rendered_value(
    cmd: CdpCommand,
    *,
    session_id: str | None,
    extract_js: str,
    ready_js: str | None,
    timeout_s: float,
    poll_interval_s: float = _POLL_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """Enable Page+Runtime, optionally wait for ``ready_js`` to gate on render,
    then run ``extract_js`` once and return its value.

    The readiness poll is bounded by the first of: ``ready_js`` returning True,
    ``timeout_s`` elapsing, or a hard max-poll cap (so a misbehaving clock can
    never produce an infinite loop). The final extraction always runs — a
    not-ready page yields a partial/None value that the caller treats as
    fail-open ``unavailable``. This never raises on a slow/blocked page.
    """
    await cmd("Page.enable", {}, session_id)
    await cmd("Runtime.enable", {}, session_id)

    if ready_js is not None:
        interval = poll_interval_s if poll_interval_s > 0 else _POLL_INTERVAL_S
        max_polls = max(1, int(timeout_s / interval) + 1)
        deadline = monotonic() + timeout_s
        for _ in range(max_polls):
            ready = await _evaluate(cmd, ready_js, session_id, await_promise=False)
            if ready is True:
                break
            if monotonic() >= deadline:
                break
            await sleep(interval)

    return await _evaluate(cmd, extract_js, session_id, await_promise=True)


class CdpClient:
    """Real CDP client. Host-locked to 127.0.0.1:9222 at construction.

    NOTE: ``fetch_rendered`` talks to a live browser; it is not unit-tested
    (no browser in CI). The render-wait orchestration it delegates to
    (``await_rendered_value``) is unit-tested via a fake command channel.
    """

    def __init__(self, *, host_port: str = "127.0.0.1:9222") -> None:
        assert_cdp_debug_host(host_port)
        self._host_port = host_port

    async def fetch_rendered(
        self, url: str, js: str, *, timeout_s: float, ready_js: str | None = None
    ) -> Any:
        from websockets.asyncio.client import connect as ws_connect

        # 1. Discover the browser-level websocket endpoint.
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(f"http://{self._host_port}/json/version")
                ws_url = resp.json()["webSocketDebuggerUrl"]
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear setup error
            raise CdpUnavailableError(
                f"no Chrome remote-debug at {self._host_port}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        target_id: str | None = None
        async with ws_connect(ws_url, open_timeout=timeout_s) as ws:
            _msg_id = 0

            async def cmd(
                method: str, params: dict[str, Any], session_id: str | None = None
            ) -> dict[str, Any]:
                nonlocal _msg_id
                _msg_id += 1
                payload: dict[str, Any] = {
                    "id": _msg_id,
                    "method": method,
                    "params": params,
                }
                if session_id is not None:
                    payload["sessionId"] = session_id
                await ws.send(json.dumps(payload))
                while True:
                    raw = json.loads(await ws.recv())
                    if raw.get("id") == _msg_id:
                        return raw

            try:
                created = await cmd("Target.createTarget", {"url": url})
                target_id = created["result"]["targetId"]
                attached = await cmd(
                    "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
                session_id = attached["result"]["sessionId"]
                # Wait for the page to render (bounded) before the final read.
                return await await_rendered_value(
                    cmd,
                    session_id=session_id,
                    extract_js=js,
                    ready_js=ready_js,
                    timeout_s=timeout_s,
                )
            finally:
                if target_id is not None:
                    await cmd("Target.closeTarget", {"targetId": target_id})
