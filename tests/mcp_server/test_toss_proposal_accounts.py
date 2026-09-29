"""Read-only broker account source for explicit Toss parking proposals."""

import pytest

from app.mcp_server.tooling.order_proposal_tools import toss_proposal_accounts
from app.services.brokers.toss.dto import TossAccount

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_returns_all_broker_sequences_without_selecting_one(monkeypatch):
    from app.core import config
    from app.services.brokers.toss import client as client_module

    class FakeClient:
        async def accounts(self):
            return [
                TossAccount("fake-a", 731, "STOCK"),
                TossAccount("fake-b", 732, "STOCK"),
            ]

        async def aclose(self):
            pass

    monkeypatch.setattr(config, "validate_toss_api_config", lambda: [])
    monkeypatch.setattr(
        client_module.TossReadClient,
        "from_settings",
        lambda: FakeClient(),
    )

    result = await toss_proposal_accounts()
    assert result == {
        "success": True,
        "account_mode": "toss_live",
        "accounts": [
            {"broker_account_id": "731", "account_type": "STOCK"},
            {"broker_account_id": "732", "account_type": "STOCK"},
        ],
    }


@pytest.mark.asyncio
async def test_unavailable_account_read_has_no_account_fallback(monkeypatch):
    from app.core import config
    from app.services.brokers.toss import client as client_module

    def forbidden():
        raise AssertionError("must not instantiate a client")

    monkeypatch.setattr(
        config, "validate_toss_api_config", lambda: ["TOSS_API_ENABLED"]
    )
    monkeypatch.setattr(client_module.TossReadClient, "from_settings", forbidden)
    assert await toss_proposal_accounts() == {
        "success": False,
        "error": "toss_account_read_unavailable",
    }
