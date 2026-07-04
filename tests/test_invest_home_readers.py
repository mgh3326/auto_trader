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
async def test_kis_reader_emits_provider_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[str, str, dict[str, Any]]] = []

    class _Span:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

        def set_data(self, key: str, value: Any) -> None:
            self.data[key] = value

        def set_tag(self, key: str, value: Any) -> None:
            self.data[key] = value

    class _SpanContext:
        def __init__(self, op: str, name: str) -> None:
            self.op = op
            self.name = name
            self.span = _Span()

        def __enter__(self) -> _Span:
            started.append((self.op, self.name, self.span.data))
            return self.span

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(op, name)

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    names = [name for _, name, _ in started]
    assert "invest.home.kis.domestic_balance" in names
    assert "invest.home.kis.integrated_margin" in names
    assert "invest.home.kis.overseas_balance" in names
    assert "invest.home.kis.fx" in names


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
                    "frcr_gnrl_ord_psbl_amt": 40.0,
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
                    "frcr_gnrl_ord_psbl_amt": 49.25,
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
async def test_kis_reader_overseas_margin_uses_general_orderable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-374 (B2): USD buying power must come from ``frcr_gnrl_ord_psbl_amt``.

    The live regression had ``frcr_ord_psbl_amt1 == 0`` while the real orderable
    (``frcr_gnrl_ord_psbl_amt``) was $3,080.62 — the same value ``get_available_capital``
    / ``portfolio_cash`` report. Reading the field-1 value produced a false
    ``buying_power_usd=0`` and a bogus "buying_power < 5% NAV" risk flag.
    """

    class _FakeKISAccountGeneralOrderable:
        async def fetch_my_stocks(self, *, is_overseas: bool) -> list[dict[str, Any]]:
            return []

        async def inquire_integrated_margin(self) -> dict[str, Any]:
            # Integrated margin reports no USD orderable -> triggers overseas fallback.
            return {
                "stck_cash_objt_amt": "100000",
                "stck_cash100_max_ord_psbl_amt": "50000",
                "usd_balance": None,
                "usd_ord_psbl_amt": None,
            }

        async def fetch_my_overseas_stocks(
            self, *, exchange_code: str
        ) -> list[dict[str, Any]]:
            return [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "1"}]

        async def inquire_overseas_margin(self) -> list[dict[str, Any]]:
            # Real KIS rows carry BOTH fields; field-1 can be 0 even when the
            # general orderable is non-zero.
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": 3_080.0,
                    "frcr_ord_psbl_amt1": 0.0,
                    "frcr_gnrl_ord_psbl_amt": 3_080.62,
                }
            ]

    class _FakeKISClient:
        def __init__(self) -> None:
            self.account = _FakeKISAccountGeneralOrderable()

    monkeypatch.setattr(readers, "SafeKISClient", _FakeKISClient)

    async def _fx() -> float:
        return 1_300.0

    monkeypatch.setattr(readers, "get_usd_krw_rate", _fx)

    result = await readers.KISHomeReader(db=None).fetch(user_id=1)  # type: ignore[arg-type]

    account = result.accounts[0]
    # Orderable must be the general orderable ($3,080.62), not the zero field-1 value.
    assert account.buyingPower.usd == pytest.approx(3_080.62)
    assert account.cashBalances.usd == pytest.approx(3_080.0)
    # A non-zero orderable means no "USD unavailable" warning.
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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manual_reader_emits_load_quote_and_fx_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _BrokerAccount:
        broker_type = "toss"

    class _ManualHolding:
        id = 1
        broker_account_id = 10
        broker_account = _BrokerAccount()
        ticker = "005930"
        display_name = "삼성전자"
        market_type = MarketType.KR
        quantity = 2
        avg_price = 70_000

    class _ManualHoldingsService:
        def __init__(self, db: object) -> None:
            self.db = db

        async def get_holdings_by_user(self, user_id: int) -> list[_ManualHolding]:
            assert user_id == 1
            return [_ManualHolding()]

    class _QuoteService:
        async def fetch_kr_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == ["005930"]
            return {"005930": 72_000.0}

        async def fetch_us_prices(self, tickers: list[str]) -> dict[str, float | None]:
            assert tickers == []
            return {}

    monkeypatch.setattr(readers.sentry_sdk, "start_span", _start_span)
    monkeypatch.setattr(readers, "ManualHoldingsService", _ManualHoldingsService)

    result = await readers.ManualHomeReader(
        db=object(), quote_service=_QuoteService()
    ).fetch(user_id=1)  # type: ignore[arg-type]

    assert result.warning is None
    assert "invest.home.manual.load_holdings" in started
    assert "invest.home.manual.fetch_kr_prices" in started
    assert "invest.home.manual.fetch_us_prices" in started


# ---------------------------------------------------------------------------
# ROB-238: KISMockHomeReader tests
# ---------------------------------------------------------------------------


class _FakeKISMockAccount:
    async def fetch_domestic_balance_snapshot(
        self, *, is_mock: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        assert is_mock is True
        return {
            "holdings": [
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
            ],
            "cash": {
                "dnca_tot_amt": 200_000.0,
                "stck_cash_ord_psbl_amt": 180_000.0,
            },
            "page_count": 1,
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
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            assert is_mock is True
            return {
                "holdings": [
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
                ],
                "cash": {"dnca_tot_amt": 0, "stck_cash_ord_psbl_amt": 0},
                "page_count": 1,
            }

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
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            assert is_mock is True
            return {
                "holdings": [],
                "cash": {
                    "dnca_tot_amt": "not-a-number",
                    "stck_cash_ord_psbl_amt": "also-bad",
                },
                "page_count": 1,
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


# ---------------------------------------------------------------------------
# ROB-268: KISMockHomeReader uses balance snapshot (no duplicate VTS calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_uses_balance_snapshot_and_skips_inquire_cash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: KISMockHomeReader pulls cash from the holdings snapshot.

    The reader must not issue a second /inquire-balance via
    ``inquire_domestic_cash_balance`` when the snapshot already carries cash.
    """
    snapshot_calls: list[bool] = []
    legacy_calls: list[str] = []

    class _SnapshotAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            snapshot_calls.append(is_mock)
            return {
                "holdings": [
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
                ],
                "cash": {
                    "dnca_tot_amt": "200000",
                    "stck_cash_ord_psbl_amt": "180000",
                },
                "page_count": 1,
            }

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            legacy_calls.append("inquire_domestic_cash_balance")
            return {}

        async def fetch_my_stocks(self, **kwargs: Any) -> list[dict[str, Any]]:
            legacy_calls.append("fetch_my_stocks")
            return []

    class _SnapshotClient:
        def __init__(self) -> None:
            self.account = _SnapshotAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _SnapshotClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert snapshot_calls == [True], (
        "Expected one fetch_domestic_balance_snapshot(is_mock=True) call; "
        f"got {snapshot_calls}"
    )
    assert legacy_calls == [], (
        "Reader must not call legacy duplicate paths when snapshot is "
        f"available; got {legacy_calls}"
    )

    account = result.accounts[0]
    assert account.source == "kis_mock"
    assert account.valueKrw == 72_000
    assert account.cashBalances.krw == pytest.approx(200_000.0)
    assert account.buyingPower.krw == pytest.approx(180_000.0)

    holding = result.holdings[0]
    assert holding.symbol == "005930"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_snapshot_unparseable_cash_no_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: snapshot cash that is unparseable must degrade gracefully.

    The reader must surface cash=None without falling back to a duplicate
    /inquire-balance — the fallback path is exactly what we are eliminating.
    """
    extra_calls: list[str] = []

    class _BadCashSnapshotAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            return {
                "holdings": [],
                "cash": {
                    "dnca_tot_amt": "not-a-number",
                    "stck_cash_ord_psbl_amt": "also-bad",
                },
                "page_count": 1,
            }

        async def inquire_domestic_cash_balance(
            self, is_mock: bool = False
        ) -> dict[str, Any]:
            extra_calls.append("inquire_domestic_cash_balance")
            return {}

        async def fetch_my_stocks(self, **kwargs: Any) -> list[dict[str, Any]]:
            extra_calls.append("fetch_my_stocks")
            return []

    class _BadCashSnapshotClient:
        def __init__(self) -> None:
            self.account = _BadCashSnapshotAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _BadCashSnapshotClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert extra_calls == [], (
        f"Bad cash parsing must not trigger duplicate VTS calls; got {extra_calls}"
    )
    account = result.accounts[0]
    assert account.cashBalances.krw is None
    assert account.buyingPower.krw is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_snapshot_missing_cash_keys_does_not_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: snapshot with no output2 (empty ``cash`` dict) must not fail the reader.

    Distinct from the unparseable-cash case: this models KIS returning output1
    only, with no output2 array — e.g. KIS-side schema drift.
    """

    class _MissingCashAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            return {
                "holdings": [
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
                ],
                "cash": {},
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _MissingCashClient:
        def __init__(self) -> None:
            self.account = _MissingCashAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _MissingCashClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert len(result.accounts) == 1
    assert len(result.holdings) == 1
    account = result.accounts[0]
    assert account.cashBalances.krw is None
    assert account.buyingPower.krw is None


class _RecordingSpan:
    """Captures Sentry set_tag / set_data calls for assertion."""

    def __init__(self) -> None:
        self.tags: dict[str, Any] = {}
        self.data: dict[str, Any] = {}

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_emits_sentry_observability_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: tag the current Sentry span with snapshot observability fields.

    Deploy-time verification of the duplicate-call fix depends on these tags;
    they are part of the acceptance criteria (see Linear ROB-268).
    """

    class _SnapshotAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            return {
                "holdings": [],
                "cash": {
                    "dnca_tot_amt": "100000",
                    "stck_cash_ord_psbl_amt": "90000",
                },
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _SnapshotClient:
        def __init__(self) -> None:
            self.account = _SnapshotAccount()

    span = _RecordingSpan()
    monkeypatch.setattr(readers, "SafeKISMockClient", _SnapshotClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)
    monkeypatch.setattr(readers.sentry_sdk, "get_current_span", lambda: span)

    await readers.KISMockHomeReader().fetch(user_id=1)

    assert span.tags.get("kis_mock.used_cash_from_snapshot") is True
    assert span.tags.get("kis_mock.cash_fallback") is False
    assert span.tags.get("kis_mock.pagination_stop_reason") == "tr_cont_end"
    assert span.data.get("kis_mock.balance_page_count") == 1
    assert span.data.get("kis_mock.balance_call_count") == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_cash_fallback_tag_when_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: ``kis_mock.cash_fallback`` must be True when cash cannot be parsed."""

    class _BadCashSnapshotAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            return {
                "holdings": [],
                "cash": {
                    "dnca_tot_amt": "not-a-number",
                    "stck_cash_ord_psbl_amt": "also-bad",
                },
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _BadCashClient:
        def __init__(self) -> None:
            self.account = _BadCashSnapshotAccount()

    span = _RecordingSpan()
    monkeypatch.setattr(readers, "SafeKISMockClient", _BadCashClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)
    monkeypatch.setattr(readers.sentry_sdk, "get_current_span", lambda: span)

    await readers.KISMockHomeReader().fetch(user_id=1)

    assert span.tags.get("kis_mock.cash_fallback") is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_no_op_when_no_active_sentry_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: reader must not break when Sentry has no active span (disabled / tests)."""

    class _SnapshotAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            return {
                "holdings": [],
                "cash": {
                    "dnca_tot_amt": "100000",
                    "stck_cash_ord_psbl_amt": "90000",
                },
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _SnapshotClient:
        def __init__(self) -> None:
            self.account = _SnapshotAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _SnapshotClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)
    monkeypatch.setattr(readers.sentry_sdk, "get_current_span", lambda: None)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts and result.accounts[0].source == "kis_mock"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_snapshot_failure_returns_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-268: snapshot exception (e.g. VTS timeout) must yield warning,
    not propagate up and break live/manual/Upbit sources."""

    class _ExplodingAccount:
        async def fetch_domestic_balance_snapshot(
            self, *, is_mock: bool = False, **kwargs: Any
        ) -> dict[str, Any]:

            raise RuntimeError("VTS timeout")

    class _ExplodingClient:
        def __init__(self) -> None:
            self.account = _ExplodingAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _ExplodingClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
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


# ---------------------------------------------------------------------------
# ROB-270: KIS mock UI read path uses bounded single-attempt timeout policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_passes_bounded_single_attempt_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: KISMockHomeReader requests timeout=10, retry_request_errors=False,
    and a reduced max_pages cap from fetch_domestic_balance_snapshot."""
    captured: dict[str, Any] = {}

    class _CapturingAccount:
        async def fetch_domestic_balance_snapshot(
            self, **kwargs: Any
        ) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "holdings": [],
                "cash": {},
                "page_count": 1,
                "stop_reason": "tr_cont_end",
            }

    class _CapturingClient:
        def __init__(self) -> None:
            self.account = _CapturingAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _CapturingClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.warning is None or result.warning.source == "kis_mock"
    assert captured.get("is_mock") is True
    assert captured.get("timeout") == pytest.approx(10.0)
    assert captured.get("retry_request_errors") is False
    assert captured.get("max_pages") == 3


@pytest.mark.asyncio
@pytest.mark.unit
async def test_kis_mock_reader_degrades_when_wall_time_bound_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-270: When snapshot exceeds the wall-time bound, the reader returns
    a sanitized warning instead of propagating, so live/manual/Upbit sources
    can still render."""
    import asyncio as _asyncio

    class _SlowAccount:
        async def fetch_domestic_balance_snapshot(
            self, **kwargs: Any
        ) -> dict[str, Any]:
            await _asyncio.sleep(1.0)  # longer than the patched bound
            return {"holdings": [], "cash": {}, "page_count": 1}

    class _SlowClient:
        def __init__(self) -> None:
            self.account = _SlowAccount()

    monkeypatch.setattr(readers, "SafeKISMockClient", _SlowClient)
    monkeypatch.setattr(readers, "_kis_mock_configured", lambda: True)

    # Patch the wait_for timeout to a tiny value by patching asyncio.wait_for
    real_wait_for = _asyncio.wait_for

    async def _short_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.05)

    monkeypatch.setattr(readers.asyncio, "wait_for", _short_wait_for)

    result = await readers.KISMockHomeReader().fetch(user_id=1)

    assert result.accounts == []
    assert result.holdings == []
    assert result.warning is not None
    assert result.warning.source == "kis_mock"
    assert "시간" in result.warning.message or "초과" in result.warning.message


@pytest.mark.asyncio
async def test_toss_api_home_reader_maps_read_only_holdings_and_cash(monkeypatch):
    from decimal import Decimal

    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    # ROB-549: mutations disabled (default) -> reference-only.
    from app.core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", False, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert result.warning is None
    assert result.accounts[0].source == "toss_api"
    assert result.accounts[0].accountKind == "live"
    assert result.accounts[0].cashBalances.krw == 123456.0
    assert result.accounts[0].cashBalances.usd == 789.01
    assert result.accounts[0].buyingPower.krw is None
    assert result.accounts[0].buyingPower.usd is None
    holding = result.holdings[0]
    assert holding.source == "toss_api"
    assert holding.sourceOfTruth is True
    assert holding.isTradeable is False
    assert holding.manualOnly is False
    assert holding.sellableQuantity == 0.0
    assert holding.referenceQuantity == 1.5


@pytest.mark.asyncio
async def test_toss_api_home_reader_tradeable_when_mutations_enabled(monkeypatch):
    """ROB-549: with Toss live mutations armed, toss_api holdings become tradeable
    and surface the API-provided sellable_quantity instead of discarding it."""
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(_cfg, "toss_live_order_mutations_enabled", True, raising=False)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    holding = result.holdings[0]
    assert holding.isTradeable is True
    assert holding.sellableQuantity == 1.25
    assert holding.pendingSellQuantity == pytest.approx(0.25)  # qty 1.5 - sellable 1.25


@pytest.mark.asyncio
async def test_toss_api_home_reader_converts_us_holdings_to_krw(monkeypatch):
    from decimal import Decimal

    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import (
        TossPortfolioPosition,
        TossPortfolioSnapshot,
    )

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        return TossPortfolioSnapshot(
            positions=[
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type="equity_us",
                    market="us",
                    symbol="BRK.B",
                    name="Berkshire Hathaway B",
                    quantity=Decimal("1.5"),
                    avg_buy_price=Decimal("400"),
                    current_price=Decimal("430.12"),
                    evaluation_amount=Decimal("645.18"),
                    profit_loss=Decimal("45.18"),
                    profit_rate=Decimal("0.0753"),
                    sellable_quantity=Decimal("1.25"),
                )
            ],
            cash_krw=Decimal("123456"),
            cash_usd=Decimal("789.01"),
        )

    async def fake_fx() -> float:
        return 1300.0

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(readers, "get_usd_krw_rate", fake_fx)

    result = await readers.TossApiHomeReader().fetch(user_id=1)

    assert result.warning is None
    assert result.holdings[0].valueKrw == pytest.approx(645.18 * 1300.0)
    assert result.holdings[0].pnlKrw == pytest.approx(45.18 * 1300.0)
    assert result.accounts[0].valueKrw == pytest.approx(645.18 * 1300.0)
    assert result.accounts[0].costBasisKrw == pytest.approx(600.0 * 1300.0)
    assert result.accounts[0].pnlKrw == pytest.approx(45.18 * 1300.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutations,expected_need", [(False, False), (True, True)])
async def test_toss_api_home_reader_gates_sellable_fetch_on_mutations(
    monkeypatch, mutations, expected_need
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot

    captured: dict[str, bool] = {}

    async def fake_fetch_toss_snapshot(
        *, need_sellable: bool = True, sellable_cache=None
    ):
        captured["need_sellable"] = need_sellable
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    # ROB-685: mutations off (default) => reader discards sellable anyway => skip fetch.
    assert captured["need_sellable"] is expected_need


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("mutations", [False, True])
async def test_toss_api_home_reader_passes_sellable_cache_when_mutations_on(
    monkeypatch, mutations
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot
    from app.services.toss_sellable_cache import TossSellableCache

    captured: dict[str, object] = {}

    async def fake_fetch_toss_snapshot(*, need_sellable=True, sellable_cache=None):
        captured["need_sellable"] = need_sellable
        captured["sellable_cache"] = sellable_cache
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    if mutations:
        # mutations armed => cache is threaded so repeated loads reuse it.
        assert isinstance(captured["sellable_cache"], TossSellableCache)
        assert captured["need_sellable"] is True
    else:
        # mutations off => ROB-685 skip, no cache needed.
        assert captured["sellable_cache"] is None
        assert captured["need_sellable"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_toss_portfolio_snapshot_emits_phase_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decimal import Decimal

    from app.services import toss_portfolio_service as toss_service

    started: list[str] = []

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            return None

        def set_tag(self, key: str, value: Any) -> None:
            return None

    class _SpanContext:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> _Span:
            started.append(self.name)
            return _Span()

        def __exit__(self, *exc: object) -> bool:
            return False

    def _start_span(*, op: str, name: str, **kwargs: Any) -> _SpanContext:
        return _SpanContext(name)

    class _Client:
        async def holdings(self) -> SimpleNamespace:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        symbol="005930",
                        name="삼성전자",
                        market_country="KR",
                        quantity=Decimal("2"),
                        average_purchase_price=Decimal("70000"),
                        last_price=Decimal("72000"),
                        market_value={"amount": Decimal("144000")},
                        profit_loss={
                            "amount": Decimal("4000"),
                            "rate": Decimal("0.0285"),
                        },
                    )
                ]
            )

        async def sellable_quantity(self, *, symbol: str) -> SimpleNamespace:
            assert symbol == "005930"
            return SimpleNamespace(sellable_quantity=Decimal("1"))

        async def buying_power(self, *, currency: str) -> SimpleNamespace:
            return SimpleNamespace(currency=currency, cash_buying_power=Decimal("1000"))

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(toss_service.sentry_sdk, "start_span", _start_span)

    snapshot = await toss_service.fetch_toss_portfolio_snapshot(client=_Client())

    assert snapshot.positions[0].symbol == "005930"
    assert "invest.home.toss_api.holdings" in started
    assert "invest.home.toss_api.sellable_quantity" in started
    assert "invest.home.toss_api.buying_power" in started
