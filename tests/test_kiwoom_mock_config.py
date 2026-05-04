# tests/test_kiwoom_mock_config.py
"""Verify Kiwoom mock settings are off-by-default and fail-closed when partial."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.config import settings, validate_kiwoom_mock_config


def test_settings_have_kiwoom_mock_defaults():
    assert settings.kiwoom_mock_enabled is False
    assert settings.kiwoom_mock_app_key is None
    assert settings.kiwoom_mock_app_secret is None
    assert settings.kiwoom_mock_account_no is None
    assert settings.kiwoom_mock_base_url == "https://mockapi.kiwoom.com"
    assert settings.kiwoom_base_url == "https://api.kiwoom.com"


def test_validate_kiwoom_mock_config_lists_missing_when_disabled():
    obj = SimpleNamespace(
        kiwoom_mock_enabled=False,
        kiwoom_mock_app_key="x",
        kiwoom_mock_app_secret="x",
        kiwoom_mock_account_no="x",
    )
    missing = validate_kiwoom_mock_config(obj)
    assert "KIWOOM_MOCK_ENABLED" in missing


def test_validate_kiwoom_mock_config_lists_each_missing_field():
    obj = SimpleNamespace(
        kiwoom_mock_enabled=True,
        kiwoom_mock_app_key=None,
        kiwoom_mock_app_secret="   ",
        kiwoom_mock_account_no="",
    )
    missing = validate_kiwoom_mock_config(obj)
    assert "KIWOOM_MOCK_APP_KEY" in missing
    assert "KIWOOM_MOCK_APP_SECRET" in missing
    assert "KIWOOM_MOCK_ACCOUNT_NO" in missing


def test_validate_kiwoom_mock_config_returns_empty_when_complete():
    obj = SimpleNamespace(
        kiwoom_mock_enabled=True,
        kiwoom_mock_app_key="ak",
        kiwoom_mock_app_secret="sk",
        kiwoom_mock_account_no="123",
    )
    assert validate_kiwoom_mock_config(obj) == []
