from __future__ import annotations

from pydantic import SecretStr

from app.core.config import validate_toss_api_config


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
