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
                "ord_psbl_qty": "6",
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
                "ord_psbl_qty": "1",
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
    assert kr_holding.accountKind == "live"
    assert kr_holding.sourceOfTruth is True
    assert kr_holding.isTradeable is True
    assert kr_holding.manualOnly is False
    assert kr_holding.sellableQuantity == 6
    assert kr_holding.pendingSellQuantity == 4
    assert kr_holding.referenceQuantity == 0

    us_holding = next(h for h in result.holdings if h.symbol == "AAPL")
    assert us_holding.assetCategory == "us_stock"
    assert us_holding.valueNative == 220
    assert us_holding.valueKrw == 286_000
    assert us_holding.pnlKrw == 26_000
    assert us_holding.accountKind == "live"
    assert us_holding.sourceOfTruth is True
    assert us_holding.isTradeable is True
    assert us_holding.manualOnly is False
    assert us_holding.sellableQuantity == 1
    assert us_holding.pendingSellQuantity == 1
    assert us_holding.referenceQuantity == 0


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
    assert account.cashBalances.usd == pytest.approx(50.0)
    assert account.buyingPower.usd == pytest.approx(40.0)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_reader_overseas_margin_fallback_without_us_holdings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKISAccountUsdOnly:
        async def fetch_my_stocks(self, *, is_overseas: bool) -> list[dict[str, Any]]:
            return []

        async def inquire_integrated_margin(self) -> dict[str, Any]:
            return {
                "stck_cash_objt_amt": "100000",
                "stck_cash100_max_ord_psbl_amt": "50000",
                "usd_balance": "0",
                "usd_ord_psbl_amt": None,
            }

        async def fetch_my_overseas_stocks(
            self, *, exchange_code: str
        ) -> list[dict[str, Any]]:
            return []

        async def inquire_overseas_margin(self) -> list[dict[str, Any]]:
            return [
                {
                    "natn_name": "US",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": 49.25,
                    "frcr_ord_psbl_amt1": 49.25,
                }
            ]

    class _FakeKISClient:
        def __init__(self) -> None:
            self.account = _FakeKISAccountUsdOnly()

    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    assert account.cashBalances.usd == pytest.approx(49.25)
    assert account.buyingPower.usd == pytest.approx(49.25)
    assert result.warning is None


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
    assert h.valueKrw == pytest.approx(720_000.0)
    assert h.pnlKrw == pytest.approx(20_000.0)
    assert h.priceState == "live"
    assert h.accountKind == "manual"
    assert h.sourceOfTruth is False
    assert h.isTradeable is False
    assert h.manualOnly is True
    assert h.sellableQuantity == 0
    assert h.pendingSellQuantity == 0
    assert h.referenceQuantity == 10
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


# ---------------------------------------------------------------------------
# ROB-238: KISMockHomeReader tests
# ---------------------------------------------------------------------------


class _FakeKISMockAccount:
    async def fetch_my_stocks(
        self, *, is_mock: bool, is_overseas: bool
    ) -> list[dict[str, Any]]:
        assert is_mock is True
        assert is_overseas is False
        return [
            {
                "pdno": "005935",
                "prdt_name": "삼성전자우",
                "hldg_qty": "5",
                "pchs_avg_pric": "60000",
                "pchs_amt": "300000",
                "evlu_amt": "310000",
                "evlu_pfls_amt": "10000",
                "evlu_pfls_rt": "3.3333",
            }
        ]

    async def inquire_domestic_cash_balance(
        self, is_mock: bool = False
    ) -> dict[str, Any]:
        assert is_mock is True
        return {
            "dnca_tot_amt": 200_000.0,
            "stck_cash_ord_psbl_amt": 180_000.0,
            "raw": {},
        }


class _FakeKISMockClient:
    def __init__(self) -> None:
        self.account = _FakeKISMockAccount()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_passes_is_mock_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readers, "SafeKISMockClient", _FakeKISMockClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.source == "kis_mock"
    assert account.accountKind == "paper"
    assert account.includedInHome is False
    assert account.accountId == "kis_mock_account"
    assert account.valueKrw == 310_000
    assert account.cashBalances.krw == pytest.approx(200_000.0)
    assert account.buyingPower.krw == pytest.approx(180_000.0)
    # Cash must NOT be included in investment value
    assert account.valueKrw != account.cashBalances.krw

    h = result.holdings[0]
    assert h.source == "kis_mock"
    assert h.accountKind == "paper"
    assert h.symbol == "005935"
    assert h.assetCategory == "kr_stock"
    assert h.currency == "KRW"
    assert h.valueKrw == 310_000
    assert h.costBasis == 300_000
    assert h.pnlKrw == 10_000
    assert h.pnlRate == pytest.approx(3.3333 / 100.0)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_reports_zero_cost_basis_gain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero recorded cost basis should not hide an otherwise valued mock position."""

    class _ZeroCostAccount:
        async def fetch_my_stocks(
            self, *, is_mock: bool, is_overseas: bool
        ) -> list[dict[str, Any]]:
            assert is_mock is True
            assert is_overseas is False
            return [
                {
                    "pdno": "000000",
                    "prdt_name": "무상입고",
                    "hldg_qty": "1",
                    "pchs_avg_pric": "0",
                    "pchs_amt": "0",
                    "evlu_amt": "12345",
                    "evlu_pfls_amt": "12345",
                    "evlu_pfls_rt": "0",
                }
            ]

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            assert is_mock is True
            return {"dnca_tot_amt": 0, "stck_cash_ord_psbl_amt": 0, "raw": {}}

    class _ZeroCostClient:
        def __init__(self) -> None:
            self.account = _ZeroCostAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _ZeroCostClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.valueKrw == pytest.approx(12_345.0)
    assert account.costBasisKrw is None
    assert account.pnlKrw == pytest.approx(12_345.0)
    assert account.pnlRate is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_ignores_unparseable_cash_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad mock cash strings should not poison holdings or account valuation."""

    class _BadCashAccount:
        async def fetch_my_stocks(
            self, *, is_mock: bool, is_overseas: bool
        ) -> list[dict[str, Any]]:
            assert is_mock is True
            assert is_overseas is False
            return []

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            assert is_mock is True
            return {
                "dnca_tot_amt": "not-a-number",
                "stck_cash_ord_psbl_amt": "also-bad",
                "raw": {},
            }

    class _BadCashClient:
        def __init__(self) -> None:
            self.account = _BadCashAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _BadCashClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.valueKrw == 0
    assert account.costBasisKrw is None
    assert account.pnlKrw is None
    assert account.pnlRate is None
    assert account.cashBalances.krw is None
    assert account.buyingPower.krw is None
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_returns_warning_when_not_configured() -> None:
    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "kis_mock"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_partial_failure_returns_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenKISMockClient:
        def __init__(self) -> None:
            self.account = None

        async def fetch_my_stocks(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("mock API down")

    class _BrokenAccount:
        async def fetch_my_stocks(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("mock API down")

    class _BrokenClient:
        def __init__(self) -> None:
            self.account = _BrokenAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _BrokenClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "kis_mock"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_cash_balance_called_with_is_mock_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inquire_domestic_cash_balance must be called with is_mock=True."""
    cash_balance_calls: list[bool] = []

    class _TrackingAccount:
        async def fetch_my_stocks(
            self, *, is_mock: bool, is_overseas: bool
        ) -> list[dict[str, Any]]:
            return []

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            cash_balance_calls.append(is_mock)
            return {
                "dnca_tot_amt": 50_000.0,
                "stck_cash_ord_psbl_amt": 40_000.0,
                "raw": {},
            }

    class _TrackingClient:
        def __init__(self) -> None:
            self.account = _TrackingAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _TrackingClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert cash_balance_calls == [True], (
        "inquire_domestic_cash_balance was not called with is_mock=True"
    )
    account = result.accounts[0]
    assert account.cashBalances.krw == pytest.approx(50_000.0)
    assert account.buyingPower.krw == pytest.approx(40_000.0)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_cash_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cash balance fetch failure must not block holdings/account — produces a warning."""

    class _CashFailAccount:
        async def fetch_my_stocks(
            self, *, is_mock: bool, is_overseas: bool
        ) -> list[dict[str, Any]]:
            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "1",
                    "pchs_avg_pric": "70000",
                    "pchs_amt": "70000",
                    "evlu_amt": "72000",
                    "evlu_pfls_amt": "2000",
                    "evlu_pfls_rt": "2.857",
                }
            ]

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            raise RuntimeError("cash API unavailable")

    class _CashFailClient:
        def __init__(self) -> None:
            self.account = _CashFailAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _CashFailClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert len(result.accounts) == 1
    assert len(result.holdings) == 1
    account = result.accounts[0]
    assert account.cashBalances.krw is None
    assert account.buyingPower.krw is None
    # Warning emitted but result is not empty
    assert result.warning is not None
    assert result.warning.source == "kis_mock"


# ---------------------------------------------------------------------------
# ROB-238: AlpacaPaperHomeReader tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_maps_positions_and_converts_usd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot, Position

    fake_account = AccountSnapshot(
        id="alpaca-test",
        buying_power=Decimal("1000"),
        cash=Decimal("500"),
        portfolio_value=Decimal("5500"),
        status="ACTIVE",
    )
    fake_positions = [
        Position(
            asset_id="aapl-id",
            symbol="AAPL",
            qty=Decimal("2"),
            avg_entry_price=Decimal("100"),
            current_price=Decimal("110"),
            market_value=Decimal("220"),
            unrealized_pl=Decimal("20"),
            side="long",
        )
    ]

    class _FakeAlpacaSvc:
        async def get_account(self) -> AccountSnapshot:
            return fake_account

        async def list_positions(self) -> list[Position]:
            return fake_positions

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _FakeAlpacaSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.source == "alpaca_paper"
    assert account.accountKind == "paper"
    assert account.includedInHome is False
    assert account.accountId == "alpaca_paper_account"
    assert account.cashBalances.usd == pytest.approx(500.0)
    assert account.buyingPower.usd == pytest.approx(1000.0)
    assert account.valueKrw == pytest.approx(220 * 1_300.0)
    assert account.costBasisKrw == pytest.approx(200 * 1_300.0)
    assert account.pnlKrw == pytest.approx(20 * 1_300.0)
    assert account.pnlRate == pytest.approx(20 / 200)

    h = result.holdings[0]
    assert h.source == "alpaca_paper"
    assert h.accountKind == "paper"
    assert h.symbol == "AAPL"
    assert h.market == "US"
    assert h.currency == "USD"
    assert h.assetCategory == "us_stock"
    assert h.valueNative == pytest.approx(220.0)
    assert h.valueKrw == pytest.approx(220 * 1_300.0)
    assert h.pnlKrw == pytest.approx(20 * 1_300.0)
    assert h.pnlRate == pytest.approx(20 / 200)
    assert h.priceState == "live"
    assert result.warning is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_keeps_account_pnl_unknown_when_basis_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot, Position

    class _MissingBasisSvc:
        async def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                id="alpaca-missing-basis",
                buying_power=Decimal("100"),
                cash=Decimal("50"),
                portfolio_value=Decimal("100"),
                status="ACTIVE",
            )

        async def list_positions(self) -> list[Position]:
            return [
                Position(
                    asset_id="free-share",
                    symbol="FREE",
                    qty=Decimal("1"),
                    avg_entry_price=Decimal("0"),
                    current_price=Decimal("50"),
                    market_value=Decimal("50"),
                    unrealized_pl=Decimal("50"),
                    side="long",
                ),
                Position(
                    asset_id="missing-price",
                    symbol="MISS",
                    qty=Decimal("1"),
                    avg_entry_price=Decimal("10"),
                    current_price=None,
                    market_value=None,
                    unrealized_pl=None,
                    side="long",
                ),
            ]

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _MissingBasisSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    account = result.accounts[0]
    assert account.valueKrw == pytest.approx(50 * 1_300.0)
    assert account.costBasisKrw is None
    assert account.pnlKrw is None
    assert account.pnlRate is None

    free = next(h for h in result.holdings if h.symbol == "FREE")
    assert free.valueKrw == pytest.approx(50 * 1_300.0)
    assert free.costBasis is None
    assert free.priceState == "live"

    missing = next(h for h in result.holdings if h.symbol == "MISS")
    assert missing.valueKrw is None
    assert missing.pnlKrw is None
    assert missing.priceState == "missing"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_not_configured_returns_warning() -> None:
    from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError

    class _Reader(readers.AlpacaPaperHomeReader):
        @staticmethod
        def _make_service() -> Any:
            raise AlpacaPaperConfigurationError("no creds")

    result = await _Reader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "alpaca_paper"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_alpaca_paper_reader_mutation_methods_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that submit_order and cancel_order are never called during fetch."""
    from decimal import Decimal

    from app.services.brokers.alpaca.schemas import AccountSnapshot

    mutation_called: list[str] = []

    class _SafeFakeAlpacaSvc:
        async def get_account(self) -> AccountSnapshot:
            return AccountSnapshot(
                id="safe-test",
                buying_power=Decimal("0"),
                cash=Decimal("0"),
                portfolio_value=Decimal("0"),
                status="ACTIVE",
            )

        async def list_positions(self) -> list[Any]:
            return []

        async def submit_order(self, *args: Any, **kwargs: Any) -> Any:
            mutation_called.append("submit_order")
            raise AssertionError("submit_order must not be called from Invest Home")

        async def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
            mutation_called.append("cancel_order")
            raise AssertionError("cancel_order must not be called from Invest Home")

    monkeypatch.setattr(
        readers.AlpacaPaperHomeReader,
        "_make_service",
        staticmethod(lambda: _SafeFakeAlpacaSvc()),
    )

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    await readers.AlpacaPaperHomeReader().fetch(user_id=1)

    assert mutation_called == [], (
        "Mutation methods were called during Invest Home fetch"
    )
