"""hk 5419 — pacing survives the MCP handler's fresh client per call.

``_kiwoom_mock_order_history_impl`` builds ``KiwoomMockClient.from_app_settings()``
on every invocation. Two calls through it with the same app key must still be
spaced by the TR interval (kt00009 is 1 request/second on the mock host).
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from app.mcp_server.tooling import orders_kiwoom_variants as mod
from app.services.brokers.kiwoom import client as client_mod
from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.client import KiwoomMockClient, _PerTrRateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_order_history_handler_spaces_same_key_kt00009_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    dispatches: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dispatches.append((request.headers[constants.HEADER_API_ID], clock.now))
        return httpx.Response(
            200,
            json={"return_code": 0, "acnt_ord_cntr_prst_array": []},
            headers={"cont-yn": "N", "next-key": ""},
        )

    settings = SimpleNamespace(
        kiwoom_mock_enabled=True,
        kiwoom_mock_base_url=constants.MOCK_BASE_URL,
        kiwoom_mock_app_key="HANDLER-KEY",
        kiwoom_mock_app_secret="HANDLER-SECRET",
        kiwoom_mock_account_no="HANDLER-ACCOUNT",
    )
    monkeypatch.setattr("app.core.config.settings", settings)
    monkeypatch.setitem(
        client_mod._SHARED_TR_LIMITERS,
        hashlib.sha256(b"HANDLER-KEY").hexdigest(),
        _PerTrRateLimiter(clock=clock, sleep=clock.sleep),
    )
    real_init = KiwoomMockClient.__init__
    constructed: list[KiwoomMockClient] = []

    def init_with_transport(self: KiwoomMockClient, *args, **kwargs) -> None:
        real_init(self, *args, **kwargs)
        self.set_transport_for_test(httpx.MockTransport(handler), token="TKN")
        constructed.append(self)

    monkeypatch.setattr(KiwoomMockClient, "__init__", init_with_transport)

    await mod._kiwoom_mock_order_history_impl()
    await mod._kiwoom_mock_order_history_impl()

    assert len(constructed) == 2 and constructed[0] is not constructed[1]
    assert [api for api, _ in dispatches] == ["kt00009", "kt00009"]
    assert dispatches[1][1] - dispatches[0][1] >= 1.2
