"""Regression tests for _KISSettingsView credential isolation.

Pins that when is_mock=True the five hot credential fields return mock values
and do NOT leak live values, and vice versa when is_mock=False.

ROB-19 phase-2 carry: tightens _KISSettingsView against future leaky
__getattr__ regressions for the four sensitive live/mock credential fields.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.brokers.kis.client import _KISSettingsView


class TestMockViewDoesNotLeakLiveCredentials:
    """When is_mock=True, all five hot fields must return mock values."""

    def test_kis_app_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_app_key", "LIVE-KEY")
        monkeypatch.setattr(settings, "kis_mock_app_key", "MOCK-KEY")
        view = _KISSettingsView(is_mock=True)
        assert view.kis_app_key == "MOCK-KEY"
        assert view.kis_app_key != "LIVE-KEY"

    def test_kis_app_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_app_secret", "LIVE-SECRET")
        monkeypatch.setattr(settings, "kis_mock_app_secret", "MOCK-SECRET")
        view = _KISSettingsView(is_mock=True)
        assert view.kis_app_secret == "MOCK-SECRET"
        assert view.kis_app_secret != "LIVE-SECRET"

    def test_kis_account_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_account_no", "11111111-01")
        monkeypatch.setattr(settings, "kis_mock_account_no", "99999999-01")
        view = _KISSettingsView(is_mock=True)
        assert view.kis_account_no == "99999999-01"
        assert view.kis_account_no != "11111111-01"

    def test_kis_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_base_url", "https://live.example.com")
        monkeypatch.setattr(settings, "kis_mock_base_url", "https://mock.example.com")
        view = _KISSettingsView(is_mock=True)
        assert view.kis_base_url == "https://mock.example.com"
        assert view.kis_base_url != "https://live.example.com"

    def test_kis_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_access_token", "LIVE-TOKEN")
        monkeypatch.setattr(settings, "kis_mock_access_token", "MOCK-TOKEN")
        view = _KISSettingsView(is_mock=True)
        assert view.kis_access_token == "MOCK-TOKEN"
        assert view.kis_access_token != "LIVE-TOKEN"


class TestLiveViewDoesNotLeakMockCredentials:
    """When is_mock=False, all five hot fields must return live values."""

    def test_kis_app_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_app_key", "LIVE-KEY")
        monkeypatch.setattr(settings, "kis_mock_app_key", "MOCK-KEY")
        view = _KISSettingsView(is_mock=False)
        assert view.kis_app_key == "LIVE-KEY"
        assert view.kis_app_key != "MOCK-KEY"

    def test_kis_app_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_app_secret", "LIVE-SECRET")
        monkeypatch.setattr(settings, "kis_mock_app_secret", "MOCK-SECRET")
        view = _KISSettingsView(is_mock=False)
        assert view.kis_app_secret == "LIVE-SECRET"
        assert view.kis_app_secret != "MOCK-SECRET"

    def test_kis_account_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_account_no", "11111111-01")
        monkeypatch.setattr(settings, "kis_mock_account_no", "99999999-01")
        view = _KISSettingsView(is_mock=False)
        assert view.kis_account_no == "11111111-01"
        assert view.kis_account_no != "99999999-01"

    def test_kis_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_base_url", "https://live.example.com")
        monkeypatch.setattr(settings, "kis_mock_base_url", "https://mock.example.com")
        view = _KISSettingsView(is_mock=False)
        assert view.kis_base_url == "https://live.example.com"
        assert view.kis_base_url != "https://mock.example.com"

    def test_kis_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "kis_access_token", "LIVE-TOKEN")
        monkeypatch.setattr(settings, "kis_mock_access_token", "MOCK-TOKEN")
        view = _KISSettingsView(is_mock=False)
        assert view.kis_access_token == "LIVE-TOKEN"
        assert view.kis_access_token != "MOCK-TOKEN"
