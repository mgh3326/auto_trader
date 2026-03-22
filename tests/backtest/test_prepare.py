"""Tests for backtest prepare module."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare


class TestLoadData:
    """Tests for load_data function."""

    def test_load_data_filters_symbols_and_dates(self, tmp_path, monkeypatch):
        """Test that load_data reads only DEFAULT_SYMBOLS and applies split dates."""
        # Create test parquet files
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create BTC data (in range)
        btc_df = pd.DataFrame({
            "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [105.0, 106.0, 107.0, 108.0],
            "low": [95.0, 96.0, 97.0, 98.0],
            "close": [102.0, 103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200, 1300],
            "value": [100000, 110000, 120000, 130000],
        })
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        # Create ETH data (in range)
        eth_df = pd.DataFrame({
            "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
            "open": [50.0, 51.0, 52.0, 53.0],
            "high": [55.0, 56.0, 57.0, 58.0],
            "low": [45.0, 46.0, 47.0, 48.0],
            "close": [52.0, 53.0, 54.0, 55.0],
            "volume": [2000, 2100, 2200, 2300],
            "value": [200000, 210000, 220000, 230000],
        })
        eth_df.to_parquet(data_dir / "KRW-ETH.parquet", index=False)

        # Create XRP data (not in DEFAULT_SYMBOLS - should be ignored)
        xrp_df = pd.DataFrame({
            "date": ["2025-04-01", "2025-04-02", "2025-04-03", "2025-04-04"],
            "open": [1.0, 1.1, 1.2, 1.3],
            "high": [1.5, 1.6, 1.7, 1.8],
            "low": [0.5, 0.6, 0.7, 0.8],
            "close": [1.2, 1.3, 1.4, 1.5],
            "volume": [10000, 11000, 12000, 13000],
            "value": [10000, 11000, 12000, 13000],
        })
        xrp_df.to_parquet(data_dir / "KRW-XRP.parquet", index=False)

        # Monkeypatch DATA_DIR
        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC", "ETH"])
        monkeypatch.setattr(prepare, "SPLITS", {
            "val": {"start": "2025-04-01", "end": "2025-04-02"},
        })

        data = prepare.load_data("val")

        assert set(data.keys()) == {"BTC", "ETH"}
        assert data["BTC"]["date"].tolist() == ["2025-04-01", "2025-04-02"]
        assert data["ETH"]["date"].tolist() == ["2025-04-01", "2025-04-02"]

    def test_load_data_returns_sorted_ascending(self, tmp_path, monkeypatch):
        """Test that returned dataframes are sorted in ascending date order."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create BTC data in descending order
        btc_df = pd.DataFrame({
            "date": ["2025-04-05", "2025-04-04", "2025-04-03", "2025-04-02", "2025-04-01"],
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [105.0, 106.0, 107.0, 108.0, 109.0],
            "low": [95.0, 96.0, 97.0, 98.0, 99.0],
            "close": [102.0, 103.0, 104.0, 105.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 1400],
            "value": [100000, 110000, 120000, 130000, 140000],
        })
        btc_df.to_parquet(data_dir / "KRW-BTC.parquet", index=False)

        monkeypatch.setattr(prepare, "DATA_DIR", data_dir)
        monkeypatch.setattr(prepare, "DEFAULT_SYMBOLS", ["BTC"])
        monkeypatch.setattr(prepare, "SPLITS", {
            "val": {"start": "2025-04-01", "end": "2025-04-05"},
        })

        data = prepare.load_data("val")

        dates = data["BTC"]["date"].tolist()
        assert dates == sorted(dates)


class TestExecutionCosts:
    """Tests for execution with slippage and fees."""

    def test_buy_execution_with_slippage_and_fee(self):
        """Test that buy orders include slippage and fee."""
        signal = prepare.Signal(
            symbol="BTC",
            action="buy",
            target_weight=0.5,
        )
        state = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )
        bar_data = {"BTC": prepare.BarData(date="2025-04-01", open=100.0, high=110.0, low=90.0, close=100.0, volume=1000, value=100000)}

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
            target_weight=0.0,
        )
        state = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )
        bar_data = {"BTC": prepare.BarData(date="2025-04-01", open=100.0, high=110.0, low=90.0, close=100.0, volume=1000, value=100000)}

        result = prepare._execute_signal(signal, state, bar_data, 100000.0)

        assert len(result.trade_log) == 1
        trade = result.trade_log[0]
        assert trade["symbol"] == "BTC"
        assert trade["action"] == "sell"
        assert trade["fee"] > 0
        # Price should include slippage (sell at lower price)
        assert trade["price"] < 100.0

    def test_target_weight_buy_sizing(self):
        """Test that buy orders respect target weight."""
        signal = prepare.Signal(
            symbol="BTC",
            action="buy",
            target_weight=0.5,
        )
        state = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )
        bar_data = {"BTC": prepare.BarData(date="2025-04-01", open=100.0, high=110.0, low=90.0, close=100.0, volume=1000, value=100000)}
        initial_value = 100000.0

        result = prepare._execute_signal(signal, state, bar_data, initial_value)

        # Should have bought approximately 50% worth
        trade = result.trade_log[0]
        cost = trade["price"] * trade["quantity"]
        weight = cost / initial_value
        assert pytest.approx(weight, abs=0.05) == 0.5

    def test_partial_sell_sizing(self):
        """Test partial sell sizing."""
        signal = prepare.Signal(
            symbol="BTC",
            action="sell",
            target_weight=0.25,  # Reduce to 25% weight
        )
        state = prepare.PortfolioState(
            cash=100.0,
            positions={"BTC": 2.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )
        bar_data = {"BTC": prepare.BarData(date="2025-04-01", open=100.0, high=110.0, low=90.0, close=100.0, volume=1000, value=100000)}
        initial_value = 300.0  # 100 cash + 2 * 100 BTC value
        # Current weight = 200/300 = 67%, target = 25%

        result = prepare._execute_signal(signal, state, bar_data, initial_value)

        assert len(result.trade_log) == 1
        trade = result.trade_log[0]
        # Should sell to reach 25% weight: target value = 75, current = 200, sell 125 worth = 1.25 BTC
        assert trade["quantity"] > 0.5  # Should sell a significant portion


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
            win_rate=0.5,
            profit_factor=1.5,
            avg_holding_days=3.0,
            trade_log=[],
            equity_curve=[100000.0, 105000.0, 110000.0, 108000.0, 115000.0],
        )

        score = prepare.compute_score(result)

        # Score should be penalized for low trade count
        # The formula likely reduces score significantly for < 10 trades
        assert score < 15.0  # Raw return is 15%, but score should be lower due to penalty


class TestRunBacktest:
    """Tests for run_backtest function."""

    def test_run_backtest_executes_strategy(self):
        """Test that run_backtest properly executes strategy signals."""
        # Create simple mock data
        data = {
            "BTC": pd.DataFrame({
                "date": ["2025-04-01", "2025-04-02", "2025-04-03"],
                "open": [100.0, 105.0, 110.0],
                "high": [106.0, 111.0, 116.0],
                "low": [95.0, 100.0, 105.0],
                "close": [105.0, 110.0, 115.0],
                "volume": [1000, 1100, 1200],
                "value": [100000, 110000, 120000],
            })
        }

        # Create a simple strategy that buys on first day
        class SimpleStrategy:
            def __init__(self):
                self.called = False

            def on_bar(self, date, bar_data, portfolio, i):
                self.called = True
                if i == 0:
                    return [prepare.Signal(symbol="BTC", action="buy", target_weight=0.95)]
                return []

        strategy = SimpleStrategy()
        result = prepare.run_backtest(data, strategy)

        assert strategy.called
        assert len(result.trade_log) >= 1
        assert result.trade_log[0]["action"] == "buy"

    def test_run_backtest_updates_equity_curve(self):
        """Test that equity curve is properly updated."""
        data = {
            "BTC": pd.DataFrame({
                "date": ["2025-04-01", "2025-04-02", "2025-04-03"],
                "open": [100.0, 105.0, 110.0],
                "high": [106.0, 111.0, 116.0],
                "low": [95.0, 100.0, 105.0],
                "close": [105.0, 110.0, 115.0],
                "volume": [1000, 1100, 1200],
                "value": [100000, 110000, 120000],
            })
        }

        class NoOpStrategy:
            def on_bar(self, date, bar_data, portfolio, i):
                return []

        strategy = NoOpStrategy()
        result = prepare.run_backtest(data, strategy)

        assert len(result.equity_curve) == 4  # Initial + 3 days
        assert result.equity_curve[0] == 100000.0  # Initial capital

    def test_run_backtest_handles_missing_symbol_dates(self):
        """Test that missing symbol dates are handled gracefully."""
        data = {
            "BTC": pd.DataFrame({
                "date": ["2025-04-01", "2025-04-02"],
                "open": [100.0, 105.0],
                "high": [106.0, 111.0],
                "low": [95.0, 100.0],
                "close": [105.0, 110.0],
                "volume": [1000, 1100],
                "value": [100000, 110000],
            }),
            "ETH": pd.DataFrame({
                "date": ["2025-04-02"],  # Missing 04-01
                "open": [50.0],
                "high": [55.0],
                "low": [45.0],
                "close": [52.0],
                "volume": [2000],
                "value": [200000],
            })
        }

        class NoOpStrategy:
            def on_bar(self, date, bar_data, portfolio, i):
                return []

        strategy = NoOpStrategy()
        result = prepare.run_backtest(data, strategy)

        # Should complete without error
        assert len(result.equity_curve) > 0
