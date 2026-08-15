"""ROB-1250 shared pacing at the exact live KIS submit boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.vts_distributed_gate import DistributedGateUnavailable


class _LiveSettings:
    kis_app_key = "pacing-app-key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    kis_mock_base_url = "https://openapivts.koreainvestment.com:29443"
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    kis_api_rate_limits: dict[str, Any] = {}
    api_rate_limit_retry_429_max = 0
    api_rate_limit_retry_429_base_delay = 0.0


class _FakePacer:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events = events
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def acquire(
        self,
        scope_key: str,
        *,
        freshness_hook: Callable[[], Awaitable[None]] | None = None,
        call_class: str = "unknown",
    ) -> None:
        self.events.append("pacer")
        self.calls.append((scope_key, call_class))
        if self.error is not None:
            raise self.error
        if freshness_hook is not None:
            await freshness_hook()


class _LivePacingClient(BaseKISClient):
    def __init__(self, pacer: _FakePacer) -> None:  # type: ignore[override]
        self._is_mock_client = False
        super().__init__()
        self._live_order_pacer = pacer  # type: ignore[assignment]
        self.events: list[str] = pacer.events
        self.http_calls = 0

    @property  # type: ignore[override]
    def _settings(self) -> Any:  # type: ignore[override]
        return _LiveSettings()

    async def _get_limiter(self, api_key: str, *, rate: int, period: float) -> Any:
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        return limiter

    async def _ensure_client(self, timeout: float | None = None) -> Any:  # type: ignore[override]
        return MagicMock()

    async def _execute_http_request(  # type: ignore[override]
        self,
        client: Any,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        self.http_calls += 1
        self.events.append("http")
        response = MagicMock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {"rt_cd": "0", "output": {}, "msg1": "ok"}
        return response


async def _dispatch(
    client: _LivePacingClient,
    *,
    method: str = "POST",
    path: str = "/uapi/domestic-stock/v1/trading/order-cash",
    tr_id: str = "TTTC0012U",
    pre_send_hook: Callable[[], Awaitable[None]] | None = None,
) -> None:
    await client._dispatch_rate_limited_with_headers(
        method,
        f"https://openapi.koreainvestment.com:9443{path}",
        headers={"appkey": "pacing-app-key", "tr_id": tr_id},
        json_body={"PDNO": "005930"} if method == "POST" else None,
        timeout=1.0,
        api_name="order_submit",
        tr_id=tr_id,
        retry_request_errors=False,
        max_retries_override=0,
        pre_send_hook=pre_send_hook,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "tr_id"),
    [
        ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U"),
        ("/uapi/domestic-stock/v1/trading/order-cash", "TTTC0011U"),
        ("/uapi/overseas-stock/v1/trading/order", "TTTT1002U"),
        ("/uapi/overseas-stock/v1/trading/order", "TTTT1006U"),
    ],
)
async def test_each_exact_live_order_contract_claims_shared_pacer(
    path: str,
    tr_id: str,
):
    events: list[str] = []
    pacer = _FakePacer(events)
    client = _LivePacingClient(pacer)

    async def _fresh() -> None:
        events.append("fresh")

    await _dispatch(client, path=path, tr_id=tr_id, pre_send_hook=_fresh)

    assert client.http_calls == 1
    assert events == ["pacer", "fresh", "http"]
    assert len(pacer.calls) == 1
    scope_key, call_class = pacer.calls[0]
    assert scope_key.startswith("kis_live:order-gate:openapi.koreainvestment.com:9443:")
    assert "pacing-app-key" not in scope_key
    assert call_class == "order_submit"


@pytest.mark.asyncio
async def test_non_order_submit_contracts_bypass_live_pacer():
    events: list[str] = []
    pacer = _FakePacer(events)
    client = _LivePacingClient(pacer)

    await _dispatch(
        client,
        path="/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id="TTTC8434R",
    )

    assert client.http_calls == 1
    assert pacer.calls == []


@pytest.mark.asyncio
async def test_live_pacer_failure_prevents_http_dispatch():
    events: list[str] = []
    pacer = _FakePacer(events, DistributedGateUnavailable("pacer unavailable"))
    client = _LivePacingClient(pacer)

    with pytest.raises(DistributedGateUnavailable, match="pacer unavailable"):
        await _dispatch(client)

    assert client.http_calls == 0
    assert len(pacer.calls) == 1


def test_mock_or_unrelated_client_cannot_build_live_order_scope():
    events: list[str] = []
    client = _LivePacingClient(_FakePacer(events))
    client._is_mock_client = True

    assert (
        client._live_order_pacer_scope(
            "POST",
            "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash",
            {"appkey": "pacing-app-key"},
            "TTTC0012U",
        )
        is None
    )
