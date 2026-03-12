import ssl

import pytest

from app.services.upbit_market_websocket import UpbitPublicWebSocketClient

pytestmark = pytest.mark.unit


def test_public_websocket_ssl_context_verifies_by_default() -> None:
    client = UpbitPublicWebSocketClient()

    ssl_context = client._create_ssl_context()

    assert ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert ssl_context.check_hostname is True


def test_public_websocket_rejects_explicit_insecure_mode() -> None:
    with pytest.raises(ValueError, match="verify_ssl=False is no longer supported"):
        UpbitPublicWebSocketClient(verify_ssl=False)
