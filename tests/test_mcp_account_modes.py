from __future__ import annotations

import pytest


def test_default_account_mode_is_kis_live():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode()

    assert routing.account_mode == "kis_live"
    assert routing.is_kis_live is True
    assert routing.warnings == []


def test_account_type_paper_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_type="paper")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True
    assert routing.warnings


def test_account_mode_simulated_is_db_simulated_alias():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode="simulated")

    assert routing.account_mode == "db_simulated"
    assert routing.is_db_simulated is True
    assert routing.deprecated_alias_used is True


def test_account_mode_kis_mock_is_official_kis_mock():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    routing = normalize_account_mode(account_mode="kis_mock")

    assert routing.account_mode == "kis_mock"
    assert routing.is_kis_mock is True
    assert routing.is_db_simulated is False


def test_conflicting_account_selectors_fail():
    from app.mcp_server.tooling.account_modes import normalize_account_mode

    with pytest.raises(ValueError, match="conflicting account selectors"):
        normalize_account_mode(account_mode="kis_mock", account_type="paper")


def test_validate_kis_mock_config_reports_names_only():
    from app.core.config import validate_kis_mock_config

    class DummySettings:
        kis_mock_enabled = False
        kis_mock_app_key = None
        kis_mock_app_secret = "secret-value"
        kis_mock_account_no = ""

    missing = validate_kis_mock_config(DummySettings())

    assert missing == [
        "KIS_MOCK_ENABLED",
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_ACCOUNT_NO",
    ]
    assert "secret-value" not in repr(missing)
