"""Unit tests for paper portfolio handler."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.paper_portfolio_handler import (
    PaperAccountSelector,
    collect_paper_cash_balances,
    collect_paper_positions,
    is_paper_account_token,
    parse_paper_account_token,
    resolve_paper_position_name,
)


class TestIsPaperAccountToken:
    def test_exact_paper(self):
        assert is_paper_account_token("paper") is True

    def test_paper_with_name(self):
        assert is_paper_account_token("paper:데이트레이딩") is True

    def test_case_insensitive(self):
        assert is_paper_account_token("PAPER") is True
        assert is_paper_account_token("Paper:swing") is True

    def test_paper_with_whitespace(self):
        assert is_paper_account_token("  paper  ") is True

    def test_non_paper(self):
        assert is_paper_account_token("kis") is False
        assert is_paper_account_token("upbit") is False
        assert (
            is_paper_account_token("paperless") is False
        )  # prefix-only match forbidden
        assert is_paper_account_token(None) is False
        assert is_paper_account_token("") is False


class TestParsePaperAccountToken:
    def test_bare_paper_returns_all_selector(self):
        sel = parse_paper_account_token("paper")
        assert sel == PaperAccountSelector(account_name=None)

    def test_paper_with_name(self):
        sel = parse_paper_account_token("paper:데이트레이딩")
        assert sel == PaperAccountSelector(account_name="데이트레이딩")

    def test_trims_whitespace(self):
        sel = parse_paper_account_token("  paper :   swing  ")
        assert sel == PaperAccountSelector(account_name="swing")

    def test_empty_name_after_colon(self):
        sel = parse_paper_account_token("paper:")
        assert sel == PaperAccountSelector(account_name=None)

    def test_non_paper_raises(self):
        with pytest.raises(ValueError, match="not a paper account token"):
            parse_paper_account_token("kis")


class TestResolvePaperPositionName:
    @pytest.mark.asyncio
    async def test_equity_kr_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "삼성전자"})()

        async def fake_get(self, symbol):
            assert symbol == "005930"
            return fake_stock

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("005930", "equity_kr", db=AsyncMock())
        assert name == "삼성전자"

    @pytest.mark.asyncio
    async def test_equity_us_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "Apple Inc."})()

        async def fake_get(self, symbol):
            return fake_stock

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("AAPL", "equity_us", db=AsyncMock())
        assert name == "Apple Inc."

    @pytest.mark.asyncio
    async def test_stock_info_missing_falls_back_to_symbol(self, monkeypatch):
        async def fake_get(self, symbol):
            return None

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("NEWCO", "equity_us", db=AsyncMock())
        assert name == "NEWCO"

    @pytest.mark.asyncio
    async def test_crypto_uses_upbit_universe(self, monkeypatch):
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            AsyncMock(return_value="비트코인"),
        )
        name = await resolve_paper_position_name("KRW-BTC", "crypto", db=AsyncMock())
        assert name == "비트코인"

    @pytest.mark.asyncio
    async def test_crypto_lookup_failure_falls_back_to_symbol(self, monkeypatch):
        from app.services.upbit_symbol_universe_service import (
            UpbitSymbolNotRegisteredError,
        )

        async def boom(coin, quote_currency=None):
            raise UpbitSymbolNotRegisteredError("x")

        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            boom,
        )
        name = await resolve_paper_position_name("KRW-XYZ", "crypto", db=AsyncMock())
        assert name == "KRW-XYZ"


class _FakePaperAccount:
    def __init__(self, id_: int, name: str, is_active: bool = True):
        self.id = id_
        self.name = name
        self.is_active = is_active


class _FakePaperService:
    """Drop-in replacement for PaperTradingService in tests."""

    def __init__(
        self,
        *,
        accounts: list[_FakePaperAccount],
        positions_by_account: dict[int, list[dict]],
        cash_by_account: dict[int, dict[str, Decimal]] | None = None,
    ):
        self._accounts = accounts
        self._positions = positions_by_account
        self._cash = cash_by_account or {}

    async def list_accounts(self, is_active=True):
        if is_active is None:
            return list(self._accounts)
        return [a for a in self._accounts if a.is_active == is_active]

    async def get_account_by_name(self, name):
        for a in self._accounts:
            if a.name == name:
                return a
        return None

    async def get_positions(self, account_id, *, market=None):
        positions = list(self._positions.get(account_id, []))
        if market is not None:
            positions = [p for p in positions if p.get("instrument_type") == market]
        return positions

    async def get_cash_balance(self, account_id):
        return self._cash.get(account_id, {"krw": Decimal("0"), "usd": Decimal("0")})


@pytest.mark.asyncio
async def test_collect_paper_positions_all_active(monkeypatch):
    svc = _FakePaperService(
        accounts=[
            _FakePaperAccount(1, "default"),
            _FakePaperAccount(2, "데이트레이딩"),
        ],
        positions_by_account={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("10"),
                    "avg_price": Decimal("72000"),
                    "total_invested": Decimal("720000"),
                    "current_price": Decimal("73500"),
                    "evaluation_amount": Decimal("735000"),
                    "unrealized_pnl": Decimal("15000"),
                    "pnl_pct": Decimal("2.08"),
                }
            ],
            2: [],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="삼성전자"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name=None),
        market_filter=None,
    )

    assert errors == []
    assert len(positions) == 1
    p = positions[0]
    assert p["account"] == "paper:default"
    assert p["account_name"] == "default"
    assert p["broker"] == "paper"
    assert p["source"] == "paper"
    assert p["instrument_type"] == "equity_kr"
    assert p["market"] == "kr"
    assert p["symbol"] == "005930"
    assert p["name"] == "삼성전자"
    assert p["quantity"] == pytest.approx(10.0)
    assert p["avg_buy_price"] == pytest.approx(72000.0)
    assert p["current_price"] == pytest.approx(73500.0)
    assert p["evaluation_amount"] == pytest.approx(735000.0)
    assert p["profit_loss"] == pytest.approx(15000.0)
    assert p["profit_rate"] == pytest.approx(2.08)


@pytest.mark.asyncio
async def test_collect_paper_positions_named_account(monkeypatch):
    svc = _FakePaperService(
        accounts=[
            _FakePaperAccount(1, "default"),
            _FakePaperAccount(2, "데이트레이딩"),
        ],
        positions_by_account={
            1: [
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "quantity": Decimal("1"),
                    "avg_price": Decimal("100"),
                    "total_invested": Decimal("100"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                }
            ],
            2: [
                {
                    "symbol": "KRW-BTC",
                    "instrument_type": "crypto",
                    "quantity": Decimal("0.5"),
                    "avg_price": Decimal("50000000"),
                    "total_invested": Decimal("25000000"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="비트코인"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name="데이트레이딩"),
        market_filter=None,
    )

    assert errors == []
    assert len(positions) == 1
    assert positions[0]["account"] == "paper:데이트레이딩"
    assert positions[0]["symbol"] == "KRW-BTC"
    assert positions[0]["market"] == "crypto"


@pytest.mark.asyncio
async def test_collect_paper_positions_missing_account_returns_error(monkeypatch):
    svc = _FakePaperService(accounts=[], positions_by_account={})
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name="ghost"),
        market_filter=None,
    )

    assert positions == []
    assert len(errors) == 1
    assert errors[0]["source"] == "paper"
    assert "ghost" in errors[0]["error"]


@pytest.mark.asyncio
async def test_collect_paper_positions_applies_market_filter(monkeypatch):
    svc = _FakePaperService(
        accounts=[_FakePaperAccount(1, "default")],
        positions_by_account={
            1: [
                {
                    "symbol": "005930",
                    "instrument_type": "equity_kr",
                    "quantity": Decimal("1"),
                    "avg_price": Decimal("70000"),
                    "total_invested": Decimal("70000"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                },
                {
                    "symbol": "AAPL",
                    "instrument_type": "equity_us",
                    "quantity": Decimal("1"),
                    "avg_price": Decimal("100"),
                    "total_invested": Decimal("100"),
                    "current_price": None,
                    "evaluation_amount": None,
                    "unrealized_pnl": None,
                    "pnl_pct": None,
                },
            ],
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler.resolve_paper_position_name",
        AsyncMock(return_value="name"),
    )

    positions, errors = await collect_paper_positions(
        selector=PaperAccountSelector(account_name=None),
        market_filter="equity_us",
    )

    assert errors == []
    assert [p["symbol"] for p in positions] == ["AAPL"]


@pytest.mark.asyncio
async def test_collect_paper_cash_balances_all_accounts(monkeypatch):
    svc = _FakePaperService(
        accounts=[_FakePaperAccount(1, "default"), _FakePaperAccount(2, "day")],
        positions_by_account={},
        cash_by_account={
            1: {"krw": Decimal("10000000"), "usd": Decimal("500")},
            2: {"krw": Decimal("5000000"), "usd": Decimal("0")},
        },
    )
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    rows, errors = await collect_paper_cash_balances(
        selector=PaperAccountSelector(account_name=None),
    )

    assert errors == []
    # 2 accounts × 2 currencies, but USD=0 rows are still emitted for symmetry
    assert len(rows) == 4
    d_krw = next(
        r for r in rows if r["account"] == "paper:default" and r["currency"] == "KRW"
    )
    assert d_krw["balance"] == pytest.approx(10_000_000.0)
    assert d_krw["orderable"] == pytest.approx(10_000_000.0)
    assert d_krw["broker"] == "paper"
    assert d_krw["formatted"] == "10,000,000 KRW"
    d_usd = next(
        r for r in rows if r["account"] == "paper:default" and r["currency"] == "USD"
    )
    assert d_usd["balance"] == pytest.approx(500.0)
    assert d_usd["exchange_rate"] is None
    assert d_usd["formatted"] == "$500.00 USD"


@pytest.mark.asyncio
async def test_collect_paper_cash_balances_missing_named_account(monkeypatch):
    svc = _FakePaperService(accounts=[], positions_by_account={})
    monkeypatch.setattr(
        "app.mcp_server.tooling.paper_portfolio_handler._build_service",
        lambda db: svc,
    )

    rows, errors = await collect_paper_cash_balances(
        selector=PaperAccountSelector(account_name="ghost"),
    )
    assert rows == []
    assert errors and "ghost" in errors[0]["error"]
