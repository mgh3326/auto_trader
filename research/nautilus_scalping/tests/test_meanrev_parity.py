# tests/test_meanrev_parity.py
"""ROB-320 — parity gates for the mean-reversion candidate.

Pure-signal parity (no Nautilus): the SAME pure ``evaluate_meanrev`` the
backtest strategy calls is exercised directly, pinning the windowing + decimal
handling. The Nautilus bar-adaptation parity reuses ``bar_to_candle`` and is
skipped if the research venv (nautilus_trader) is unavailable — documented as a
parity limitation per the ROB-320 acceptance criterion.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from meanrev_signal import MeanRevConfig, evaluate_meanrev, required_bars

from app.services.brokers.binance.demo_scalping.signal import Candle

nautilus = pytest.importorskip("nautilus_trader", reason="research venv not installed")
from nautilus_trader.model.data import Bar, BarType  # noqa: E402
from nautilus_trader.model.objects import Price, Quantity  # noqa: E402
from signal_bridge import bar_to_candle  # noqa: E402

_BAR_TYPE = BarType.from_str("XRPUSDT.BINANCE-1-MINUTE-LAST-INTERNAL")


def _bar(close: float, high: float, low: float, ts_ns: int) -> Bar:
    return Bar(_BAR_TYPE, Price(close, 4), Price(high, 4), Price(low, 4),
               Price(close, 4), Quantity(100, 1), ts_event=ts_ns, ts_init=ts_ns)


def test_bar_adaptation_feeds_same_decision() -> None:
    # build a flat-then-dip bar series; decision via bars == decision via Candles
    bars = [_bar(100.0, 100.5, 99.5, i * 60_000_000_000) for i in range(19)]
    bars.append(_bar(97.0, 100.0, 96.5, 19 * 60_000_000_000))
    candles_from_bars = [bar_to_candle(b) for b in bars]
    candles_direct = [
        Candle(open_time_ms=i * 60_000, open=Decimal("100"), high=Decimal("100.5"),
               low=Decimal("99.5"), close=Decimal("100"), close_time_ms=i * 60_000)
        for i in range(19)
    ] + [Candle(open_time_ms=19 * 60_000, open=Decimal("97"), high=Decimal("100"),
                low=Decimal("96.5"), close=Decimal("97"), close_time_ms=19 * 60_000)]
    cfg = MeanRevConfig(require_vol=False)
    assert evaluate_meanrev(candles_from_bars, cfg) == evaluate_meanrev(candles_direct, cfg)


def test_required_bars_matches_config() -> None:
    cfg = MeanRevConfig(lookback=30, atr_period=14)
    assert required_bars(cfg) == 30
