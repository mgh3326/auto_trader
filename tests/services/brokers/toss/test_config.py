from __future__ import annotations

from pydantic import SecretStr

from app.core.config import Settings, validate_toss_api_config


class _Settings:
    toss_api_enabled = False
    toss_api_client_id = None
    toss_api_client_secret = None


def test_validate_toss_api_config_disabled_lists_gate_and_credentials() -> None:
    missing = validate_toss_api_config(_Settings())

    assert missing == [
        "TOSS_API_ENABLED",
        "TOSS_API_CLIENT_ID",
        "TOSS_API_CLIENT_SECRET",
    ]


def test_validate_toss_api_config_reports_names_only() -> None:
    class Configured:
        toss_api_enabled = True
        toss_api_client_id = "client-id-value"
        toss_api_client_secret = SecretStr("secret-value")

    assert validate_toss_api_config(Configured()) == []
    assert "secret-value" not in repr(Configured.toss_api_client_secret)


def test_toss_live_order_mutation_gate_defaults_false() -> None:
    configured = Settings(
        kis_app_key="kis-key",
        kis_app_secret="kis-secret",
        opendart_api_key="dart-key",
        upbit_access_key="upbit-key",
        upbit_secret_key="upbit-secret",
        SECRET_KEY="TestSecret123-" + "x" * 32,
    )

    assert configured.toss_live_order_mutations_enabled is False


def test_toss_fill_and_auto_reconcile_gates_default_false() -> None:
    configured = Settings(
        kis_app_key="kis-key",
        kis_app_secret="kis-secret",
        opendart_api_key="dart-key",
        upbit_access_key="upbit-key",
        upbit_secret_key="upbit-secret",
        SECRET_KEY="TestSecret123-" + "x" * 32,
    )

    assert configured.toss_fill_notify_enabled is False
    assert configured.toss_live_auto_reconcile_enabled is False
    assert configured.toss_live_auto_reconcile_safety_review_passed is False
