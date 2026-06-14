from __future__ import annotations

from pydantic import SecretStr

from app.services.brokers.toss.auth import TossOAuthTokenManager


def test_token_manager_repr_does_not_leak_secret_or_raw_client_id() -> None:
    manager = TossOAuthTokenManager(
        client_id="ROB530_CLIENT_ID_SHOULD_NOT_LEAK",
        client_secret=SecretStr("ROB530_CLIENT_SECRET_SHOULD_NOT_LEAK"),
        base_url="https://openapi.tossinvest.com",
    )

    rep = repr(manager)

    assert "ROB530_CLIENT_SECRET_SHOULD_NOT_LEAK" not in rep
    assert "ROB530_CLIENT_ID_SHOULD_NOT_LEAK" not in rep
    assert "client_id_fp" in rep
