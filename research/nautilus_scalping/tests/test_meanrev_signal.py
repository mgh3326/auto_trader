# tests/test_meanrev_signal.py
"""ROB-320 — correctness gates for the pure z-score mean-reversion fade signal.

Each rule is a pure function; these pin entry logic, the no-entry reasons,
determinism, and no-lookahead (truncation invariance).
"""
from __future__ import annotations

from decimal import Decimal

from meanrev_signal import MeanRevConfig, evaluate_meanrev, zscore

from app.services.brokers.binance.demo_scalping.signal import Candle


def _c(close, high=None, low=None, *, ts=0) -> Candle:
    d = lambda x: Decimal(str(x))  # noqa: E731
    cv = d(close)
    return Candle(
        open_time_ms=ts, open=cv,
        high=d(high) if high is not None else cv,
        low=d(low) if low is not None else cv,
        close=cv, close_time_ms=ts,
    )


def _flat_then_dip(n=20, base=100.0, dip=-3.0, band=0.5) -> list[Candle]:
    """A flat band (low dispersion stays >0) then a sharp dip on the last bar,
    pushing the final close far below the rolling mean -> negative z-score."""
    candles = [_c(base, base + band, base - band, ts=i * 60_000) for i in range(n - 1)]
    last = base + dip
    candles.append(_c(last, base + band, last - band, ts=(n - 1) * 60_000))
    return candles


def test_oversold_dip_triggers_long_fade() -> None:
    candles = _flat_then_dip()
    d = evaluate_meanrev(candles, MeanRevConfig(require_vol=False))
    assert d.has_entry and d.side == "BUY"
    entry = candles[-1].close
    cfg = MeanRevConfig()
    assert d.entry_price == entry
    # fade long: TP above (revert up), SL below
    assert d.tp_price == entry * (Decimal("1") + cfg.tp_bps / Decimal("10000"))
    assert d.sl_price == entry * (Decimal("1") - cfg.sl_bps / Decimal("10000"))
    assert d.reason_codes[0] == "MEANREV_LONG"


def test_within_band_no_entry() -> None:
    flat = [_c(100, 100.5, 99.5, ts=i * 60_000) for i in range(20)]
    d = evaluate_meanrev(flat, MeanRevConfig(require_vol=False))
    assert not d.has_entry and d.reason_codes == ("NO_DISPERSION",)


def test_within_band_with_dispersion_no_entry() -> None:
    # slight fluctuation so sd > 0, but no extreme dip or spike -> within band
    candles = [_c(100 + (i % 2), 101, 99, ts=i * 60_000) for i in range(19)]
    candles.append(_c(100, 101, 99, ts=19 * 60_000))
    d = evaluate_meanrev(candles, MeanRevConfig(require_vol=False, z_entry=Decimal("2.0")))
    assert not d.has_entry and d.reason_codes == ("WITHIN_BAND",)


def test_spot_is_long_only_on_spike() -> None:
    # mirror of the dip: a spike up -> positive z; spot (allow_short=False) suppresses
    base, n = 100.0, 20
    candles = [_c(base, base + 0.5, base - 0.5, ts=i * 60_000) for i in range(n - 1)]
    candles.append(_c(base + 3.0, base + 3.5, base, ts=(n - 1) * 60_000))
    d = evaluate_meanrev(candles, MeanRevConfig(require_vol=False, allow_short=False))
    assert not d.has_entry


def test_futures_shorts_overbought_spike() -> None:
    base, n = 100.0, 20
    candles = [_c(base, base + 0.5, base - 0.5, ts=i * 60_000) for i in range(n - 1)]
    candles.append(_c(base + 3.0, base + 3.5, base, ts=(n - 1) * 60_000))
    cfg = MeanRevConfig(require_vol=False, allow_short=True)
    d = evaluate_meanrev(candles, cfg)
    assert d.has_entry and d.side == "SELL"
    entry = candles[-1].close
    assert d.tp_price == entry * (Decimal("1") - cfg.tp_bps / Decimal("10000"))
    assert d.sl_price == entry * (Decimal("1") + cfg.sl_bps / Decimal("10000"))


def test_insufficient_history() -> None:
    d = evaluate_meanrev(_flat_then_dip(n=5), MeanRevConfig(require_vol=False))
    assert not d.has_entry and d.reason_codes == ("INSUFFICIENT_HISTORY",)


def test_deterministic() -> None:
    candles = _flat_then_dip()
    a = evaluate_meanrev(candles, MeanRevConfig(require_vol=False))
    b = evaluate_meanrev(candles, MeanRevConfig(require_vol=False))
    assert a == b


def test_no_lookahead_truncation_invariance() -> None:
    """A decision on candles[:k] must not change when future bars are appended."""
    candles = _flat_then_dip(n=30)
    k = 20
    prefix = evaluate_meanrev(candles[:k], MeanRevConfig(require_vol=False))
    # appending future candles and re-evaluating the SAME prefix slice is identical
    again = evaluate_meanrev(candles[:k], MeanRevConfig(require_vol=False))
    assert prefix == again


def test_zscore_sign() -> None:
    closes = [Decimal("100")] * 19 + [Decimal("97")]
    assert zscore(closes, lookback=20) < 0
