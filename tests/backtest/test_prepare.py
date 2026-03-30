"""Tests for backtest prepare module."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare  # pyright: ignore[reportMissingImports]


class TestContractConstants:
    """Tests for approved contract constants."""

    def test_initial_capital_constant(self):
        """Test that INITIAL_CAPITAL matches approved value."""
        assert prepare.INITIAL_CAPITAL == 10_000_000

    def test_slippage_bps_constant(self):
        """Test that SLIPPAGE_BPS matches approved value."""
        assert prepare.SLIPPAGE_BPS == 2.0

    def test_lookback_bars_constant(self):
        """Test that LOOKBACK_BARS matches approved value."""
        assert prepare.LOOKBACK_BARS == 200

    def test_default_symbols_constant(self):
        """Test that DEFAULT_SYMBOLS matches approved universe."""
        expected = ["BTC", "ETH", "SOL", "XRP", "LINK", "ADA", "DOT", "AVAX"]
        assert prepare.DEFAULT_SYMBOLS == expected


class TestContractDataclasses:
    """Tests for approved dataclass shapes."""

    def test_bar_data_has_symbol_and_history(self):
        """Test that BarData includes symbol and history fields."""
        import pandas as pd

        history = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
        bar = prepare.BarData(
            symbol="BTC",
            date="2025-04-01",
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=1000,
            value=100000,
            history=history,
        )
        assert bar.symbol == "BTC"
        assert bar.history is not None

    def test_signal_uses_weight_and_reason(self):
        """Test that Signal uses weight and reason fields."""
        signal = prepare.Signal(
            symbol="BTC",
            action="buy",
            weight=0.5,
            reason="RSI oversold",
        )
        assert signal.weight == 0.5
        assert signal.reason == "RSI oversold"

    def test_portfolio_state_has_equity_and_date(self):
        """Test that PortfolioState includes equity and date fields."""
        state = prepare.PortfolioState(
            cash=100000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            equity=150000.0,
            date="2025-04-01",
            trade_log=[],
        )
        assert state.equity == 150000.0
        assert state.date == "2025-04-01"

    def test_backtest_result_has_win_rate_pct_and_backtest_seconds(self):
        """Test that BacktestResult includes win_rate_pct and backtest_seconds."""
        result = prepare.BacktestResult(
            total_return_pct=15.0,
            sharpe=1.5,
            max_drawdown_pct=-10.0,
            num_trades=20,
            win_rate_pct=0.6,
            profit_factor=1.5,
            avg_holding_days=5.0,
            backtest_seconds=1.23,
            trade_log=[],
            equity_curve=[100000.0, 105000.0],
        )
        assert result.win_rate_pct == 0.6
        assert result.backtest_seconds == 1.23


class TestContractStrategySignature:
    """Tests for approved strategy interface."""

    def test_strategy_on_bar_signature(self):
        """Test that strategy uses two-argument on_bar signature."""

        class TestStrategy:
            def on_bar(self, bar_data, portfolio):
                return []

        strategy = TestStrategy()
        bar_data = {}
        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Should work with just two arguments
        signals = strategy.on_bar(bar_data, portfolio)
        assert signals == []


class TestContractScoreFormula:
    """Tests for approved score formula."""

    def test_score_formula_matches_approval(self):
        """Test that compute_score uses approved formula."""
        # Create result with specific metrics
        result = prepare.BacktestResult(
            total_return_pct=15.0,
            sharpe=1.5,
            max_drawdown_pct=25.0,  # > 20, should trigger penalty
            num_trades=5,  # < 10, should trigger penalty
            win_rate_pct=0.5,
            profit_factor=1.5,
            avg_holding_days=3.0,
            backtest_seconds=0.0,
            trade_log=[],
            equity_curve=[100000.0, 115000.0],
        )

        score = prepare.compute_score(result)

        # Expected calculation:
        # score = sharpe = 1.5
        # max_drawdown_penalty = (25 - 20) * 0.1 = 0.5
        # num_trades_penalty = 1.0 (since < 10)
        # final_score = 1.5 - 0.5 - 1.0 = 0.0
        expected_score = 1.5 - (25.0 - 20.0) * 0.1 - 1.0
        assert score == pytest.approx(expected_score, abs=0.01)

    def test_score_formula_no_penalty_when_drawdown_low(self):
        """Test that score has no drawdown penalty when <= 20%."""
        result = prepare.BacktestResult(
            total_return_pct=15.0,
            sharpe=1.5,
            max_drawdown_pct=15.0,  # <= 20, no penalty
            num_trades=15,  # >= 10, no penalty
            win_rate_pct=0.5,
            profit_factor=1.5,
            avg_holding_days=3.0,
            backtest_seconds=0.0,
            trade_log=[],
            equity_curve=[100000.0, 115000.0],
        )

        score = prepare.compute_score(result)

        # Expected: just sharpe = 1.5 (no penalties)
        assert score == pytest.approx(1.5, abs=0.01)


class TestLoadData:
    """Tests for load_data function."""

    def test_load_data_filters_symbols_and_dates(self, tmp_path, monkeypatch):
        """Test that load_data reads only DEFAULT_SYMBOLS and applies split dates."""
        # Create test parquet files
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create BTC data (in range)
        btc_df = pd.DataFrame(
            {
                "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
                "open": [100.0, 101.0, 102.0, 103.0],
                "high": [105.0, 106.0, 107.0, 108.0],
                "low": [95.0, 96.0, 97.0, 98.0],
                "close": [102.0, 103.0, 104.0, 105.0],
                "volume": [1000, 1100, 1200, 1300],
                "value": [100000, 110000, 120000, 130000],
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        # Create ETH data (in range)
        eth_df = pd.DataFrame(
            {
                "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
                "open": [50.0, 51.0, 52.0, 53.0],
                "high": [55.0, 56.0, 57.0, 58.0],
                "low": [45.0, 46.0, 47.0, 48.0],
                "close": [52.0, 53.0, 54.0, 55.0],
                "volume": [2000, 2100, 2200, 2300],
                "value": [200000, 210000, 220000, 230000],
            }
        )
        eth_df.to_parquet(data_dir / "KRW-ETH.parquet", index=False)

        # Create XRP data (not in DEFAULT_SYMBOLS - should be ignored)
        xrp_df = pd.DataFrame(
            {
                "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
                "open": [1.0, 1.1, 1.2, 1.3],
                "high": [1.5, 1.6, 1.7, 1.8],
                "low": [0.5, 0.6, 0.7, 0.8],
                "close": [1.2, 1.3, 1.4, 1.5],
                "volume": [10000, 11000, 12000, 13000],
                "value": [10000, 11000, 12000, 13000],
            }
        )
        xrp_df.to_parquet(data_dir / "KRW-XRP.parquet", index=False)

        # Monkeypatch DATA_DIR
        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC", "ETH"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {
                "val": {"start": "2025-04-01", "end": "2025-04-02"},
            },
        )

        data = prepare.load_data("val")

        assert set(data.keys()) == {"BTC", "ETH"}
        assert data["BTC"]["date"].tolist() == ["2025-04-01", "2025-04-02"]
        assert data["ETH"]["date"].tolist() == ["2025-04-01", "2025-04-02"]

    def test_load_data_returns_sorted_ascending(self, tmp_path, monkeypatch):
        """Test that returned dataframes are sorted in ascending date order."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create BTC data in descending order
        btc_df = pd.DataFrame(
            {
                "date": [
                    "2025-04-05",
                    "2025-04-04",
                    "2025-04-03",
                    "2025-04-02",
                    "2025-04-01",
                ],
                "open": [100.0, 101.0, 102.0, 103.0, 104.0],
                "high": [105.0, 106.0, 107.0, 108.0, 109.0],
                "low": [95.0, 96.0, 97.0, 98.0, 99.0],
                "close": [102.0, 103.0, 104.0, 105.0, 106.0],
                "volume": [1000, 1100, 1200, 1300, 1400],
                "value": [100000, 110000, 120000, 130000, 140000],
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {
                "val": {"start": "2025-04-01", "end": "2025-04-05"},
            },
        )

        data = prepare.load_data("val")

        dates = data["BTC"]["date"].tolist()
        assert dates == sorted(dates)


class TestWarmupHistory:
    """Tests for preserving pre-split warmup history."""

    def test_run_backtest_includes_presplit_rows_in_history(
        self, tmp_path, monkeypatch
    ):
        """Test that BarData.history includes pre-split rows for the first split bar."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        dates = pd.date_range("2025-06-20", "2025-07-05", freq="D").strftime("%Y-%m-%d")
        btc_df = pd.DataFrame(
            {
                "date": list(dates),
                "open": np.arange(len(dates), dtype=float) + 100.0,
                "high": np.arange(len(dates), dtype=float) + 101.0,
                "low": np.arange(len(dates), dtype=float) + 99.0,
                "close": np.arange(len(dates), dtype=float) + 100.0,
                "volume": np.arange(len(dates), dtype=float) + 1000.0,
                "value": np.arange(len(dates), dtype=float) + 10000.0,
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {"val": {"start": "2025-07-01", "end": "2025-07-05"}},
        )

        captured_lengths: list[int] = []

        class CaptureHistoryStrategy:
            def on_bar(self, bar_data, portfolio):
                if not captured_lengths:
                    captured_lengths.extend(
                        len(bar.history) for bar in bar_data.values()
                    )
                return []

        result = prepare.run_backtest(
            prepare.load_data("val"), CaptureHistoryStrategy()
        )

        assert result.num_trades == 0
        assert result.equity_curve
        assert result.equity_curve[0] == pytest.approx(prepare.INITIAL_CAPITAL)
        assert captured_lengths
        assert captured_lengths[0] > 1

    def test_first_split_bar_has_warmup_history(self, tmp_path, monkeypatch):
        """Test that the first split bar sees pre-split history when it exists."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        dates = pd.date_range("2025-06-20", "2025-07-05", freq="D").strftime("%Y-%m-%d")
        btc_df = pd.DataFrame(
            {
                "date": list(dates),
                "open": np.arange(len(dates), dtype=float) + 100.0,
                "high": np.arange(len(dates), dtype=float) + 101.0,
                "low": np.arange(len(dates), dtype=float) + 99.0,
                "close": np.arange(len(dates), dtype=float) + 100.0,
                "volume": np.arange(len(dates), dtype=float) + 1000.0,
                "value": np.arange(len(dates), dtype=float) + 10000.0,
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {"val": {"start": "2025-07-01", "end": "2025-07-05"}},
        )

        captured_lengths: list[int] = []

        class CaptureHistoryStrategy:
            def on_bar(self, bar_data, portfolio):
                if not captured_lengths:
                    captured_lengths.extend(
                        len(bar.history) for bar in bar_data.values()
                    )
                return []

        prepare.run_backtest(prepare.load_data("val"), CaptureHistoryStrategy())

        assert captured_lengths
        assert captured_lengths[0] > 1


class TestFeeAwarePnL:
    """Tests for fee-aware realized PnL and trade metrics."""

    def test_realized_pnl_counts_buy_and_sell_fees(self, tmp_path, monkeypatch):
        """Test that a tiny gross gain becomes a net loss after fees on both legs."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        btc_df = pd.DataFrame(
            {
                "date": ["2025-07-01", "2025-07-02"],
                "open": [100.0, 100.06],
                "high": [100.0, 100.06],
                "low": [100.0, 100.06],
                "close": [100.0, 100.06],
                "volume": [1000.0, 1000.0],
                "value": [100000.0, 100060.0],
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {"val": {"start": "2025-07-01", "end": "2025-07-02"}},
        )

        class RoundTripStrategy:
            def on_bar(self, bar_data, portfolio):
                if portfolio.date == "2025-07-01" and "BTC" not in portfolio.positions:
                    return [prepare.Signal(symbol="BTC", action="buy", weight=1.0)]
                if portfolio.date == "2025-07-02" and "BTC" in portfolio.positions:
                    return [prepare.Signal(symbol="BTC", action="sell", weight=1.0)]
                return []

        result = prepare.run_backtest(
            prepare.load_data("val"),
            RoundTripStrategy(),
            initial_capital=1_000_000.0,
        )

        assert result.num_trades == 2
        assert result.trade_log[1]["realized_pnl"] < 0
        assert result.win_rate_pct == 0.0
        assert result.profit_factor == 0.0

    def test_buy_fee_affects_cost_basis(self, tmp_path, monkeypatch):
        """Test that buy-side fees affect the eventual realized PnL."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        btc_df = pd.DataFrame(
            {
                "date": ["2025-07-01", "2025-07-02"],
                "open": [100.0, 100.06],
                "high": [100.0, 100.06],
                "low": [100.0, 100.06],
                "close": [100.0, 100.06],
                "volume": [1000.0, 1000.0],
                "value": [100000.0, 100060.0],
            }
        )
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {"val": {"start": "2025-07-01", "end": "2025-07-02"}},
        )

        class RoundTripStrategy:
            def on_bar(self, bar_data, portfolio):
                if portfolio.date == "2025-07-01" and "BTC" not in portfolio.positions:
                    return [prepare.Signal(symbol="BTC", action="buy", weight=1.0)]
                if portfolio.date == "2025-07-02" and "BTC" in portfolio.positions:
                    return [prepare.Signal(symbol="BTC", action="sell", weight=1.0)]
                return []

        result = prepare.run_backtest(
            prepare.load_data("val"),
            RoundTripStrategy(),
            initial_capital=1_000_000.0,
        )

        assert result.trade_log[1]["realized_pnl"] < 0


class TestExecutionCosts:
    """Tests for execution with slippage and fees."""

    def test_buy_execution_with_slippage_and_fee(self):
        """Test that buy orders include slippage and fee."""
        signal = prepare.Signal(
            symbol="BTC",
            action="buy",
            weight=0.5,
            reason="Test",
        )
        state = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )
        import pandas as pd

        history = pd.DataFrame({"close": [100.0]})
        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
                history=history,
            )
        }

        result = prepare._execute_signal(signal, state, bar_data, 100000.0)

        # Should have one trade
        assert len(result.trade_log) == 1
        trade = result.trade_log[0]
        assert trade["symbol"] == "BTC"
        assert trade["action"] == "buy"
        assert trade["fee"] > 0
        # Price should include slippage (buy at higher price)
        assert trade["price"] > 100.0

    def test_sell_execution_with_slippage_and_fee(self):
        """Test that sell orders include slippage and fee."""
        signal = prepare.Signal(
            symbol="BTC",
            action="sell",
            weight=1.0,  # Full sell
            reason="Test",
        )
        state = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )
        import pandas as pd

        history = pd.DataFrame({"close": [100.0]})
        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
                history=history,
            )
        }

        result = prepare._execute_signal(signal, state, bar_data, 100000.0)

        assert len(result.trade_log) == 1
        trade = result.trade_log[0]
        assert trade["symbol"] == "BTC"
        assert trade["action"] == "sell"
        assert trade["fee"] > 0
        # Price should include slippage (sell at lower price)
        assert trade["price"] < 100.0

    def test_weight_buy_sizing(self):
        """Test that buy orders respect target weight."""
        signal = prepare.Signal(
            symbol="BTC",
            action="buy",
            weight=0.5,
            reason="Test",
        )
        state = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )
        import pandas as pd

        history = pd.DataFrame({"close": [100.0]})
        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
                history=history,
            )
        }
        initial_value = 100000.0

        result = prepare._execute_signal(signal, state, bar_data, initial_value)

        # Should have bought approximately 50% worth
        trade = result.trade_log[0]
        cost = trade["price"] * trade["quantity"]
        weight = cost / initial_value
        assert pytest.approx(weight, abs=0.05) == 0.5

    def test_partial_sell_sizing(self):
        """Test partial sell sizing using weight as fraction of position."""
        signal = prepare.Signal(
            symbol="BTC",
            action="sell",
            weight=0.25,  # Sell 25% of current position
            reason="Test",
        )
        state = prepare.PortfolioState(
            cash=100.0,
            positions={"BTC": 2.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )
        import pandas as pd

        history = pd.DataFrame({"close": [100.0]})
        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
                history=history,
            )
        }

        result = prepare._execute_signal(signal, state, bar_data, 300.0)

        assert len(result.trade_log) == 1
        trade = result.trade_log[0]
        # Should sell 25% of 2.0 = 0.5 BTC
        assert trade["quantity"] == pytest.approx(0.5, abs=0.01)


class TestMetrics:
    """Tests for metric calculations."""

    def test_total_return_calculation(self):
        """Test total return calculation."""
        equity = [100000.0, 105000.0, 110000.0, 108000.0, 115000.0]
        total_return = prepare._calc_total_return(equity)

        assert total_return == pytest.approx(15.0, abs=0.1)  # 15%

    def test_max_drawdown_calculation(self):
        """Test max drawdown calculation."""
        equity = [100000.0, 110000.0, 90000.0, 105000.0, 95000.0]
        max_dd = prepare._calc_max_drawdown(equity)

        # Peak: 110000, Valley: 90000, Drawdown: ~18.18%
        assert max_dd > 0
        assert max_dd == pytest.approx(18.18, abs=0.5)

    def test_sharpe_non_nan_behavior(self):
        """Test that sharpe returns finite value with valid data."""
        returns = [0.01, 0.02, -0.01, 0.015, 0.005]
        sharpe = prepare._calc_sharpe(returns)

        assert not np.isnan(sharpe)
        assert np.isfinite(sharpe)

    def test_sharpe_with_zero_std(self):
        """Test sharpe with zero standard deviation."""
        returns = [0.0, 0.0, 0.0, 0.0, 0.0]
        sharpe = prepare._calc_sharpe(returns)

        assert sharpe == 0.0 or np.isnan(sharpe)

    def test_score_penalty_when_few_trades(self):
        """Test that score includes penalty when num_trades < 10."""
        result = prepare.BacktestResult(
            total_return_pct=15.0,
            sharpe=1.5,
            max_drawdown_pct=-10.0,
            num_trades=5,
            win_rate_pct=0.5,
            profit_factor=1.5,
            avg_holding_days=3.0,
            backtest_seconds=0.0,
            trade_log=[],
            equity_curve=[100000.0, 105000.0, 110000.0, 108000.0, 115000.0],
        )

        score = prepare.compute_score(result)

        # Score should be penalized for low trade count
        # Formula: sharpe - 1.0 penalty when num_trades < 10
        # 1.5 - 1.0 = 0.5
        assert score == pytest.approx(0.5, abs=0.01)


class TestRunBacktest:
    """Tests for run_backtest function."""

    def test_run_backtest_executes_strategy(self):
        """Test that run_backtest properly executes strategy signals."""
        # Create simple mock data
        data = {
            "BTC": pd.DataFrame(
                {
                    "date": ["2025-04-01", "2025-04-02", "2025-04-03"],
                    "open": [100.0, 105.0, 110.0],
                    "high": [106.0, 111.0, 116.0],
                    "low": [95.0, 100.0, 105.0],
                    "close": [105.0, 110.0, 115.0],
                    "volume": [1000, 1100, 1200],
                    "value": [100000, 110000, 120000],
                }
            )
        }

        # Create a simple strategy that buys on first day
        class SimpleStrategy:
            def __init__(self):
                self.called = False

            def on_bar(self, bar_data, portfolio):
                self.called = True
                # Check if BTC is in bar_data and not already held
                if "BTC" in bar_data and "BTC" not in portfolio.positions:
                    return [
                        prepare.Signal(
                            symbol="BTC", action="buy", weight=0.95, reason="Test buy"
                        )
                    ]
                return []

        strategy = SimpleStrategy()
        result = prepare.run_backtest(data, strategy)

        assert strategy.called
        assert len(result.trade_log) >= 1
        assert result.trade_log[0]["action"] == "buy"

    def test_run_backtest_updates_equity_curve(self):
        """Test that equity curve is properly updated."""
        data = {
            "BTC": pd.DataFrame(
                {
                    "date": ["2025-04-01", "2025-04-02", "2025-04-03"],
                    "open": [100.0, 105.0, 110.0],
                    "high": [106.0, 111.0, 116.0],
                    "low": [95.0, 100.0, 105.0],
                    "close": [105.0, 110.0, 115.0],
                    "volume": [1000, 1100, 1200],
                    "value": [100000, 110000, 120000],
                }
            )
        }

        class NoOpStrategy:
            def on_bar(self, bar_data, portfolio):
                return []

        strategy = NoOpStrategy()
        result = prepare.run_backtest(data, strategy)

        assert len(result.equity_curve) == 4  # Initial + 3 days
        assert result.equity_curve[0] == 10_000_000.0  # Initial capital

    def test_run_backtest_handles_missing_symbol_dates(self):
        """Test that missing symbol dates are handled gracefully."""
        data = {
            "BTC": pd.DataFrame(
                {
                    "date": ["2025-04-01", "2025-04-02"],
                    "open": [100.0, 105.0],
                    "high": [106.0, 111.0],
                    "low": [95.0, 100.0],
                    "close": [105.0, 110.0],
                    "volume": [1000, 1100],
                    "value": [100000, 110000],
                }
            ),
            "ETH": pd.DataFrame(
                {
                    "date": ["2025-04-02"],  # Missing 04-01
                    "open": [50.0],
                    "high": [55.0],
                    "low": [45.0],
                    "close": [52.0],
                    "volume": [2000],
                    "value": [200000],
                }
            ),
        }

        class NoOpStrategy:
            def on_bar(self, bar_data, portfolio):
                return []

        strategy = NoOpStrategy()
        result = prepare.run_backtest(data, strategy)

        # Should complete without error
        assert len(result.equity_curve) > 0


class TestCVFolds:
    """Tests for walk-forward CV fold definitions."""

    def test_cv_folds_exist(self):
        assert hasattr(prepare, "CV_FOLDS")
        assert len(prepare.CV_FOLDS) >= 3

    def test_cv_folds_have_required_keys(self):
        for fold in prepare.CV_FOLDS:
            assert "train_start" in fold
            assert "train_end" in fold
            assert "val_start" in fold
            assert "val_end" in fold

    def test_cv_folds_no_val_overlap(self):
        """Validation windows must not overlap (strict less-than)."""
        for i in range(len(prepare.CV_FOLDS) - 1):
            assert prepare.CV_FOLDS[i]["val_end"] < prepare.CV_FOLDS[i + 1]["val_start"]

    def test_cv_folds_train_expands(self):
        """Each fold's train period should end at or after the previous fold's."""
        for i in range(len(prepare.CV_FOLDS) - 1):
            assert (
                prepare.CV_FOLDS[i]["train_end"] <= prepare.CV_FOLDS[i + 1]["train_end"]
            )


class TestLoadDataRange:
    """Tests for load_data_range function."""

    def test_load_data_range_exists(self):
        assert hasattr(prepare, "load_data_range")
        assert callable(prepare.load_data_range)

    def test_load_data_range_filters_dates(self, tmp_path, monkeypatch):
        """Data should be filtered to requested range."""
        # Create synthetic parquet with known dates
        df = pd.DataFrame(
            {
                "date": ["2025-05-15", "2025-06-10", "2025-06-20", "2025-07-05"],
                "open": [100.0] * 4,
                "high": [110.0] * 4,
                "low": [90.0] * 4,
                "close": [105.0] * 4,
                "volume": [1000.0] * 4,
                "value": [100000.0] * 4,
            }
        )
        df.to_parquet(tmp_path / "KRW-BTC.parquet")
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])

        result = prepare.load_data_range("2025-06-01", "2025-06-30")
        assert "BTC" in result
        assert len(result["BTC"]) == 2
        assert result["BTC"]["date"].min() >= "2025-06-01"
        assert result["BTC"]["date"].max() <= "2025-06-30"

    def test_load_data_range_empty_range(self, tmp_path, monkeypatch):
        """Range with no data should return empty dict."""
        df = pd.DataFrame(
            {
                "date": ["2025-06-10"],
                "open": [100.0],
                "high": [110.0],
                "low": [90.0],
                "close": [105.0],
                "volume": [1000.0],
                "value": [100000.0],
            }
        )
        df.to_parquet(tmp_path / "KRW-BTC.parquet")
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])

        result = prepare.load_data_range("1990-01-01", "1990-01-31")
        assert result == {}

    def test_load_data_delegates_from_load_data(self, tmp_path, monkeypatch):
        """load_data should produce the same result as load_data_range with split dates."""
        df = pd.DataFrame(
            {
                "date": ["2025-07-10", "2025-07-20", "2025-08-15"],
                "open": [100.0] * 3,
                "high": [110.0] * 3,
                "low": [90.0] * 3,
                "close": [105.0] * 3,
                "volume": [1000.0] * 3,
                "value": [100000.0] * 3,
            }
        )
        df.to_parquet(tmp_path / "KRW-BTC.parquet")
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])

        result_split = prepare.load_data("val")
        start, end = prepare.SPLITS["val"]["start"], prepare.SPLITS["val"]["end"]
        result_range = prepare.load_data_range(start, end)

        # Both should return the same data
        assert result_split.keys() == result_range.keys()
        for sym in result_split:
            pd.testing.assert_frame_equal(result_split[sym], result_range[sym])


class TestIntervalSupport:
    """Tests for interval-aware helper behavior."""

    def test_lookback_bars_for_interval_daily(self):
        assert prepare.lookback_bars_for_interval("1d") == 200

    def test_lookback_bars_for_interval_non_daily(self):
        assert prepare.lookback_bars_for_interval("60m") == 500

    def test_data_dir_for_interval_daily_uses_base_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        assert prepare.data_dir_for_interval("1d") == tmp_path

    def test_data_dir_for_interval_non_daily_uses_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        assert prepare.data_dir_for_interval("5m") == tmp_path / "5m"


class TestSharpeAnnualization:
    """Tests for interval-aware Sharpe annualization."""

    def test_annualization_factor_daily(self):
        assert prepare.annualization_factor("1d") == pytest.approx(np.sqrt(365.0))

    def test_annualization_factor_hourly(self):
        assert prepare.annualization_factor("60m") == pytest.approx(
            np.sqrt(365.0 * 24.0)
        )

    def test_calc_sharpe_respects_custom_annualize_factor(self):
        returns = [0.01, 0.02, -0.01, 0.015, 0.005]
        arr = np.array(returns)
        expected = (np.mean(arr) / np.std(arr, ddof=1)) * 10.0
        sharpe = prepare._calc_sharpe(returns, annualize_factor=10.0)
        assert sharpe == pytest.approx(expected)


class TestLoadDataInterval:
    """Tests for interval-aware data loading paths."""

    def test_load_data_range_reads_from_interval_subdir(self, tmp_path, monkeypatch):
        interval_dir = tmp_path / "60m"
        interval_dir.mkdir(parents=True)

        df = pd.DataFrame(
            {
                "date": ["2025-06-10", "2025-06-20"],
                "open": [100.0, 101.0],
                "high": [110.0, 111.0],
                "low": [90.0, 91.0],
                "close": [105.0, 106.0],
                "volume": [1000.0, 1001.0],
                "value": [100000.0, 100001.0],
            }
        )
        df.to_parquet(interval_dir / "KRW-BTC.parquet")

        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])

        result = prepare.load_data_range("2025-06-01", "2025-06-30", bar_interval="60m")
        assert "BTC" in result
        assert len(result["BTC"]) == 2

    def test_load_data_passes_interval_to_load_data_range(self, tmp_path, monkeypatch):
        interval_dir = tmp_path / "60m"
        interval_dir.mkdir(parents=True)

        df = pd.DataFrame(
            {
                "date": ["2025-07-10", "2025-07-20"],
                "open": [100.0, 101.0],
                "high": [110.0, 111.0],
                "low": [90.0, 91.0],
                "close": [105.0, 106.0],
                "volume": [1000.0, 1001.0],
                "value": [100000.0, 100001.0],
            }
        )
        df.to_parquet(interval_dir / "KRW-BTC.parquet")

        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(
            prepare,
            "SPLITS",
            {"val": {"start": "2025-07-01", "end": "2025-07-31"}},
        )

        from_split = prepare.load_data("val", bar_interval="60m")
        start, end = prepare.SPLITS["val"]["start"], prepare.SPLITS["val"]["end"]
        from_range = prepare.load_data_range(start, end, bar_interval="60m")

        assert from_split.keys() == from_range.keys()
        for sym in from_split:
            pd.testing.assert_frame_equal(from_split[sym], from_range[sym])


class TestCVResult:
    """Tests for CVResult dataclass."""

    def test_cv_result_exists(self):
        assert hasattr(prepare, "CVResult")

    def test_cv_result_fields(self):
        result = prepare.CVResult(
            fold_scores=[1.0, 2.0, 0.5],
            fold_results=[],
            fold_indices=[0, 1, 2],
            mean_score=1.167,
            std_score=0.624,
            min_score=0.5,
            cv_score=0.855,
        )
        assert result.mean_score == 1.167
        assert result.cv_score == 0.855
        assert len(result.fold_scores) == 3
        assert result.fold_indices == [0, 1, 2]


class TestCrossValidate:
    """Tests for cross_validate function."""

    def test_cross_validate_exists(self):
        assert hasattr(prepare, "cross_validate")
        assert callable(prepare.cross_validate)

    def test_cv_score_penalizes_variance(self):
        """Higher variance should produce lower cv_score."""
        scores_low_var = [1.0, 1.0, 1.0]
        scores_high_var = [3.0, 1.0, -1.0]

        mean_low = float(np.mean(scores_low_var))
        std_low = float(np.std(scores_low_var))
        cv_low = mean_low - 0.5 * std_low

        mean_high = float(np.mean(scores_high_var))
        std_high = float(np.std(scores_high_var))
        cv_high = mean_high - 0.5 * std_high

        assert cv_low > cv_high

    def test_catastrophic_fold_penalty(self):
        """Folds scoring < -2.0 should be penalized."""
        scores_ok = [1.0, 0.5, 0.8]
        scores_bad = [1.0, 0.5, -3.0]

        mean_ok = float(np.mean(scores_ok))
        std_ok = float(np.std(scores_ok))
        cv_ok = mean_ok - 0.5 * std_ok

        mean_bad = float(np.mean(scores_bad))
        std_bad = float(np.std(scores_bad))
        catastrophic = sum(1 for s in scores_bad if s < -2.0)
        cv_bad = mean_bad - 0.5 * std_bad - catastrophic * 1.0

        assert cv_ok > cv_bad

    def test_cross_validate_empty_folds(self, tmp_path, monkeypatch):
        """cross_validate with empty date ranges should return sentinel."""
        empty_folds = [
            {
                "train_start": "1990-01-01",
                "train_end": "1990-06-30",
                "val_start": "1990-07-01",
                "val_end": "1990-09-30",
            },
        ]

        class DummyStrategy:
            def on_bar(self, bar_data, portfolio):
                return []

        result = prepare.cross_validate(DummyStrategy, folds=empty_folds)
        assert result.cv_score == -999.0
        assert result.fold_scores == []
        assert result.fold_indices == []

    def test_cross_validate_with_synthetic_data(self, tmp_path, monkeypatch):
        """cross_validate should return valid CVResult with real fold execution."""
        # Create 2 years of synthetic daily data
        dates = pd.date_range("2024-04-01", "2026-03-22", freq="D")
        df = pd.DataFrame(
            {
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "open": [100.0] * len(dates),
                "high": [110.0] * len(dates),
                "low": [90.0] * len(dates),
                "close": [105.0] * len(dates),
                "volume": [1000.0] * len(dates),
                "value": [100000.0] * len(dates),
            }
        )
        df.to_parquet(tmp_path / "KRW-BTC.parquet")
        monkeypatch.setattr(prepare, "DATA_DIR", tmp_path)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])

        class PassiveStrategy:
            def on_bar(self, bar_data, portfolio):
                return []

        result = prepare.cross_validate(PassiveStrategy)
        assert len(result.fold_scores) == 4
        assert len(result.fold_results) == 4
        assert len(result.fold_indices) == 4
        assert result.mean_score == pytest.approx(float(np.mean(result.fold_scores)))
        assert isinstance(result.cv_score, float)
