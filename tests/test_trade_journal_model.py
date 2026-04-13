# tests/test_trade_journal_model.py
"""Unit tests for TradeJournal model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.trade_journal import JournalStatus, TradeJournal
from app.models.trading import InstrumentType


class TestTradeJournalModel:
    def test_create_minimal_journal(self) -> None:
        journal = TradeJournal(
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="RSI oversold bounce play",
        )
        assert journal.symbol == "KRW-BTC"
        assert journal.instrument_type == InstrumentType.crypto
        assert journal.thesis == "RSI oversold bounce play"
        assert journal.side == "buy"
        assert journal.status == "draft"

    def test_create_full_journal(self) -> None:
        now = datetime.now(UTC)
        journal = TradeJournal(
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            side="buy",
            thesis="Strong earnings momentum",
            strategy="momentum",
            entry_price=Decimal("175.50"),
            quantity=Decimal("10"),
            amount=Decimal("1755.00"),
            target_price=Decimal("200.00"),
            stop_loss=Decimal("160.00"),
            min_hold_days=14,
            hold_until=now + timedelta(days=14),
            indicators_snapshot={"rsi_14": 42, "adx": 25},
            status="active",
            account="kis",
        )
        assert journal.strategy == "momentum"
        assert journal.target_price == Decimal("200.00")
        assert journal.stop_loss == Decimal("160.00")
        assert journal.min_hold_days == 14
        assert journal.indicators_snapshot == {"rsi_14": 42, "adx": 25}

    def test_journal_status_enum_values(self) -> None:
        assert JournalStatus.draft == "draft"
        assert JournalStatus.active == "active"
        assert JournalStatus.closed == "closed"
        assert JournalStatus.stopped == "stopped"
        assert JournalStatus.expired == "expired"

    def test_pnl_calculation(self) -> None:
        _journal = TradeJournal(
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="test",
            entry_price=Decimal("100.00"),
            exit_price=Decimal("120.00"),
        )
        expected_pnl = (Decimal("120.00") / Decimal("100.00") - 1) * 100
        assert float(expected_pnl) == pytest.approx(20.0)

    def test_table_args(self) -> None:
        assert TradeJournal.__table_args__[-1] == {"schema": "review"}
        assert TradeJournal.__tablename__ == "trade_journals"


class TestAccountTypeField:
    """account_type 및 paper_trade_id 필드 테스트."""

    def test_default_account_type_is_live(self) -> None:
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
        )
        assert journal.account_type == "live"

    def test_paper_account_type(self) -> None:
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
            account_type="paper",
            paper_trade_id=42,
            account="paper-momentum",
        )
        assert journal.account_type == "paper"
        assert journal.paper_trade_id == 42

    def test_live_journal_paper_trade_id_is_none(self) -> None:
        journal = TradeJournal(
            symbol="005930",
            instrument_type=InstrumentType.equity_kr,
            thesis="Test thesis",
        )
        assert journal.paper_trade_id is None
