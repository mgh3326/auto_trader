from __future__ import annotations

from pathlib import Path

TOSS_DIR = Path("app/services/brokers/toss")


def test_toss_client_has_no_order_mutation_methods() -> None:
    source = (TOSS_DIR / "client.py").read_text()

    forbidden = [
        "create_order",
        "modify_order",
        "cancel_order",
        "/api/v1/orders/{orderId}/modify",
        "/api/v1/orders/{orderId}/cancel",
        '"POST", "/api/v1/orders"',
        "'POST', '/api/v1/orders'",
    ]
    for needle in forbidden:
        assert needle not in source


def test_oauth_token_is_only_toss_post_in_client_package() -> None:
    sources = "\n".join(path.read_text() for path in TOSS_DIR.glob("*.py"))

    assert (
        'post("/oauth2/token"' in sources
        or 'post(\n                "/oauth2/token"' in sources
    )
    assert 'post("/api/v1/orders"' not in sources
    assert 'request("POST", "/api/v1/orders"' not in sources
