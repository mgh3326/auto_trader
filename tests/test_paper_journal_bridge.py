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


def _make_simple_closed_journal(*, pnl_pct: float, journal_id: int) -> TradeJournal:
    j = TradeJournal(
        symbol=f"SYM{journal_id:03d}",
        instrument_type=InstrumentType.equity_kr,
        thesis="Test",
        entry_price=Decimal("10000"),
        account_type="paper",
        account="paper-test",
        status="closed",
        pnl_pct=Decimal(str(pnl_pct)),
    )
    j.id = journal_id
    j.created_at = now_kst()
    j.updated_at = now_kst()
    return j


class TestRecommendGoLive:
    """recommend_go_live 판정 테스트."""

    @pytest.mark.asyncio
    async def test_all_criteria_met_go_live(self, monkeypatch):
        """세 기준 모두 충족 → go_live."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "momentum"

        # 25 trades: 16 wins (3.0%), 9 losses (-2.0%)
        # win_rate = 64%, total_return = 16*3 + 9*(-2) = 30.0
        journals = [
            _make_simple_closed_journal(pnl_pct=3.0, journal_id=i + 1)
            for i in range(16)
        ] + [
            _make_simple_closed_journal(pnl_pct=-2.0, journal_id=i + 17)
            for i in range(9)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(2),  # active_positions count
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["success"] is True
        assert result["recommendation"] == "go_live"
        assert result["all_passed"] is True
        assert result["criteria"]["min_trades"]["passed"] is True
        assert result["criteria"]["min_win_rate"]["passed"] is True
        assert result["criteria"]["min_return_pct"]["passed"] is True
        assert result["summary"]["total_trades"] == 25
        assert result["summary"]["active_positions"] == 2

    @pytest.mark.asyncio
    async def test_insufficient_trades_not_ready(self, monkeypatch):
        """거래 수 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        journals = [
            _make_simple_closed_journal(pnl_pct=5.0, journal_id=i + 1)
            for i in range(10)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_trades"]["passed"] is False
        assert result["criteria"]["min_trades"]["actual"] == 10
        assert result["criteria"]["min_trades"]["required"] == 20

    @pytest.mark.asyncio
    async def test_low_win_rate_not_ready(self, monkeypatch):
        """승률 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # 20 trades, 5 wins → 25%
        journals = [
            _make_simple_closed_journal(pnl_pct=2.0, journal_id=i + 1)
            for i in range(5)
        ] + [
            _make_simple_closed_journal(pnl_pct=-1.0, journal_id=i + 6)
            for i in range(15)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_win_rate"]["passed"] is False

    @pytest.mark.asyncio
    async def test_negative_return_not_ready(self, monkeypatch):
        """수익률 미달 → not_ready."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # 20 trades, 11 wins at 1%, 9 losses at -3% → total = 11 - 27 = -16
        journals = [
            _make_simple_closed_journal(pnl_pct=1.0, journal_id=i + 1)
            for i in range(11)
        ] + [
            _make_simple_closed_journal(pnl_pct=-3.0, journal_id=i + 12)
            for i in range(9)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["recommendation"] == "not_ready"
        assert result["criteria"]["min_return_pct"]["passed"] is False

    @pytest.mark.asyncio
    async def test_custom_thresholds(self, monkeypatch):
        """커스텀 기준값 override."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        journals = [
            _make_simple_closed_journal(pnl_pct=1.0, journal_id=i + 1)
            for i in range(10)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(0),
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test",
            min_trades=5,
            min_win_rate=80.0,
            min_return_pct=5.0,
        )
        assert result["recommendation"] == "go_live"
        assert result["criteria"]["min_trades"]["required"] == 5

    @pytest.mark.asyncio
    async def test_account_not_found(self, monkeypatch):
        """존재하지 않는 account → 에러."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = None
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [account_result])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="nonexistent"
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_active_journals_excluded_from_metrics(self, monkeypatch):
        """active journal은 집계에서 제외 — closed만 계산."""
        from app.mcp_server.tooling import paper_journal_bridge

        mock_account = MagicMock()
        mock_account.name = "paper-test"
        mock_account.strategy_name = "test"

        # Query returns closed only (the function queries closed only)
        # but we verify the count is correct
        journals = [
            _make_simple_closed_journal(pnl_pct=5.0, journal_id=i + 1)
            for i in range(20)
        ]

        mock_scalars_account = MagicMock()
        mock_scalars_account.one_or_none.return_value = mock_account
        account_result = MagicMock()
        account_result.scalars.return_value = mock_scalars_account

        _mock_bridge_session(monkeypatch, [
            account_result,
            _scalars_result(journals),
            _scalar_one_result(5),  # 5 active positions
        ])

        result = await paper_journal_bridge.recommend_go_live(
            account_name="paper-test"
        )
        assert result["summary"]["total_trades"] == 20
        assert result["summary"]["active_positions"] == 5
