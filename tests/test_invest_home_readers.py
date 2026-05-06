"""ROB-123 — Invest home reader mapping tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.services import invest_home_readers as readers


class _FakeKISAccount:
    async def fetch_my_stocks(self, *, is_overseas: bool) -> list[dict[str, Any]]:
        assert is_overseas is False
        return [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "10",
                "pchs_avg_pric": "70000",
                "pchs_amt": "700000",
                "evlu_amt": "720000",
                "evlu_pfls_amt": "20000",
                "evlu_pfls_rt": "2.8571",
            }
        ]

    async def inquire_integrated_margin(self) -> dict[str, Any]:
        return {
            "stck_cash_objt_amt": "100000",
            "stck_cash100_max_ord_psbl_amt": "50000",
            "usd_balance": 7.0,
            "usd_ord_psbl_amt": 5.0,
        }

    async def fetch_my_overseas_stocks(
        self, *, exchange_code: str
    ) -> list[dict[str, Any]]:
        assert exchange_code == "NASD"
        return [
            {
                "ovrs_pdno": "AAPL",
                "ovrs_item_name": "Apple",
                "ovrs_cblc_qty": "2",
                "pchs_avg_pric": "100",
                "frcr_pchs_amt1": "200",
                "ovrs_stck_evlu_amt": "220",
                "frcr_evlu_pfls_amt": "20",
                "evlu_pfls_rt": "10",
            }
        ]


class _FakeKISClient:
    def __init__(self) -> None:
        self.account = _FakeKISAccount()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_reader_excludes_cash_from_value_and_converts_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    assert account.valueKrw == 720_000 + 220 * 1_300
    assert account.costBasisKrw == 700_000 + 200 * 1_300
    assert account.pnlKrw == (720_000 + 220 * 1_300) - (700_000 + 200 * 1_300)
    assert account.cashBalances.krw == 100_000
    assert account.buyingPower.krw == 50_000
    assert account.cashBalances.krw not in (account.valueKrw, account.costBasisKrw)

    kr_holding = next(h for h in result.holdings if h.symbol == "005930")
    assert kr_holding.assetCategory == "kr_stock"

    us_holding = next(h for h in result.holdings if h.symbol == "AAPL")
    assert us_holding.assetCategory == "us_stock"
    assert us_holding.valueNative == 220
    assert us_holding.valueKrw == 286_000
    assert us_holding.pnlKrw == 26_000


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_reader_overseas_margin_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKISAccountFallback:
        async def fetch_my_stocks(self, *, is_overseas: bool) -> list[dict[str, Any]]:
            return []

        async def inquire_integrated_margin(self) -> dict[str, Any]:
            # Returns 0/None for USD
            return {
                "stck_cash_objt_amt": "100000",
                "stck_cash100_max_ord_psbl_amt": "50000",
                "usd_balance": 0.0,
                "usd_ord_psbl_amt": None,
            }

        async def fetch_my_overseas_stocks(
            self, *, exchange_code: str
        ) -> list[dict[str, Any]]:
            return [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "1"}]

        async def inquire_overseas_margin(self) -> list[dict[str, Any]]:
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": 50.0,
                    "frcr_ord_psbl_amt1": 40.0,
                }
            ]

    class _FakeKISClient:
        def __init__(self) -> None:
            self.account = _FakeKISAccountFallback()

    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    # Fallback should have picked up 50.0 and 40.0
    assert account.cashBalances.usd == 50.0
    assert account.buyingPower.usd == 40.0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_uses_coin_value_not_krw_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "BTC",
                "balance": "0.1",
                "locked": "0",
                "avg_buy_price": "80000000",
            },
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 100_000_000.0}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers, "get_active_upbit_markets", AsyncMock(return_value={"KRW-BTC"})
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    assert account.valueKrw == 10_000_000
    assert account.costBasisKrw == 8_000_000
    assert account.pnlKrw == 2_000_000
    assert account.cashBalances.krw == 90_000
    assert account.buyingPower.krw == 90_000
    assert account.valueKrw != account.cashBalances.krw
    assert result.holdings[0].valueKrw == 10_000_000
    assert result.holdings[0].pnlKrw == 2_000_000
    assert result.holdings[0].assetCategory == "crypto"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_falls_back_per_market_and_skips_zero_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "BTC",
                "balance": "0.1",
                "locked": "0",
                "avg_buy_price": "80000000",
            },
            {
                "currency": "XYM",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "10",
            },
            {
                "currency": "PCI",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "1000",
            },
        ]

    calls: list[list[str]] = []

    async def _prices(markets: list[str]) -> dict[str, float]:
        calls.append(markets)
        if markets == ["KRW-BTC", "KRW-PCI"]:
            return {}
        if markets == ["KRW-BTC"]:
            return {"KRW-BTC": 100_000_000.0}
        return {}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)

    # Mock active/warning markets
    monkeypatch.setattr(
        readers,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC", "KRW-PCI"}),
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert [h.symbol for h in result.holdings] == ["BTC", "PCI"]
    assert calls == [["KRW-BTC", "KRW-PCI"], ["KRW-BTC"], ["KRW-PCI"]]
    assert result.holdings[0].valueKrw == 10_000_000
    assert result.accounts[0].valueKrw == 10_000_000
    assert result.accounts[0].costBasisKrw == 8_000_000
    assert result.accounts[0].pnlKrw == 2_000_000
    assert result.warning is not None
    assert result.hidden_counts.upbitInactive == 1  # XYM is inactive


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_filters_dust(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "BTC", "balance": "1", "locked": "0"},  # 100M
            {"currency": "DOGE", "balance": "1", "locked": "0"},  # 100 (dust)
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        return {"KRW-BTC": 100_000_000.0, "KRW-DOGE": 100.0}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers,
        "get_active_upbit_markets",
        AsyncMock(return_value={"KRW-BTC", "KRW-DOGE"}),
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert len(result.holdings) == 1
    assert result.holdings[0].symbol == "BTC"
    assert len(result.hidden_holdings) == 1
    assert result.hidden_holdings[0].symbol == "DOGE"
    assert result.hidden_counts.upbitDust == 1
    assert result.accounts[0].valueKrw == 100_000_000  # DOGE excluded


@pytest.mark.asyncio
@pytest.mark.unit
async def test_upbit_reader_does_not_show_loss_when_all_prices_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _coins() -> list[dict[str, Any]]:
        return [
            {"currency": "KRW", "balance": "90000", "locked": "0"},
            {
                "currency": "PCI",
                "balance": "1",
                "locked": "0",
                "avg_buy_price": "1000",
            },
        ]

    async def _prices(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-PCI"]
        return {}

    monkeypatch.setattr(readers, "fetch_my_coins", _coins)
    monkeypatch.setattr(readers, "fetch_multiple_current_prices", _prices)
    monkeypatch.setattr(
        readers, "get_active_upbit_markets", AsyncMock(return_value={"KRW-PCI"})
    )
    monkeypatch.setattr(
        readers, "get_upbit_warning_markets", AsyncMock(return_value=set())
    )

    result = await readers.UpbitHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.accounts[0].valueKrw == 0
    assert result.accounts[0].costBasisKrw is None
    assert result.accounts[0].pnlKrw is None
    assert result.accounts[0].pnlRate is None
    assert result.warning is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_valuates_with_quote_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SimpleNamespace(id=3, broker_type="toss", account_name="Toss 수동")
    holding = SimpleNamespace(
        id=11,
        broker_account_id=3,
        broker_account=broker,
        ticker="005930",
        market_type=MarketType.KR,
        display_name="삼성전자",
        quantity=10,
        avg_price=70_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    # Mock QuoteService
    quote_service = MagicMock()
    quote_service.fetch_kr_prices = AsyncMock(return_value={"005930": 72_000.0})
    quote_service.fetch_us_prices = AsyncMock(return_value={})

    result = await readers.ManualHomeReader(db=None, quote_service=quote_service).fetch(
        user_id=1
    )  # type: ignore[arg-type]

    h = result.holdings[0]
    assert h.symbol == "005930"
    assert h.valueKrw == 720_000.0
    assert h.pnlKrw == 20_000.0
    assert h.priceState == "live"
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_does_not_fabricate_value_from_cost_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = SimpleNamespace(id=3, broker_type="toss", account_name="Toss 수동")
    holding = SimpleNamespace(
        id=11,
        broker_account_id=3,
        broker_account=broker,
        ticker="005930",
        market_type=MarketType.KR,
        display_name="삼성전자",
        quantity=10,
        avg_price=70_000,
    )

    class _FakeManualService:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[Any]:
            assert user_id == 1
            return [holding]

    monkeypatch.setattr(readers, "ManualHoldingsService", _FakeManualService)

    result = await readers.ManualHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.accounts == []
    assert result.holdings[0].costBasis == 700_000
    assert result.holdings[0].valueKrw is None
    assert result.holdings[0].assetCategory == "kr_stock"
    assert result.holdings[0].priceState == "missing"
    assert result.warning is not None
