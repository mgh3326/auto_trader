"""Minimal Chrome DevTools Protocol client for the operator audit CLI.

``CdpSession`` is the seam every consumer depends on; ``CdpClient`` is the real
implementation (httpx discovery + websockets session), exercised only by the
operator against a local Chrome. ``FakeCdpSession`` backs the unit tests.

Read-only: opens a NEW tab per URL, evaluates one expression, closes the tab.
Never touches pre-existing operator tabs.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.services.action_report.remote_debug_audit.host_allowlist import (
    assert_cdp_debug_host,
)


class CdpUnavailableError(RuntimeError):
    """Raised when the local Chrome remote-debug endpoint cannot be reached."""


class CdpSession(Protocol):
    async def fetch_rendered(
        self, url: str, js: str, *, timeout_s: float
    ) -> Any: ...


class FakeCdpSession:
    """Test double: returns canned ``fetch_rendered`` results keyed by URL."""

    def __init__(self, *, results: dict[str, Any]) -> None:
        self._results = results

    async def fetch_rendered(self, url: str, js: str, *, timeout_s: float) -> Any:
        if url not in self._results:
            raise RuntimeError(f"no canned result for {url!r}")
        value = self._results[url]
        if isinstance(value, Exception):
            raise value
        return value


class CdpClient:
    """Real CDP client. Host-locked to 127.0.0.1:9222 at construction.

    NOTE: ``fetch_rendered`` talks to a live browser; it is not unit-tested
    (no browser in CI). It is covered by the operator runbook smoke only.
    """

    def __init__(self, *, host_port: str = "127.0.0.1:9222") -> None:
        assert_cdp_debug_host(host_port)
        self._host_port = host_port

    async def fetch_rendered(self, url: str, js: str, *, timeout_s: float) -> Any:
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

            async def cmd(method: str, params: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
                nonlocal _msg_id
                _msg_id += 1
                payload: dict[str, Any] = {"id": _msg_id, "method": method, "params": params}
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
                # Give the page a beat to render, then read.
                await cmd("Runtime.enable", {}, session_id)
                evaluated = await cmd(
                    "Runtime.evaluate",
                    {"expression": js, "returnByValue": True, "awaitPromise": True},
                    session_id,
                )
                return evaluated.get("result", {}).get("result", {}).get("value")
            finally:
                if target_id is not None:
                    await cmd("Target.closeTarget", {"targetId": target_id})
