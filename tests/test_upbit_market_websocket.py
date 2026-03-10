import ssl

from app.services.upbit_market_websocket import UpbitPublicWebSocketClient


def test_public_websocket_ssl_context_verifies_by_default() -> None:
    client = UpbitPublicWebSocketClient()

    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_public_websocket_ssl_context_supports_explicit_insecure_mode() -> None:
    client = UpbitPublicWebSocketClient(verify_ssl=False)

    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_NONE
    assert ssl_context.check_hostname is False
