from __future__ import annotations

import httpx
import pytest

from app.services.brokers.toss.errors import TossHostBlocked
from app.services.toss_consumer.transport import (
    assert_toss_consumer_host,
    build_toss_consumer_client,
)

pytestmark = [pytest.mark.unit]


def test_assert_toss_consumer_host():
    # Valid host & scheme
    assert_toss_consumer_host("wts-info-api.tossinvest.com", scheme="https")

    # Invalid scheme
    with pytest.raises(TossHostBlocked, match="Scheme .* is not allowed"):
        assert_toss_consumer_host("wts-info-api.tossinvest.com", scheme="http")

    # Invalid host
    with pytest.raises(TossHostBlocked, match="Host .* is not in TOSS_CONSUMER_HOSTS"):
        assert_toss_consumer_host("openapi.tossinvest.com", scheme="https")

    with pytest.raises(TossHostBlocked, match="Host .* is not in TOSS_CONSUMER_HOSTS"):
        assert_toss_consumer_host("google.com", scheme="https")


@pytest.mark.asyncio
async def test_build_toss_consumer_client_redirect_block():
    client = build_toss_consumer_client()
    assert str(client.base_url).rstrip("/") == "https://wts-info-api.tossinvest.com"
    assert client.follow_redirects is False

    # Check hooks assert correct host on request
    req = httpx.Request("GET", "https://openapi.tossinvest.com/some/path")
    with pytest.raises(TossHostBlocked):
        for hook in client.event_hooks["request"]:
            await hook(req)

    # Check hook blocks redirects
    resp_redirect = httpx.Response(
        302, headers={"Location": "https://other.com"}, request=req
    )
    with pytest.raises(TossHostBlocked, match="Unexpected redirect"):
        for hook in client.event_hooks["response"]:
            await hook(resp_redirect)
