# tests/test_mcp_trade_journal.py
"""MCP tool tests for trade journal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType


def _build_session_cm(session: AsyncMock) -> AsyncMock:
    """Build an async context manager that yields the mock session."""
    session_cm = AsyncMock()
    session_cm.__aenter__.return_value = session
    session_cm.__aexit__.return_value = None
    return session_cm


def _mock_session_factory(session: AsyncMock) -> MagicMock:
    return MagicMock(return_value=_build_session_cm(session))


class TestSaveTradeJournal:
    @pytest.mark.asyncio
    async def test_save_draft_journal_crypto(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        # Mock: no existing active journal for this symbol
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="KRW-BTC",
                thesis="RSI oversold at 28, ADX rising — bounce expected",
                entry_price=95000000.0,
                target_price=105000000.0,
                stop_loss=90000000.0,
                min_hold_days=7,
                strategy="dca_oversold",
                indicators_snapshot={"rsi_14": 28, "adx": 22},
            )

        assert result["success"] is True
        assert result["action"] == "created"
        # Verify session.add was called with a TradeJournal
        added_obj = mock_session.add.call_args[0][0]
        assert isinstance(added_obj, TradeJournal)
        assert added_obj.symbol == "KRW-BTC"
        assert added_obj.instrument_type == InstrumentType.crypto
        assert added_obj.thesis == "RSI oversold at 28, ADX rising — bounce expected"
        assert added_obj.status == "draft"
        assert added_obj.min_hold_days == 7
        assert added_obj.hold_until is not None

    @pytest.mark.asyncio
    async def test_save_warns_on_existing_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        # Mock: existing active journal found
        existing = TradeJournal(
            id=1,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="old thesis",
            status="active",
        )
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = existing
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="KRW-BTC",
                thesis="New thesis",
            )

        assert result["success"] is True
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_save_rejects_empty_thesis(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        result = await save_trade_journal(symbol="KRW-BTC", thesis="")
        assert result["success"] is False
        assert "thesis" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_save_detects_instrument_type_us(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import save_trade_journal

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await save_trade_journal(
                symbol="AAPL",
                thesis="Strong Q1 earnings beat",
            )

        assert result["success"] is True
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.instrument_type == InstrumentType.equity_us


class TestGetTradeJournal:
    @pytest.mark.asyncio
    async def test_get_active_journals(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        now = datetime.now(UTC)
        journals = [
            TradeJournal(
                id=1,
                symbol="KRW-BTC",
                instrument_type=InstrumentType.crypto,
                thesis="RSI oversold",
                status="active",
                entry_price=Decimal("95000000"),
                target_price=Decimal("105000000"),
                stop_loss=Decimal("90000000"),
                hold_until=now + timedelta(days=5),
                min_hold_days=7,
                created_at=now,
                updated_at=now,
                side="buy",
            ),
        ]

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = journals
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(status="active")

        assert result["success"] is True
        assert result["summary"]["total_active"] == 1
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["symbol"] == "KRW-BTC"
        assert entry["hold_expired"] is False
        assert entry["hold_remaining_days"] >= 0

    @pytest.mark.asyncio
    async def test_get_journal_by_symbol(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        now = datetime.now(UTC)
        journals = [
            TradeJournal(
                id=2,
                symbol="AAPL",
                instrument_type=InstrumentType.equity_us,
                thesis="Earnings play",
                status="active",
                hold_until=now - timedelta(days=1),
                min_hold_days=7,
                created_at=now - timedelta(days=8),
                updated_at=now,
                side="buy",
            ),
        ]

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = journals
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(symbol="AAPL")

        assert result["success"] is True
        entry = result["entries"][0]
        assert entry["hold_expired"] is True
        assert entry["hold_remaining_days"] < 0

    @pytest.mark.asyncio
    async def test_get_returns_empty_when_none_found(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await get_trade_journal(symbol="NONEXIST")

        assert result["success"] is True
        assert result["entries"] == []
        assert result["summary"]["total_active"] == 0


class TestUpdateTradeJournal:
    @pytest.mark.asyncio
    async def test_update_draft_to_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(UTC)
        journal = TradeJournal(
            id=1,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="RSI oversold",
            status="draft",
            entry_price=Decimal("95000000"),
            min_hold_days=7,
            created_at=now,
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = journal

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(
                journal_id=1, status="active", trade_id=100
            )

        assert result["success"] is True
        assert journal.status == "active"
        assert journal.trade_id == 100
        assert journal.hold_until is not None  # recalculated from now

    @pytest.mark.asyncio
    async def test_update_close_with_pnl(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(UTC)
        journal = TradeJournal(
            id=2,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="Earnings play",
            status="active",
            entry_price=Decimal("175.50"),
            created_at=now - timedelta(days=10),
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = journal

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(
                journal_id=2,
                status="closed",
                exit_price=200.0,
                exit_reason="target_reached",
            )

        assert result["success"] is True
        assert journal.status == "closed"
        assert journal.exit_price == Decimal("200.0")
        assert journal.exit_reason == "target_reached"
        assert journal.exit_date is not None
        # pnl_pct = (200 / 175.5 - 1) * 100 ≈ 13.96
        assert float(journal.pnl_pct) == pytest.approx(13.96, abs=0.1)

    @pytest.mark.asyncio
    async def test_update_by_symbol_finds_latest_active(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        now = datetime.now(UTC)
        journal = TradeJournal(
            id=5,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="test",
            status="active",
            created_at=now,
            updated_at=now,
            side="buy",
        )

        mock_session = AsyncMock()
        mock_session.get.return_value = None  # journal_id not provided
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(
                symbol="KRW-BTC",
                notes="Updating strategy notes",
            )

        assert result["success"] is True
        assert journal.notes == "Updating strategy notes"

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        from app.mcp_server.tooling.trade_journal_tools import update_trade_journal

        mock_session = AsyncMock()
        mock_session.get.return_value = None
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.trade_journal_tools._session_factory",
            return_value=factory,
        ):
            result = await update_trade_journal(journal_id=999)

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestTradeJournalRegistration:
    def test_tools_are_registered(self) -> None:
        from tests._mcp_tooling_support import build_tools

        tools = build_tools()
        assert "save_trade_journal" in tools
        assert "get_trade_journal" in tools
        assert "update_trade_journal" in tools

    def test_tool_names_set(self) -> None:
        from app.mcp_server.tooling.trade_journal_registration import (
            TRADE_JOURNAL_TOOL_NAMES,
        )

        assert TRADE_JOURNAL_TOOL_NAMES == {
            "save_trade_journal",
            "get_trade_journal",
            "update_trade_journal",
        }


class TestCreateTradeJournalForBuy:
    """Tests for _create_trade_journal_for_buy helper."""

    @pytest.mark.asyncio
    async def test_create_trade_journal_for_buy_inserts_new_draft(self) -> None:
        """Helper should always insert a new draft journal row."""
        from app.mcp_server.tooling.order_execution import _create_trade_journal_for_buy
        from app.models.trade_journal import JournalStatus

        mock_session = AsyncMock()
        factory = _mock_session_factory(mock_session)

        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _create_trade_journal_for_buy(
                symbol="KRW-BTC",
                market_type="crypto",
                preview={
                    "price": 95_000_000.0,
                    "quantity": 0.001,
                    "estimated_value": 95_000.0,
                },
                thesis="weekly breakout",
                strategy="weekly-breakout",
                target_price=None,
                stop_loss=None,
                min_hold_days=7,
                notes="first scale-in",
                indicators_snapshot={"rsi_14": 58.2},
            )

        inserted = mock_session.add.call_args.args[0]
        assert inserted.status == JournalStatus.draft
        assert inserted.symbol == "KRW-BTC"
        assert inserted.trade_id is None
        assert result["journal_created"] is True
        assert result["journal_status"] == "draft"

    @pytest.mark.asyncio
    async def test_rebuy_same_symbol_creates_fresh_draft_without_active_lookup(
        self,
    ) -> None:
        """Re-buy should create a fresh draft instead of reusing an active journal."""
        from app.mcp_server.tooling.order_execution import _create_trade_journal_for_buy
        from app.models.trade_journal import JournalStatus

        mock_session = AsyncMock()

        async def refresh_side_effect(journal: TradeJournal) -> None:
            journal.id = 99

        mock_session.refresh.side_effect = refresh_side_effect
        factory = _mock_session_factory(mock_session)

        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _create_trade_journal_for_buy(
                symbol="KRW-BTC",
                market_type="crypto",
                preview={
                    "price": 96_000_000.0,
                    "quantity": 0.002,
                    "estimated_value": 192_000.0,
                },
                thesis="second scale-in after breakout retest",
                strategy="weekly-breakout",
                target_price=110_000_000.0,
                stop_loss=92_000_000.0,
                min_hold_days=14,
                notes="rebuy entry",
                indicators_snapshot={"rsi_14": 61.5},
            )

        inserted = mock_session.add.call_args.args[0]
        assert inserted.status == JournalStatus.draft
        assert inserted.symbol == "KRW-BTC"
        assert inserted.trade_id is None
        mock_session.execute.assert_not_called()
        assert result["journal_created"] is True
        assert result["journal_id"] == 99
        assert result["journal_status"] == "draft"


class TestJournalFillIntegration:
    """Integration tests for journal-fill linking functionality."""

    @pytest.mark.asyncio
    async def test_link_journal_to_fill_activates_draft(self) -> None:
        """Test that linking a fill to a draft journal activates it and sets trade_id."""
        from app.mcp_server.tooling.order_execution import _link_journal_to_fill

        mock_session = AsyncMock()
        # Create a mock draft journal
        draft_journal = TradeJournal(
            id=42,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="Test thesis",
            status="draft",
            min_hold_days=7,
        )

        mock_scalars = MagicMock()
        mock_scalars.first.return_value = draft_journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            await _link_journal_to_fill("KRW-BTC", trade_id=123)

        # Verify journal was updated
        assert draft_journal.status == "active"
        assert draft_journal.trade_id == 123
        assert draft_journal.hold_until is not None
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_link_journal_noop_when_no_draft(self) -> None:
        """Test that linking is a no-op when no draft journal exists."""
        from app.mcp_server.tooling.order_execution import _link_journal_to_fill

        mock_session = AsyncMock()
        # No draft journal found
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            await _link_journal_to_fill("KRW-BTC", trade_id=123)

        # Verify no commit was made (nothing to update)
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_journal_to_fill_queries_latest_draft_for_same_symbol(
        self,
    ) -> None:
        """Linking should target the newest draft for the symbol on re-buy."""
        from app.mcp_server.tooling.order_execution import _link_journal_to_fill
        from app.models.trade_journal import JournalStatus

        mock_session = AsyncMock()
        draft_journal = TradeJournal(
            id=84,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="newest thesis",
            status="draft",
        )

        mock_scalars = MagicMock()
        mock_scalars.first.return_value = draft_journal
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            await _link_journal_to_fill("KRW-BTC", trade_id=321)

        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile()

        assert compiled.params["symbol_1"] == "KRW-BTC"
        assert compiled.params["status_1"] == JournalStatus.draft
        assert "ORDER BY review.trade_journals.created_at DESC" in str(compiled)


class TestCloseJournalsOnSell:
    """Tests for _close_journals_on_sell FIFO helper."""

    @pytest.mark.asyncio
    async def test_close_journals_on_sell_closes_single_full_exit(self) -> None:
        """Sell quantity exactly matches journal quantity - close it."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell

        now = datetime.now(UTC)
        active = TradeJournal(
            id=42,
            symbol="KRW-BTC",
            instrument_type=InstrumentType.crypto,
            thesis="first entry",
            status="active",
            side="buy",
            entry_price=Decimal("95000000"),
            quantity=Decimal("0.01000000"),
            created_at=now,
            updated_at=now,
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [active]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _close_journals_on_sell(
                symbol="KRW-BTC",
                sell_quantity=0.01,
                sell_price=100000000.0,
                exit_reason="manual_take_profit",
            )

        assert active.status == "closed"
        assert active.exit_reason == "manual_take_profit"
        assert active.exit_date is not None
        assert float(active.pnl_pct) == pytest.approx(5.26, abs=0.1)
        assert result["journals_closed"] == 1
        assert result["journals_kept"] == 0
        assert result["closed_ids"] == [42]

    @pytest.mark.asyncio
    async def test_close_journals_on_sell_partial_sell_keeps_all_active(self) -> None:
        """Sell 5 when first journal has 8 - FIFO stops, no journals closed."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell

        now = datetime.now(UTC)
        first = TradeJournal(
            id=42,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="scale-in 1",
            status="active",
            side="buy",
            entry_price=Decimal("100"),
            quantity=Decimal("8"),
            created_at=now,
            updated_at=now,
        )
        second = TradeJournal(
            id=55,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="scale-in 2",
            status="active",
            side="buy",
            entry_price=Decimal("120"),
            quantity=Decimal("3"),
            created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [first, second]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _close_journals_on_sell(
                symbol="AAPL",
                sell_quantity=5.0,
                sell_price=130.0,
                exit_reason="rebalance",
            )

        # First journal qty (8) > sell qty (5), so FIFO stops, nothing closed
        assert first.status == "active"
        assert first.exit_price is None
        assert second.status == "active"
        assert second.exit_price is None
        assert result == {
            "journals_closed": 0,
            "journals_kept": 2,
            "closed_ids": [],
            "total_pnl_pct": 0.0,
        }
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_journals_on_sell_null_quantity_closes_without_consuming(
        self,
    ) -> None:
        """quantity=None journal closes immediately without affecting remaining qty."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell

        now = datetime.now(UTC)
        null_qty_journal = TradeJournal(
            id=99,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="legacy manual entry",
            status="active",
            side="buy",
            entry_price=Decimal("100"),
            quantity=None,
            created_at=now,
            updated_at=now,
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [null_qty_journal]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _close_journals_on_sell(
                symbol="AAPL",
                sell_quantity=5.0,
                sell_price=130.0,
            )

        assert null_qty_journal.status == "closed"
        assert null_qty_journal.exit_reason == "sold_via_place_order"
        assert result["journals_closed"] == 1
        # No weighted PnL since quantity is None
        assert result["total_pnl_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_close_journals_on_sell_fully_consumes_multiple(self) -> None:
        """Sell 11 with qty=8 and qty=3 journals - both close."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell

        now = datetime.now(UTC)
        first = TradeJournal(
            id=42,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="scale-in 1",
            status="active",
            side="buy",
            entry_price=Decimal("100"),
            quantity=Decimal("8"),
            created_at=now,
            updated_at=now,
        )
        second = TradeJournal(
            id=55,
            symbol="AAPL",
            instrument_type=InstrumentType.equity_us,
            thesis="scale-in 2",
            status="active",
            side="buy",
            entry_price=Decimal("120"),
            quantity=Decimal("3"),
            created_at=now + timedelta(seconds=1),
            updated_at=now + timedelta(seconds=1),
        )

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [first, second]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _close_journals_on_sell(
                symbol="AAPL",
                sell_quantity=11.0,
                sell_price=130.0,
                exit_reason="full_exit",
            )

        assert first.status == "closed"
        assert second.status == "closed"
        assert result["journals_closed"] == 2
        assert result["journals_kept"] == 0
        assert result["closed_ids"] == [42, 55]

    @pytest.mark.asyncio
    async def test_close_journals_on_sell_no_active_journals(self) -> None:
        """No active journals - return zeros."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            result = await _close_journals_on_sell(
                symbol="AAPL",
                sell_quantity=5.0,
                sell_price=130.0,
            )

        assert result == {
            "journals_closed": 0,
            "journals_kept": 0,
            "closed_ids": [],
            "total_pnl_pct": 0.0,
        }
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_journals_queries_active_ordered_by_created_at(self) -> None:
        """Verify SQL queries only active journals ordered by created_at ASC."""
        from app.mcp_server.tooling.order_execution import _close_journals_on_sell
        from app.models.trade_journal import JournalStatus

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute.return_value = mock_result

        factory = _mock_session_factory(mock_session)
        with patch(
            "app.mcp_server.tooling.order_execution._order_session_factory",
            return_value=factory,
        ):
            await _close_journals_on_sell(
                symbol="AAPL",
                sell_quantity=5.0,
                sell_price=130.0,
            )

        stmt = mock_session.execute.call_args.args[0]
        compiled = stmt.compile()

        assert compiled.params["symbol_1"] == "AAPL"
        assert compiled.params["status_1"] == JournalStatus.active
        assert "ORDER BY review.trade_journals.created_at ASC" in str(compiled)
