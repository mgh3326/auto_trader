from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
