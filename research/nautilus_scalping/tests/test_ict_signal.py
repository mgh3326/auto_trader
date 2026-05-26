"""ROB-316 — correctness gates for the deterministic ICT-like signal.

Each ICT filter is a pure function; these pin the detection logic and prove the
composite entry gates correctly (and reports the right no-entry reason when a
single filter fails). No-lookahead is structural (only closed candles passed).
"""

from __future__ import annotations

from decimal import Decimal

from app.services.brokers.binance.demo_scalping.signal import Candle
from ict_signal import (
    IctConfig,
    atr_bps,
    evaluate_ict,
    has_bullish_fvg,
    swept_low_reclaim,
)

_HOUR_MS = 3_600_000


def _c(close, high, low, *, ts=0) -> Candle:
    d = lambda x: Decimal(str(x))  # noqa: E731
    return Candle(
        open_time_ms=ts, open=d(close), high=d(high), low=d(low),
        close=d(close), close_time_ms=ts,
    )


def _series(n=27, base=100.0, step=0.1, band=0.3, last_jump=1.5, hour=8):
    """Rising series. ``last_jump`` adds a displacement on the final bar to
    create a bullish FVG + breakout; ``hour`` sets the final bar's UTC hour."""
    candles = []
    for i in range(n):
        close = base + step * i + (last_jump if i == n - 1 else 0.0)
        ts = hour * _HOUR_MS + i * 60_000
        candles.append(_c(close, close + band, close - band, ts=ts))
    return candles


# ---- helper unit tests ----------------------------------------------------
def test_bullish_fvg_detected() -> None:
    # candle[0].high (100.3) < candle[2].low (101.0) -> bullish gap
    candles = [_c(100, 100.3, 99.7), _c(100.6, 101.5, 100.5), _c(101.3, 101.6, 101.0)]
    assert has_bullish_fvg(candles, lookback=1)


def test_bullish_fvg_absent_in_steady_rise() -> None:
    candles = [_c(100 + 0.1 * i, 100 + 0.1 * i + 0.3, 100 + 0.1 * i - 0.3) for i in range(5)]
    assert not has_bullish_fvg(candles, lookback=3)


def test_swept_low_reclaim() -> None:
    prior = [_c(100, 100.3, 99.7) for _ in range(20)]
    sweep = _c(100.5, 100.8, 99.0)  # low 99.0 < prior min 99.7, close 100.5 > 99.7
    assert swept_low_reclaim(prior + [sweep], lookback=20)
    no_sweep = _c(100.1, 100.4, 99.8)  # low 99.8 does not breach 99.7
    assert not swept_low_reclaim(prior + [no_sweep], lookback=20)


def test_atr_bps_positive() -> None:
    candles = [_c(100, 100.5, 99.5) for _ in range(20)]
    # TR ~1.0 on price 100 -> ~100 bps
    assert atr_bps(candles, period=14) > Decimal("90")


# ---- composite entry ------------------------------------------------------
def test_full_setup_triggers_long() -> None:
    d = evaluate_ict(_series(), IctConfig())
    assert d.has_entry and d.side == "BUY"
    entry = _series()[-1].close
    cfg = IctConfig()
    assert d.tp_price == entry * (Decimal("1") + cfg.tp_bps / Decimal("10000"))
    assert d.sl_price == entry * (Decimal("1") - cfg.sl_bps / Decimal("10000"))


def test_out_of_session_blocks() -> None:
    d = evaluate_ict(_series(hour=3), IctConfig())  # 03:00 UTC not a killzone
    assert not d.has_entry and d.reason_codes == ("OUT_OF_SESSION",)


def test_low_volatility_blocks() -> None:
    # no jump (no TR spike) + tiny step/band -> ATR ~1.4 bps < 15 floor
    d = evaluate_ict(_series(step=0.01, band=0.004, last_jump=0.0), IctConfig())
    assert not d.has_entry and d.reason_codes == ("LOW_VOLATILITY",)


def test_no_breakout_blocks() -> None:
    d = evaluate_ict(_series(last_jump=0.0), IctConfig())  # final bar doesn't break out
    assert not d.has_entry and d.reason_codes == ("NO_BREAKOUT",)


def test_breakout_without_fvg_blocks() -> None:
    # jump 0.4 breaks the prior high (>0.3) but is too small for a 3-bar FVG (<0.5)
    d = evaluate_ict(_series(last_jump=0.4), IctConfig())
    assert not d.has_entry and d.reason_codes == ("NO_FVG",)


def test_insufficient_history() -> None:
    d = evaluate_ict(_series(n=10), IctConfig())
    assert not d.has_entry and d.reason_codes == ("INSUFFICIENT_HISTORY",)


def test_session_filter_can_be_disabled() -> None:
    d = evaluate_ict(_series(hour=3), IctConfig(require_session=False))
    assert d.has_entry  # off-session now allowed
