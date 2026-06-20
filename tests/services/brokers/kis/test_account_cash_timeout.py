from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.brokers.kis.account import AccountClient


class _Settings:
    kis_account_no = "12345678-01"
    kis_access_token = "tok"


def _make_account_client():
    parent = MagicMock()
    parent._settings = _Settings()
    parent._ensure_token = AsyncMock()
    parent._hdr_base = {}
    parent._kis_url = lambda path: f"https://host{path}"
    parent._request_with_rate_limit = AsyncMock(
        return_value={"rt_cd": "0", "output2": [{}]}
    )
    return AccountClient(parent), parent


@pytest.mark.asyncio
async def test_inquire_domestic_cash_balance_mock_uses_10s_timeout():
    """ROB-600: mock VTS is slow near the 5s boundary; mock read uses 10s."""
    client, parent = _make_account_client()
    await client.inquire_domestic_cash_balance(is_mock=True)
    assert parent._request_with_rate_limit.call_args.kwargs["timeout"] == 10


@pytest.mark.asyncio
async def test_inquire_domestic_cash_balance_live_keeps_5s_timeout():
    client, parent = _make_account_client()
    await client.inquire_domestic_cash_balance(is_mock=False)
    assert parent._request_with_rate_limit.call_args.kwargs["timeout"] == 5
