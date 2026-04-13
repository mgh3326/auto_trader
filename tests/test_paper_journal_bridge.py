"""Paper Journal Bridge unit tests — compare_strategies, recommend_go_live."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import now_kst
from app.models.trade_journal import TradeJournal
from app.models.trading import InstrumentType


def _make_closed_journal(
    *,
    symbol: str,
    account: str,
    strategy: str | None,
    account_type: str = "paper",
    entry_price: float,
    pnl_pct: float,
    journal_id: int,
) -> TradeJournal:
    j = TradeJournal(
        symbol=symbol,
        instrument_type=InstrumentType.equity_kr,
        thesis="Test thesis",
        entry_price=Decimal(str(entry_price)),
        account_type=account_type,
        account=account,
        strategy=strategy,
        status="closed",
        pnl_pct=Decimal(str(pnl_pct)),
    )
    j.id = journal_id
    j.created_at = now_kst()
    j.updated_at = now_kst()
    j.exit_date = now_kst()
    j.exit_price = Decimal(str(entry_price * (1 + pnl_pct / 100)))
    return j


def _mock_bridge_session(monkeypatch, execute_side_effects: list):
    """Set up mock session factory for paper_journal_bridge."""
    from app.mcp_server.tooling import paper_journal_bridge

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=execute_side_effects)

    cm = AsyncMock()
    cm.__aenter__.return_value = mock_session
    cm.__aexit__.return_value = None
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(paper_journal_bridge, "_session_factory", lambda: factory)
    return mock_session


def _scalars_result(items: list) -> MagicMock:
    """Build a mock SQLAlchemy result with .scalars().all()."""
    scalars = MagicMock()
    scalars.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _scalar_one_result(value) -> MagicMock:
    """Build a mock SQLAlchemy result with .scalar_one()."""
    result = MagicMock()
    result.scalar_one.return_value = value
    return result


class TestCompareStrategies:
    """compare_strategies 단위 테스트."""

    @pytest.mark.asyncio
    async def test_closed_only_aggregation(self, monkeypatch):
        """closed journal 기준으로만 집계. active는 제외."""
        from app.mcp_server.tooling import paper_journal_bridge

        closed_win = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        closed_loss = _make_closed_journal(
            symbol="AAPL", account="paper-m", strategy="momentum",
            entry_price=150, pnl_pct=-3.0, journal_id=2,
        )
        # active journal — must be excluded from aggregation
        active_journal = TradeJournal(
            symbol="TSLA",
            instrument_type=InstrumentType.equity_us,
            thesis="Test",
            account_type="paper",
            account="paper-m",
            strategy="momentum",
            status="active",
        )
        active_journal.id = 3
        active_journal.created_at = now_kst()
        active_journal.updated_at = now_kst()

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),                          # accounts
            _scalars_result([closed_win, closed_loss, active_journal]),  # paper journals
            _scalars_result([]),                                      # live journals
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)

        assert result["success"] is True
        assert len(result["strategies"]) == 1
        s = result["strategies"][0]
        assert s["total_trades"] == 2  # active excluded
        assert s["win_count"] == 1
        assert s["loss_count"] == 1
        assert s["win_rate"] == 50.0
        assert s["total_return_pct"] == 2.0  # 5.0 + (-3.0)
        assert s["best_trade"]["symbol"] == "005930"
        assert s["worst_trade"]["symbol"] == "AAPL"

    @pytest.mark.asyncio
    async def test_strategy_name_filter(self, monkeypatch):
        """strategy_name 필터가 TradeJournal.strategy 기준으로 적용."""
        from app.mcp_server.tooling import paper_journal_bridge

        # Only momentum journals returned (filtered by query)
        momentum_j = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),
            _scalars_result([momentum_j]),
            _scalars_result([]),
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, strategy_name="momentum"
        )
        assert result["success"] is True
        assert len(result["strategies"]) == 1
        assert result["strategies"][0]["strategy_name"] == "momentum"

    @pytest.mark.asyncio
    async def test_include_live_comparison_false(self, monkeypatch):
        """include_live_comparison=False → live_vs_paper 빈 배열."""
        from app.mcp_server.tooling import paper_journal_bridge

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),  # accounts
            _scalars_result([]),  # paper journals
            # no live query issued
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=False
        )
        assert result["success"] is True
        assert result["live_vs_paper"] == []

    @pytest.mark.asyncio
    async def test_live_vs_paper_same_symbol(self, monkeypatch):
        """같은 종목 live/paper closed journal → 비교 결과 생성."""
        from app.mcp_server.tooling import paper_journal_bridge

        paper_j = _make_closed_journal(
            symbol="005930", account="paper-m", strategy="momentum",
            account_type="paper", entry_price=72000, pnl_pct=5.0, journal_id=1,
        )
        live_j = _make_closed_journal(
            symbol="005930", account="kis-main", strategy=None,
            account_type="live", entry_price=71000, pnl_pct=3.0, journal_id=2,
        )

        mock_account = MagicMock()
        mock_account.id = 1
        mock_account.name = "paper-m"
        mock_account.strategy_name = "momentum"

        _mock_bridge_session(monkeypatch, [
            _scalars_result([mock_account]),
            _scalars_result([paper_j]),
            _scalars_result([live_j]),
        ])

        result = await paper_journal_bridge.compare_strategies(
            days=30, include_live_comparison=True
        )
        assert result["success"] is True
        assert len(result["live_vs_paper"]) == 1
        comp = result["live_vs_paper"][0]
        assert comp["symbol"] == "005930"
        assert comp["paper_pnl_pct"] == 5.0
        assert comp["live_pnl_pct"] == 3.0
        assert comp["delta_pnl_pct"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_live_only_symbol_not_in_comparison(self, monkeypatch):
        """live만 있고 paper 없는 종목은 live_vs_paper에 미포함."""
        from app.mcp_server.tooling import paper_journal_bridge

        live_j = _make_closed_journal(
            symbol="TSLA", account="kis-main", strategy=None,
            account_type="live", entry_price=200, pnl_pct=10.0, journal_id=1,
        )

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),
            _scalars_result([]),
            _scalars_result([live_j]),
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)
        assert result["live_vs_paper"] == []

    @pytest.mark.asyncio
    async def test_empty_strategies_no_error(self, monkeypatch):
        """paper journal 없으면 빈 strategies 반환."""
        from app.mcp_server.tooling import paper_journal_bridge

        _mock_bridge_session(monkeypatch, [
            _scalars_result([]),
            _scalars_result([]),
            _scalars_result([]),
        ])

        result = await paper_journal_bridge.compare_strategies(days=30)
        assert result["success"] is True
        assert result["strategies"] == []
        assert result["live_vs_paper"] == []
