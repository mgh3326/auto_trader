"""Synthetic proof for throttle protocol evidence (ROB-s257 E-1b).

The test transport intentionally includes synthetic credentials in request URLs,
request settings, response headers, and response bodies.  The one structured
throttle event must retain only the protocol allowlist projection.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from io import StringIO
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


def _throttle_response(
    *,
    status_code: int = 500,
    body_kind: str = "json",
    extensions: dict[str, object] | None = None,
) -> httpx.Response:
    headers = {
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
    }
    if body_kind == "html":
        headers["content-type"] = "text/html"
    response_kwargs: dict[str, object] = {
        "headers": headers,
        "extensions": (
            extensions
            if extensions is not None
            else {
                "http_version": b"HTTP/1.1",
                "reason_phrase": b"Internal Server Error",
            }
        ),
        "request": httpx.Request(
            "POST",
            f"https://gateway.example:9443/order?appkey={_APP_KEY}&account={_ACCOUNT_NO}",
        ),
    }
    if body_kind == "json":
        response_kwargs["json"] = _THROTTLE_BODY
    else:
        response_kwargs["content"] = b"<html>synthetic proxy error</html>"
    return httpx.Response(
        status_code,
        **response_kwargs,  # type: ignore[arg-type]
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
    assert protocol["reason_phrase_observed"] is True
    assert protocol["http_version"] == "HTTP/1.1"
    assert protocol["http_version_observed"] is True
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
    def extensions(self) -> object:
        raise RuntimeError("malformed extensions")


class _ExplodingResponse:
    def __getattr__(self, _: str) -> object:
        raise RuntimeError("malformed response")


class _ItemsRaise:
    def items(self) -> object:
        raise RuntimeError("malformed header items")


class _PartialItems:
    def items(self) -> object:
        def _iterate() -> object:
            yield "server", "partial-ok"
            raise RuntimeError("iterator stopped")

        return _iterate()


class _RequestAccessForbidden:
    status_code = 500
    extensions: dict[str, object] = {}
    headers = SimpleNamespace(items=lambda: [("server", "synthetic-edge")])

    @property
    def request(self) -> object:
        raise AssertionError("response.request must not be read")


@pytest.mark.unit
@pytest.mark.parametrize(
    "response_factory",
    [
        pytest.param(_ExplodingResponse, id="getattr-raises"),
        pytest.param(
            lambda: SimpleNamespace(
                status_code=500, extensions={}, headers=_ItemsRaise()
            ),
            id="header-items-raises",
        ),
        pytest.param(
            lambda: SimpleNamespace(
                status_code=500, extensions={}, headers=_PartialItems()
            ),
            id="header-items-partial",
        ),
        pytest.param(
            lambda: SimpleNamespace(
                status_code=500,
                extensions={},
                headers=SimpleNamespace(
                    items=lambda: [(b"server", "ignored"), ("via", 1)]
                ),
            ),
            id="non-string-headers",
        ),
        pytest.param(
            lambda: SimpleNamespace(status_code=True, extensions={}, headers={}),
            id="bool-status",
        ),
        pytest.param(
            lambda: SimpleNamespace(status_code=500.0, extensions={}, headers={}),
            id="float-status",
        ),
        pytest.param(lambda: None, id="none"),
        pytest.param(lambda: "not-a-response", id="string"),
        pytest.param(lambda: 42, id="integer"),
    ],
)
def test_malformed_protocol_observation_never_raises(
    response_factory: Callable[[], object],
) -> None:
    response = response_factory()
    tracker = OrderSendOutcomeTracker()
    tracker.record_response_protocol(
        response=response,
        endpoint_url="https://gateway.example/order",
        response_body=None,
    )
    assert tracker.protocol_evidence is not None


@pytest.mark.unit
def test_protocol_observation_does_not_access_response_request() -> None:
    tracker = OrderSendOutcomeTracker()
    tracker.record_response_protocol(
        response=_RequestAccessForbidden(),
        endpoint_url="https://gateway.example/order?access_token=synthetic",
        response_body=None,
    )
    assert tracker.protocol_evidence is not None
    assert tracker.protocol_evidence["endpoint"] == "gateway.example/order"


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


@pytest.mark.unit
@pytest.mark.parametrize("order_surface", ["domestic", "overseas"])
@pytest.mark.asyncio
async def test_surviving_throttle_has_a_safe_stdout_summary(
    order_surface: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttle followed by acceptance is observable without a Sentry event."""
    parent = _make_parent(
        [
            (_throttle_response(status_code=400), _THROTTLE_BODY),
            (_throttle_response(status_code=200), _ACCEPTED_BODY),
        ]
    )
    monkeypatch.setattr(
        "app.services.brokers.kis.domestic_orders.is_nxt_eligible",
        AsyncMock(return_value=False),
    )
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    evidence_logger = logging.getLogger("app.services.brokers.kis.send_outcome")
    evidence_logger.addHandler(handler)
    try:
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
    finally:
        evidence_logger.removeHandler(handler)

    assert result["odno"] == "0030808418"
    stdout_summary = stream.getvalue()
    assert (
        "kis_throttle_protocol_evidence status_code=400 "
        "status_line=HTTP/1.1 400 Internal Server Error "
        "server=synthetic-edge via=1.1 synthetic-proxy"
    ) in stdout_summary
    for secret in (_APP_KEY, _APP_SECRET, _ACCESS_TOKEN, _ACCOUNT_NO):
        assert secret not in stdout_summary
    assert "authorization" not in stdout_summary
    assert "x-private-debug" not in stdout_summary
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
@pytest.mark.parametrize(
    ("status_code", "body_kind"),
    [
        pytest.param(500, "html", id="500-html"),
        pytest.param(502, "html", id="502-html"),
        pytest.param(504, "json", id="504-json"),
        pytest.param(403, "json", id="403-json"),
        pytest.param(500, "json", id="500-json"),
    ],
)
@pytest.mark.asyncio
async def test_http_boundary_records_protocol_before_all_parser_error_shapes(
    status_code: int,
    body_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy/KIS error shapes retain evidence even when parsing raises first."""
    client = _CaptureClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    breaker = MagicMock()
    breaker.before_request.return_value = 1
    response = _throttle_response(status_code=status_code, body_kind=body_kind)

    monkeypatch.setattr(client, "_get_limiter", AsyncMock(return_value=limiter))
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        client, "_execute_http_request", AsyncMock(return_value=response)
    )
    monkeypatch.setattr(kis_base, "get_kis_circuit_breaker", lambda: breaker)

    tracker = OrderSendOutcomeTracker()
    request = client._request_with_rate_limit(
        "POST",
        f"https://gateway.example:9443/uapi/domestic-stock/v1/trading/order-cash?appkey={_APP_KEY}",
        headers={"authorization": f"Bearer {_ACCESS_TOKEN}", "appkey": _APP_KEY},
        json_body={"CANO": _ACCOUNT_NO},
        retry_request_errors=False,
        max_retries_override=0,
        api_name="order_korea_stock",
        send_outcome=tracker,
    )
    if status_code == 500 and body_kind == "json":
        data = await request
        assert data["msg_cd"] == "EGW00201"
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await request

    assert tracker.protocol_evidence is not None
    protocol = tracker.protocol_evidence
    assert protocol["status_code"] == status_code
    assert protocol["status_line"] == (f"HTTP/1.1 {status_code} Internal Server Error")
    assert protocol["endpoint"] == (
        "gateway.example:9443/uapi/domestic-stock/v1/trading/order-cash"
    )
    assert set(protocol["response_headers"]) == {
        "server",
        "via",
        "x-request-id",
        "x-kis-correlation-id",
        "date",
        "content-type",
    }
    if status_code == 500 and body_kind == "json":
        assert protocol["correlation_ids"]["body:gt_uid"] == "kis-body-correlation-123"
    else:
        assert "body:gt_uid" not in protocol["correlation_ids"]


@pytest.mark.unit
def test_httpx_synthesized_defaults_are_not_marked_as_observed() -> None:
    """httpx defaults must not masquerade as wire status-line evidence."""
    tracker = OrderSendOutcomeTracker()
    tracker.record_response_protocol(
        response=_throttle_response(extensions={}),
        endpoint_url="https://gateway.example:9443/order",
        response_body=None,
    )

    assert tracker.protocol_evidence is not None
    assert tracker.protocol_evidence["status_code"] == 500
    assert tracker.protocol_evidence["reason_phrase"] is None
    assert tracker.protocol_evidence["reason_phrase_observed"] is False
    assert tracker.protocol_evidence["http_version"] is None
    assert tracker.protocol_evidence["http_version_observed"] is False
    assert tracker.protocol_evidence["status_line"] is None


@pytest.mark.unit
def test_wire_extensions_and_list_correlation_ids_are_observed() -> None:
    tracker = OrderSendOutcomeTracker()
    tracker.record_response_protocol(
        response=_throttle_response(
            extensions={"http_version": b"HTTP/2", "reason_phrase": b""}
        ),
        endpoint_url="https://[::1]:9443/order?access_token=secret",
        response_body={"output": [{"GT_UID": "listed-correlation-789"}]},
    )

    assert tracker.protocol_evidence is not None
    assert tracker.protocol_evidence["http_version"] == "HTTP/2"
    assert tracker.protocol_evidence["http_version_observed"] is True
    assert tracker.protocol_evidence["reason_phrase"] is None
    assert tracker.protocol_evidence["reason_phrase_observed"] is False
    assert tracker.protocol_evidence["status_line"] == "HTTP/2 500"
    assert tracker.protocol_evidence["endpoint"] == "[::1]:9443/order"
    assert tracker.protocol_evidence["correlation_ids"]["body:gt_uid"] == (
        "listed-correlation-789"
    )


@pytest.mark.unit
def test_header_projection_is_exact_bounded_and_newline_safe() -> None:
    tracker = OrderSendOutcomeTracker()
    long_server = "server\r\n" + "x" * 600
    response = SimpleNamespace(
        status_code=500,
        extensions={},
        headers=SimpleNamespace(
            items=lambda: [
                ("server", long_server),
                ("x-request-id", "request\nline"),
                ("x-request-id-extra", _APP_KEY),
                ("authorization", _ACCESS_TOKEN),
            ]
        ),
    )
    tracker.record_response_protocol(
        response=response,
        endpoint_url="https://gateway.example/order",
        response_body={"request_id_extra": _APP_SECRET, "request_id": True},
    )

    assert tracker.protocol_evidence is not None
    protocol = tracker.protocol_evidence
    assert set(protocol["response_headers"]) == {"server", "x-request-id"}
    assert protocol["response_headers"]["server"].startswith("server\\r\\n")
    assert len(protocol["response_headers"]["server"]) == 512
    assert protocol["response_headers"]["x-request-id"] == "request\\nline"
    assert protocol["correlation_ids"] == {"header:x-request-id": "request\\nline"}
    serialized_protocol = repr(protocol)
    for secret in (_APP_KEY, _APP_SECRET, _ACCESS_TOKEN, _ACCOUNT_NO):
        assert secret not in serialized_protocol
