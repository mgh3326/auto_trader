"""Unit tests for paper trading models (PaperAccount / PaperPosition / PaperTrade)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.paper_trading import PaperAccount, PaperPosition, PaperTrade
from app.models.trading import InstrumentType


class TestPaperAccountModel:
    def test_create_minimal_account(self) -> None:
        account = PaperAccount(
            name="기본 모의계좌",
            initial_capital=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
        )
        assert account.name == "기본 모의계좌"
        assert account.initial_capital == Decimal("10000000")
        assert account.cash_krw == Decimal("10000000")

    def test_create_full_account(self) -> None:
        account = PaperAccount(
            name="데이트레이딩",
            initial_capital=Decimal("5000000"),
            cash_krw=Decimal("4500000"),
            cash_usd=Decimal("100.50"),
            description="단타 전략 전용",
            strategy_name="rsi_mean_reversion",
            is_active=True,
        )
        assert account.description == "단타 전략 전용"
        assert account.strategy_name == "rsi_mean_reversion"
        assert account.is_active is True

    def test_table_args(self) -> None:
        # schema dict is always the last element of __table_args__
        assert PaperAccount.__table_args__[-1] == {"schema": "paper"}
        assert PaperAccount.__tablename__ == "paper_accounts"


class TestPaperPositionModel:
    def test_create_position(self) -> None:
        position = PaperPosition(
            account_id=1,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            quantity=Decimal("0.00123456"),
            avg_price=Decimal("85000000"),
            total_invested=Decimal("104987.76"),
        )
        assert position.account_id == 1
        assert position.symbol == "KRW-BTC"
        assert position.instrument_type == InstrumentType.crypto
        assert position.quantity == Decimal("0.00123456")

    def test_table_args(self) -> None:
        assert PaperPosition.__table_args__[-1] == {"schema": "paper"}
        assert PaperPosition.__tablename__ == "paper_positions"


class TestPaperTradeModel:
    def test_create_buy_trade(self) -> None:
        trade = PaperTrade(
            account_id=1,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            side="buy",
            order_type="limit",
            quantity=Decimal("10"),
            price=Decimal("175.50"),
            total_amount=Decimal("1755.00"),
            fee=Decimal("0.88"),
            currency="USD",
            reason="momentum entry",
        )
        assert trade.side == "buy"
        assert trade.order_type == "limit"
        assert trade.currency == "USD"
        assert trade.realized_pnl is None

    def test_create_sell_trade_with_pnl(self) -> None:
        trade = PaperTrade(
            account_id=1,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            side="sell",
            order_type="market",
            quantity=Decimal("10"),
            price=Decimal("200.00"),
            total_amount=Decimal("2000.00"),
            fee=Decimal("1.00"),
            currency="USD",
            realized_pnl=Decimal("244.12"),
            executed_at=datetime.now(UTC),
        )
        assert trade.realized_pnl == Decimal("244.12")

    def test_table_args(self) -> None:
        assert PaperTrade.__table_args__[-1] == {"schema": "paper"}
        assert PaperTrade.__tablename__ == "paper_trades"


def test_paper_daily_snapshot_constructor() -> None:
    from datetime import date
    from decimal import Decimal

    from app.models.paper_trading import PaperDailySnapshot

    snap = PaperDailySnapshot(
        account_id=1,
        snapshot_date=date(2026, 4, 13),
        cash_krw=Decimal("1000000"),
        cash_usd=Decimal("0"),
        positions_value=Decimal("500000"),
        total_equity=Decimal("1500000"),
        daily_return_pct=Decimal("0.25"),
    )
    assert snap.account_id == 1
    assert snap.total_equity == Decimal("1500000")
    assert snap.daily_return_pct == Decimal("0.25")
