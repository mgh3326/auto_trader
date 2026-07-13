from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings, validate_kiwoom_mock_us_config
from app.services.brokers.kiwoom.client import (
    KiwoomConfigurationError,
    KiwoomEndpointError,
)
from app.services.brokers.kiwoom.us_client import KiwoomMockUsClient


def test_settings_have_kiwoom_mock_us_defaults() -> None:
    fields = Settings.model_fields
    assert fields["kiwoom_mock_us_enabled"].default is False
    assert fields["kiwoom_mock_us_app_key"].default is None
    assert fields["kiwoom_mock_us_app_secret"].default is None
    assert fields["kiwoom_mock_us_account_no"].default is None


def test_validator_reports_only_us_env_names() -> None:
    obj = SimpleNamespace(
        kiwoom_mock_us_enabled=False,
        kiwoom_mock_us_app_key=None,
        kiwoom_mock_us_app_secret="",
        kiwoom_mock_us_account_no=" ",
        kiwoom_mock_app_key="KR-AK",
        kiwoom_mock_app_secret="KR-SK",
        kiwoom_mock_account_no="KR-ACCOUNT",
    )
    assert validate_kiwoom_mock_us_config(obj) == [
        "KIWOOM_MOCK_US_ENABLED",
        "KIWOOM_MOCK_US_APP_KEY",
        "KIWOOM_MOCK_US_APP_SECRET",
        "KIWOOM_MOCK_US_ACCOUNT_NO",
    ]


def test_us_factory_never_falls_back_to_kr_credentials(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", None)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_key", "KR-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_app_secret", "KR-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_account_no", "KR-ACCOUNT")

    with pytest.raises(KiwoomConfigurationError) as exc:
        KiwoomMockUsClient.from_app_settings()

    message = str(exc.value)
    assert "KIWOOM_MOCK_US_APP_KEY" in message
    assert "KR-AK" not in message
    assert "KR-SK" not in message
    assert "KR-ACCOUNT" not in message


def test_us_factory_builds_distinct_auth_instance(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")

    first = KiwoomMockUsClient.from_app_settings()
    second = KiwoomMockUsClient.from_app_settings()

    assert first is not second
    assert first._auth is not second._auth


def test_us_factory_rejects_live_host(monkeypatch) -> None:
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_enabled", True)
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_key", "US-AK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_app_secret", "US-SK")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_us_account_no", "US-ACCOUNT")
    monkeypatch.setattr(cfg.settings, "kiwoom_mock_base_url", "https://api.kiwoom.com")
    with pytest.raises(KiwoomEndpointError):
        KiwoomMockUsClient.from_app_settings()
