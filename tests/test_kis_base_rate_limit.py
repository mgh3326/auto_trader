from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.config import DEFAULT_KIS_API_RATE_LIMITS
from app.services.brokers.kis.base import BaseKISClient


class FakeSettings:
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 3
    api_rate_limit_retry_429_base_delay = 0.1


class _FakeSettingsClient(BaseKISClient):
    """Minimal subclass that overrides _settings without real deps."""

    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return FakeSettings()


def _make_client() -> _FakeSettingsClient:
    return _FakeSettingsClient()


def test_vtts3007_rate_limit_is_mapped(caplog):
    assert DEFAULT_KIS_API_RATE_LIMITS[
        "VTTS3007R|/uapi/overseas-stock/v1/trading/inquire-psamount"
    ] == {"rate": 10, "period": 1.0}
    client = _make_client()
    FakeSettings.kis_api_rate_limits = DEFAULT_KIS_API_RATE_LIMITS
    try:
        with caplog.at_level(logging.WARNING):
            assert client._get_rate_limit_for_api(
                "VTTS3007R|/uapi/overseas-stock/v1/trading/inquire-psamount"
            ) == (10, 1.0)
        assert "VTTS3007R" not in caplog.text
    finally:
        del FakeSettings.kis_api_rate_limits


class TestCalculateRetryDelay:
    def test_uses_retry_after_when_positive(self):
        client = _make_client()
        delay = client._calculate_retry_delay(attempt=0, retry_after=2.5)
        assert delay == pytest.approx(2.5)

    def test_exponential_backoff_when_no_retry_after(self):
        client = _make_client()
        delay0 = client._calculate_retry_delay(attempt=0, retry_after=0)
        delay1 = client._calculate_retry_delay(attempt=1, retry_after=0)
        # base_delay=0.1, attempt 0 → ~0.1, attempt 1 → ~0.2 (+ jitter up to 0.1)
        assert 0.1 <= delay0 < 0.3
        assert 0.2 <= delay1 < 0.5


class TestParseKisResponse:
    def test_returns_data_on_success(self):
        client = _make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"rt_cd": "0", "output": []}

        data, is_rate_limited = client._parse_kis_response(response, api_name="test")
        assert data == {"rt_cd": "0", "output": []}
        assert is_rate_limited is False

    def test_detects_rate_limit_heuristic(self):
        client = _make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "RATE_LIMIT",
            "msg1": "요청제한 초과",
        }

        data, is_rate_limited = client._parse_kis_response(response, api_name="test")
        assert is_rate_limited is True

    def test_raises_on_non_json(self):
        client = _make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        response.raise_for_status = MagicMock()

        with pytest.raises(RuntimeError, match="non-JSON"):
            client._parse_kis_response(response, api_name="test")

    def test_raises_on_non_dict_json(self):
        client = _make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = [1, 2, 3]

        with pytest.raises(RuntimeError, match="non-JSON"):
            client._parse_kis_response(response, api_name="test")

    def test_not_rate_limited_on_success(self):
        client = _make_client()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "NORMAL_ERROR",
            "msg1": "some error",
        }

        data, is_rate_limited = client._parse_kis_response(response, api_name="test")
        assert is_rate_limited is False


class _FastRetrySettings:
    kis_app_key = "key"
    kis_app_secret = "secret"
    kis_access_token = "token"
    api_rate_limit_retry_429_max = 1
    api_rate_limit_retry_429_base_delay = 0.0
    kis_rate_limit_rate = 19
    kis_rate_limit_period = 1.0


class _FastRetryClient(BaseKISClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._unmapped_rate_limit_keys_logged: set = set()
        type(self)._shared_client_lock = None

    @property  # type: ignore[override]
    def _settings(self):  # type: ignore[override]
        return _FastRetrySettings()


@pytest.mark.asyncio
async def test_request_error_retry_log_names_the_exception(monkeypatch, caplog):
    """ROB-600: a ReadTimeout('') retry must log 'ReadTimeout', not a blank reason.
    The exception itself re-raises (bare raise); the empty str() is handled at the
    call sites via describe_exception."""
    client = _FastRetryClient()
    limiter = MagicMock()
    limiter.acquire = AsyncMock()
    monkeypatch.setattr(client, "_get_limiter", AsyncMock(return_value=limiter))
    monkeypatch.setattr(client, "_ensure_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(
        client,
        "_execute_http_request",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.ReadTimeout):
            await client._request_with_rate_limit_with_headers(
                "GET",
                "https://host/path",
                headers={},
                retry_request_errors=True,
                api_name="inquire_domestic_cash_balance",
            )

    assert any("ReadTimeout" in r.getMessage() for r in caplog.records)


def _make_rate_limited_response():
    """A 200 response whose KIS body signals '초과' (EGW00215-style throttle)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {
        "rt_cd": "1",
        "msg_cd": "EGW00215",
        "msg1": "초당 거래건수를 초과하였습니다.",
    }
    return resp


class TestOrderPathNoDoubleSubmit:
    """ROB-645: order-submission callsites pass max_retries_override=0 so a timed-out
    or rate-limited order POST is sent exactly once (never re-POSTed)."""

    def _order_client(self, monkeypatch):
        client = _FastRetryClient()
        limiter = MagicMock()
        limiter.acquire = AsyncMock()
        monkeypatch.setattr(client, "_get_limiter", AsyncMock(return_value=limiter))
        monkeypatch.setattr(
            client, "_ensure_client", AsyncMock(return_value=MagicMock())
        )
        return client

    @pytest.mark.asyncio
    async def test_no_retry_on_request_error_when_max_retries_zero(self, monkeypatch):
        client = self._order_client(monkeypatch)
        execute = AsyncMock(side_effect=httpx.ReadTimeout(""))
        monkeypatch.setattr(client, "_execute_http_request", execute)

        with pytest.raises(httpx.ReadTimeout):
            await client._request_with_rate_limit_with_headers(
                "POST",
                "https://host/uapi/domestic-stock/v1/trading/order-cash",
                headers={},
                json_body={"PDNO": "005930"},
                retry_request_errors=False,
                max_retries_override=0,
                api_name="order_korea_stock",
            )

        # Exactly one POST — no re-submission of a timed-out order.
        assert execute.await_count == 1

    @pytest.mark.asyncio
    async def test_no_repost_on_rate_limit_heuristic_when_max_retries_zero(
        self, monkeypatch
    ):
        client = self._order_client(monkeypatch)
        execute = AsyncMock(return_value=_make_rate_limited_response())
        monkeypatch.setattr(client, "_execute_http_request", execute)

        data, _headers = await client._request_with_rate_limit_with_headers(
            "POST",
            "https://host/uapi/domestic-stock/v1/trading/order-cash",
            headers={},
            json_body={"PDNO": "005930"},
            retry_request_errors=False,
            max_retries_override=0,
            api_name="order_korea_stock",
        )

        # EGW00215 '초과' is surfaced as the error body, not retried (no re-POST).
        assert data["rt_cd"] == "1"
        assert data["msg_cd"] == "EGW00215"
        assert execute.await_count == 1

    @pytest.mark.asyncio
    async def test_read_path_still_retries_request_errors(self, monkeypatch):
        """Regression: default read path keeps retrying transient RequestErrors."""
        client = self._order_client(monkeypatch)
        ok = MagicMock()
        ok.status_code = 200
        ok.headers = {}
        ok.json.return_value = {"rt_cd": "0", "output": []}
        execute = AsyncMock(side_effect=[httpx.ReadTimeout(""), ok])
        monkeypatch.setattr(client, "_execute_http_request", execute)

        data, _headers = await client._request_with_rate_limit_with_headers(
            "GET",
            "https://host/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={},
            api_name="inquire_price",
        )

        assert data["rt_cd"] == "0"
        assert execute.await_count == 2  # retried once, then succeeded
