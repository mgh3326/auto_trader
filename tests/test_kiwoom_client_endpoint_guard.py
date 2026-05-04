# tests/test_kiwoom_client_endpoint_guard.py
"""Guard tests: Kiwoom mock client must refuse live URL and incomplete config."""

from __future__ import annotations

import pytest

from app.services.brokers.kiwoom.client import (
    KiwoomMockClient,
    KiwoomConfigurationError,
    KiwoomEndpointError,
)


def test_constructor_rejects_live_base_url():
    with pytest.raises(KiwoomEndpointError, match="mockapi.kiwoom.com"):
        KiwoomMockClient(
            base_url="https://api.kiwoom.com",
            app_key="ak",
            app_secret="sk",
            account_no="123",
        )


def test_constructor_rejects_unrelated_base_url():
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockClient(
            base_url="https://example.com",
            app_key="ak",
            app_secret="sk",
            account_no="123",
        )


def test_from_app_settings_fails_closed_when_disabled(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", False)
    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockClient.from_app_settings()
    assert "KIWOOM_MOCK_ENABLED" in str(exc.value)


def test_from_app_settings_fails_closed_when_credentials_missing(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", None)
    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockClient.from_app_settings()
    msg = str(exc.value)
    assert "KIWOOM_MOCK_APP_KEY" in msg
    assert "KIWOOM_MOCK_APP_SECRET" in msg
    assert "KIWOOM_MOCK_ACCOUNT_NO" in msg
