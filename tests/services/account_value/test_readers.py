from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.account_value.readers import BrokerReader


def _fake_kis_client(*, rows, complete, us_cash=None):
    class Account:
        async def fetch_my_stocks_with_completion(self, **kwargs):
            return {"rows": rows, "complete": complete}

        async def inquire_integrated_margin(self, **kwargs):
            return {"raw": {"stck_cash_objt_amt": "10"}}

        async def inquire_overseas_margin(self, **kwargs):
            return [us_cash] if us_cash is not None else []

    class Client:
        account = Account()

        async def close(self):
            pass

    return Client


async def _read_kis(monkeypatch, market, client):
    import app.services.invest_home_readers as home_readers

    monkeypatch.setattr(home_readers, "SafeKISClient", client)
    reader = BrokerReader(None, "kis", market)

    async def marks(*args):
        return {}

    monkeypatch.setattr(reader, "_marks", marks)
    return (await reader.read())[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_kis_reader_marks_incomplete_source_unknown(monkeypatch, market):
    rows = (
        [{"pdno": "005930", "hldg_qty": "2"}]
        if market == "kr"
        else [{"ovrs_pdno": "BRK/B", "ovrs_cblc_qty": "2"}]
    )
    client = _fake_kis_client(
        rows=rows,
        complete=False,
        us_cash={"crcy_cd": "USD", "frcr_dncl_amt1": 0},
    )
    observation = await _read_kis(monkeypatch, market, client)
    assert observation.holdings[0].symbol == ("005930" if market == "kr" else "BRK.B")
    assert observation.holdings[0].mark is None
    assert "cash_allocation_unknown" in observation.reasons
    assert "holdings_completeness_unknown" in observation.reasons
    if market == "us":
        assert observation.cash["USD"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_kis_reader_drops_completeness_reason_only_when_proven(
    monkeypatch, market
):
    rows = (
        [{"pdno": "005930", "hldg_qty": "2"}]
        if market == "kr"
        else [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2"}]
    )
    client = _fake_kis_client(
        rows=rows,
        complete=True,
        us_cash={
            "crcy_cd": "USD",
            "frcr_dncl_amt1": 0,
            "frcr_dncl_amt1_present": True,
        },
    )
    observation = await _read_kis(monkeypatch, market, client)
    assert "holdings_completeness_unknown" not in observation.reasons
    if market == "us":
        assert observation.cash["USD"] == Decimal(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_kis_reader_keeps_completeness_reason_on_invalid_row(monkeypatch, market):
    rows = (
        [{"pdno": "005930", "hldg_qty": "2"}, {"pdno": "", "hldg_qty": "3"}]
        if market == "kr"
        else [
            {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2"},
            {"ovrs_pdno": "MSFT", "ovrs_cblc_qty": "bad"},
        ]
    )
    client = _fake_kis_client(rows=rows, complete=True)
    observation = await _read_kis(monkeypatch, market, client)
    assert "invalid_broker_holding" in observation.reasons
    assert "holdings_completeness_unknown" in observation.reasons


@pytest.mark.asyncio
async def test_kis_reader_us_cash_present_zero_is_zero(monkeypatch):
    client = _fake_kis_client(
        rows=[],
        complete=True,
        us_cash={
            "crcy_cd": "USD",
            "frcr_dncl_amt1": 0,
            "frcr_dncl_amt1_present": True,
        },
    )
    observation = await _read_kis(monkeypatch, "us", client)
    assert observation.cash["USD"] == Decimal(0)
    assert "holdings_completeness_unknown" not in observation.reasons


@pytest.mark.asyncio
async def test_kis_reader_us_cash_missing_field_stays_unknown(monkeypatch):
    client = _fake_kis_client(
        rows=[],
        complete=True,
        us_cash={
            "crcy_cd": "USD",
            "frcr_dncl_amt1": 0,
            "frcr_dncl_amt1_present": False,
        },
    )
    observation = await _read_kis(monkeypatch, "us", client)
    assert observation.cash["USD"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("us_fields", "expected"),
    [
        ({"frcr_dncl_amt1": "5856.200000"}, Decimal("5856.200000")),
        ({"frcr_dncl_amt1": 0, "frcr_dncl_amt_2": "123"}, Decimal(0)),
        ({"frcr_dncl_amt1": "bad", "frcr_dncl_amt_2": "0"}, Decimal(0)),
        ({"frcr_dncl_amt1": "bad", "frcr_ord_psbl_amt1": "500"}, None),
        ({"frcr_dncl_amt1": True}, None),
        ({}, None),
    ],
)
async def test_kis_reader_uses_us_cash_row_from_margin_response(
    monkeypatch, us_fields, expected
):
    from unittest.mock import AsyncMock

    from app.services.brokers.kis.account import AccountClient

    parent = SimpleNamespace(
        _settings=SimpleNamespace(kis_access_token="fixture"),
        _hdr_base={},
        _ensure_token=AsyncMock(),
        _kis_url=lambda path: path,
        _request_with_rate_limit=AsyncMock(
            return_value={
                "rt_cd": "0",
                "output": [
                    {"natn_name": "영국", "crcy_cd": "USD", "frcr_dncl_amt1": "5856.2"},
                    {"natn_name": "미국", "crcy_cd": "USD", **us_fields},
                ],
            }
        ),
        close=AsyncMock(),
    )
    parent.account = AccountClient(parent)
    monkeypatch.setattr(
        parent.account, "_resolve_account_parts", lambda: ("12345678", "01")
    )
    monkeypatch.setattr(
        parent.account,
        "fetch_my_stocks_with_completion",
        AsyncMock(return_value={"rows": [], "complete": True}),
    )
    observation = await _read_kis(monkeypatch, "us", lambda: parent)
    assert observation.cash == {"USD": expected}


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
    assert [holding.symbol for holding in observation.holdings] == ["KRW-BTC"]
    assert observation.holdings[0].quantity == Decimal(3)
    assert observation.holdings[0].mark is None


def test_existing_broker_fingerprints_keep_their_bytes(monkeypatch):
    from app.core.config import settings
    from app.services.account_value.readers import (
        kis_live_identity,
        upbit_live_identity,
    )

    monkeypatch.setattr(settings, "kis_account_no", "12345678-01")
    monkeypatch.setattr(settings, "kis_app_key", "synthetic-kis-key")
    monkeypatch.setattr(settings, "kis_app_secret", "synthetic-secret")
    monkeypatch.setattr(settings, "upbit_access_key", "synthetic-upbit-key")
    monkeypatch.setattr(settings, "upbit_secret_key", "synthetic-secret")

    assert (
        kis_live_identity()
        == "kis:kis_live:59b49e7d572cd7970280bc1285d772dbd538eae770c9f05ab1b35c138726492c"
    )
    assert (
        upbit_live_identity()
        == "upbit:upbit:41f5d7293b237022756e7528477d910388d44299c1b0949ed3e196c486847248"
    )


@pytest.mark.asyncio
async def test_toss_fingerprint_uses_response_and_hides_account(monkeypatch):
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
    monkeypatch.setattr(settings, "SECRET_KEY", "synthetic-test-secret")
    monkeypatch.setattr(settings, "toss_api_account_seq", 123)
    reader = BrokerReader(None, "toss", "us")
    [observation] = await reader.read()
    assert (
        observation.account
        == "toss:toss_live:"
        + "f356d2f7d721f36e0a73826bd7b4ee838fb3922308ee7ca911461dc9493cf655"
    )
    assert "account-example" not in observation.account
    assert "synthetic-test-secret" not in repr(observation)
    assert observation.cash == {"USD": None}
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
async def test_toss_fingerprint_rejects_configured_account_absent_from_response(
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
    monkeypatch.setattr(settings, "toss_api_account_seq", 456)
    monkeypatch.setattr(settings, "toss_token_namespace_fp", "0123456789abcdef")

    [observation] = await BrokerReader(None, "toss", "us").read()

    assert observation.account.endswith(":identity_unknown")
    assert "account_identity_unknown" in observation.reasons
