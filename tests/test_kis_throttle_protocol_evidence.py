"""Synthetic proof for throttle protocol evidence (ROB-s257 E-1b).

The test transport intentionally includes synthetic credentials in request URLs,
request settings, response headers, and response bodies.  The one structured
throttle event must retain only the protocol allowlist projection.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.brokers.kis import base as kis_base
from app.services.brokers.kis.base import BaseKISClient
from app.services.brokers.kis.send_outcome import OrderSendOutcomeTracker

_APP_KEY = "synthetic-app-key-should-not-leak"
_APP_SECRET = "synthetic-app-secret-should-not-leak"
_ACCESS_TOKEN = "synthetic-access-token-should-not-leak"
_ACCOUNT_NO = "12345678-01"
_THROTTLE_BODY = {
    "rt_cd": "1",
    "msg_cd": "EGW00201",
    "msg1": "초당 거래건수를 초과하였습니다.",
    "account_no": _ACCOUNT_NO,
    "access_token": _ACCESS_TOKEN,
    "appkey": _APP_KEY,
    "output": {"GT_UID": "kis-body-correlation-123"},
}
_ACCEPTED_BODY = {
    "rt_cd": "0",
    "msg1": "정상처리 되었습니다.",
    "output": {"ODNO": "0030808418", "ORD_TMD": "233959"},
}


def _throttle_response(*, status_code: int = 500) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={
            "server": "synthetic-edge",
            "via": "1.1 synthetic-proxy",
            "x-request-id": "request-id-123",
            "x-kis-correlation-id": "kis-header-correlation-456",
            "date": "Mon, 25 Aug 2026 00:00:00 GMT",
            "content-type": "application/json",
            "authorization": _ACCESS_TOKEN,
            "appkey": _APP_KEY,
            "appsecret": _APP_SECRET,
            "tr_id": "TTTC0802U",
            "x-private-debug": _ACCOUNT_NO,
        },
        json=_THROTTLE_BODY,
        request=httpx.Request(
            "POST",
            f"https://gateway.example:9443/order?appkey={_APP_KEY}&account={_ACCOUNT_NO}",
        ),
    )


def _make_parent(responses: list[tuple[object, dict[str, object]]]) -> MagicMock:
    """Synthetic KIS parent that records protocol evidence at its HTTP boundary."""
    parent = MagicMock()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {"appkey": _APP_KEY, "appsecret": _APP_SECRET}
    parent._kis_url = lambda path: (
        f"https://{_ACCESS_TOKEN}@gateway.example:9443{path}"
        f"?appkey={_APP_KEY}&account={_ACCOUNT_NO}"
    )
    parent._settings = SimpleNamespace(
        kis_account_no=_ACCOUNT_NO,
        kis_access_token=_ACCESS_TOKEN,
    )

    async def _request(method: str, url: str, **kwargs: object) -> dict[str, object]:
        response, body = responses.pop(0)
        tracker = kwargs["send_outcome"]
        assert isinstance(tracker, OrderSendOutcomeTracker)
        tracker.mark_dispatched()
        tracker.mark_http_response(getattr(response, "status_code", 200))
        tracker.record_response_protocol(
            response=response,
            endpoint_url=url,
            response_body=body,
        )
        return body

    parent._request_with_rate_limit = AsyncMock(side_effect=_request)
    return parent


def _event_from(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    events = [
        record.kis_throttle_protocol_evidence
        for record in caplog.records
        if hasattr(record, "kis_throttle_protocol_evidence")
    ]
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, dict)
    return event


def _assert_safe_protocol_event(
    event: dict[str, object], *, order_surface: str
) -> None:
    assert event["event"] == "kis_throttle_protocol_evidence"
    assert event["order_surface"] == order_surface
    assert event["provider_message_code"] == "EGW00201"

    protocol = event["protocol"]
    assert isinstance(protocol, dict)
    assert protocol["status_code"] == 500
    assert protocol["reason_phrase"] == "Internal Server Error"
    assert protocol["status_line"] == "HTTP/1.1 500 Internal Server Error"
    assert protocol["endpoint"] == "gateway.example:9443" + (
        "/uapi/domestic-stock/v1/trading/order-cash"
        if order_surface == "domestic"
        else "/uapi/overseas-stock/v1/trading/order"
    )
    assert protocol["response_headers"] == {
        "server": "synthetic-edge",
        "via": "1.1 synthetic-proxy",
        "x-request-id": "request-id-123",
        "x-kis-correlation-id": "kis-header-correlation-456",
        "date": "Mon, 25 Aug 2026 00:00:00 GMT",
        "content-type": "application/json",
    }
    assert protocol["correlation_ids"] == {
        "header:x-request-id": "request-id-123",
        "header:x-kis-correlation-id": "kis-header-correlation-456",
        "body:gt_uid": "kis-body-correlation-123",
    }

    serialized_event = repr(event)
    for secret in (_APP_KEY, _APP_SECRET, _ACCESS_TOKEN, _ACCOUNT_NO):
        assert secret not in serialized_event
    assert "authorization" not in serialized_event
    assert "appsecret" not in serialized_event
    assert "x-private-debug" not in serialized_event
    assert "?" not in str(protocol["endpoint"])


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.asyncio.sleep", _fake_sleep
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.overseas_orders.asyncio.sleep", _fake_sleep
    )


@pytest.mark.unit
@pytest.mark.parametrize("order_surface", ["domestic", "overseas"])
@pytest.mark.asyncio
async def test_throttle_rejection_emits_one_allowlisted_protocol_event(
    order_surface: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _make_parent([(_throttle_response(), _THROTTLE_BODY)])
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=False),
    )

    if order_surface == "domestic":
        from app.services.brokers.kis.domestic_orders import DomesticOrderClient

        submit = DomesticOrderClient(parent).order_korea_stock(
            "005930", "sell", 1, 70000
        )
    else:
        from app.services.brokers.kis.overseas_orders import OverseasOrderClient

        submit = OverseasOrderClient(parent).order_overseas_stock(
            "BAC", "NYSE", "sell", 1, 62.15
        )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="EGW00201"):
            await submit

    _assert_safe_protocol_event(_event_from(caplog), order_surface=order_surface)
    assert parent._request_with_rate_limit.await_count == 1


class _MalformedResponse:
    status_code = 400

    @property
    def headers(self) -> object:
        raise RuntimeError("malformed headers")

    @property
    def reason_phrase(self) -> str:
        raise RuntimeError("malformed reason")

    @property
    def http_version(self) -> str:
        raise RuntimeError("malformed version")


@pytest.mark.unit
@pytest.mark.parametrize("order_surface", ["domestic", "overseas"])
@pytest.mark.asyncio
async def test_malformed_protocol_observation_does_not_stop_repost(
    order_surface: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing throttle retry still completes when projection inputs break."""
    parent = _make_parent(
        [
            (_MalformedResponse(), _THROTTLE_BODY),
            (_MalformedResponse(), _ACCEPTED_BODY),
        ]
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=False),
    )

    if order_surface == "domestic":
        from app.services.brokers.kis.domestic_orders import DomesticOrderClient

        result = await DomesticOrderClient(parent).order_korea_stock(
            "005930", "sell", 1, 70000
        )
    else:
        from app.services.brokers.kis.overseas_orders import OverseasOrderClient

        result = await OverseasOrderClient(parent).order_overseas_stock(
            "BAC", "NYSE", "sell", 1, 62.15
        )

    assert result["odno"] == "0030808418"
    assert parent._request_with_rate_limit.await_count == 2


class _CaptureSettings:
    kis_app_key = _APP_KEY
    kis_app_secret = _APP_SECRET
    kis_access_token = _ACCESS_TOKEN
    api_rate_limit_retry_429_max = 0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0
    kis_mock_base_url = ""


class _CaptureClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set[str] = set()

    @property
    def _settings(self) -> _CaptureSettings:  # type: ignore[override]
        return _CaptureSettings()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_boundary_records_the_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the production base dispatch, not only the order-client test double."""
    client = _CaptureClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    breaker = MagicMock()
    breaker.before_request.return_value = 1
    response = _throttle_response()

    monkeypatch.setattr(client, "_get_limiter", AsyncMock(return_value=limiter))
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        client, "_execute_http_request", AsyncMock(return_value=response)
    )
    monkeypatch.setattr(kis_base, "get_kis_circuit_breaker", lambda: breaker)

    tracker = OrderSendOutcomeTracker()
    data = await client._request_with_rate_limit(
        "POST",
        f"https://gateway.example:9443/uapi/domestic-stock/v1/trading/order-cash?appkey={_APP_KEY}",
        headers={"authorization": f"Bearer {_ACCESS_TOKEN}", "appkey": _APP_KEY},
        json_body={"CANO": _ACCOUNT_NO},
        retry_request_errors=False,
        max_retries_override=0,
        api_name="order_korea_stock",
        send_outcome=tracker,
    )

    assert data["msg_cd"] == "EGW00201"
    assert tracker.protocol_evidence is not None
    assert tracker.protocol_evidence["endpoint"] == (
        "gateway.example:9443/uapi/domestic-stock/v1/trading/order-cash"
    )
    assert set(tracker.protocol_evidence["response_headers"]) == {
        "server",
        "via",
        "x-request-id",
        "x-kis-correlation-id",
        "date",
        "content-type",
    }
