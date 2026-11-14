from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.account_value.readers import BrokerReader


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_kis_reader_marks_incomplete_source_unknown(monkeypatch, market):
    import app.services.invest_home_readers as home_readers

    class Account:
        async def fetch_my_stocks(self):
            return [{"pdno": "005930", "hldg_qty": "2"}]

        async def fetch_my_overseas_stocks(self):
            return [{"ovrs_pdno": "BRK/B", "ovrs_cblc_qty": "2"}]

        async def inquire_integrated_margin(self):
            return {"raw": {"stck_cash_objt_amt": "10"}}

        async def inquire_overseas_margin(self):
            return [{"crcy_cd": "USD", "frcr_dncl_amt1": 0}]

    class Client:
        account = Account()

        async def close(self):
            pass

    monkeypatch.setattr(home_readers, "SafeKISClient", Client)
    reader = BrokerReader(None, "kis", market)

    async def marks(*args):
        return {}

    monkeypatch.setattr(reader, "_marks", marks)
    [observation] = await reader.read()
    assert observation.holdings[0].symbol == ("005930" if market == "kr" else "BRK.B")
    assert observation.holdings[0].mark is None
    assert "cash_allocation_unknown" in observation.reasons
    assert "holdings_completeness_unknown" in observation.reasons
    if market == "us":
        assert observation.cash["USD"] is None


@pytest.mark.asyncio
async def test_upbit_reader_includes_locked_cash_and_missing_mark(monkeypatch):
    from app.services.brokers.upbit import client

    async def coins():
        return [
            {"currency": "KRW", "balance": "10", "locked": "5"},
            {"currency": "BTC", "balance": "2", "locked": "1"},
        ]

    monkeypatch.setattr(client, "fetch_my_coins", coins)
    reader = BrokerReader(None, "upbit", "crypto")

    async def marks(*args):
        return {}

    monkeypatch.setattr(reader, "_marks", marks)
    [observation] = await reader.read()
    assert observation.cash["KRW"] == Decimal(15)
    assert observation.holdings[0].quantity == Decimal(3)
    assert observation.holdings[0].mark is None


@pytest.mark.asyncio
async def test_toss_fingerprint_uses_response_and_hides_account(monkeypatch):
    from pydantic import SecretStr

    from app.core.config import settings
    from app.services.brokers.toss.client import TossReadClient

    class Client:
        async def accounts(self):
            return [SimpleNamespace(account_no="account-example", account_seq=123)]

        async def holdings(self):
            return SimpleNamespace(items=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(TossReadClient, "from_settings", lambda: Client())
    monkeypatch.setattr(settings, "toss_api_client_secret", SecretStr("test-key"))
    monkeypatch.setattr(settings, "toss_api_account_seq", 123)
    reader = BrokerReader(None, "toss", "us")
    [observation] = await reader.read()
    assert observation.account.startswith("toss:toss_live:")
    assert "account-example" not in observation.account
    assert "123" not in observation.account[:15]
    assert "account_identity_unknown" not in observation.reasons
    assert "cash_allocation_unknown" in observation.reasons


@pytest.mark.asyncio
async def test_complete_reader_emits_account_and_market(monkeypatch):
    from datetime import UTC, datetime

    from app.services.account_value import readers
    from app.services.account_value.snapshot import Observation

    at = datetime.now(UTC)
    today = readers.today_kst()
    account = "upbit:upbit:" + "a" * 64

    class FakeReader:
        def __init__(self, session, broker, market):
            pass

        async def read(self):
            return [Observation(account, "crypto", today, at, (), {"KRW": Decimal(10)})]

    monkeypatch.setattr(readers, "BrokerReader", FakeReader)
    rows = await readers.CompleteBrokerReader(None, "upbit").read()
    assert [row.market for row in rows] == ["crypto", "account"]
    assert rows[1].account == account
    assert rows[1].cash == {"KRW": Decimal(10)}


@pytest.mark.asyncio
async def test_toss_automatic_selection_does_not_claim_an_identity(monkeypatch):
    from app.core.config import settings
    from app.services.brokers.toss.client import TossReadClient

    class Client:
        async def accounts(self):
            return [SimpleNamespace(account_no="account-example", account_seq=123)]

        async def holdings(self):
            return SimpleNamespace(items=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(TossReadClient, "from_settings", lambda: Client())
    monkeypatch.setattr(settings, "toss_api_account_seq", None)
    [observation] = await BrokerReader(None, "toss", "us").read()
    assert observation.account.endswith(":identity_unknown")
    assert "account_identity_unknown" in observation.reasons


@pytest.mark.asyncio
async def test_toss_fingerprint_only_consumer_does_not_claim_account_identity(
    monkeypatch,
):
    from app.core.config import settings
    from app.services.brokers.toss.client import TossReadClient

    class Client:
        async def accounts(self):
            return [SimpleNamespace(account_no="account-example", account_seq=123)]

        async def holdings(self):
            return SimpleNamespace(items=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(TossReadClient, "from_settings", lambda: Client())
    monkeypatch.setattr(settings, "toss_api_client_secret", None)
    monkeypatch.setattr(settings, "toss_api_account_seq", 123)
    monkeypatch.setattr(settings, "toss_token_namespace_fp", "0123456789abcdef")

    [observation] = await BrokerReader(None, "toss", "us").read()

    assert observation.account.endswith(":identity_unknown")
    assert "account_identity_unknown" in observation.reasons
