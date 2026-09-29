from __future__ import annotations

import datetime as dt
from dataclasses import asdict

from app.services.brokers.binance.h5.strategy import (
    H5Bar4h,
    H5Strategy,
    make_signal_key,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, Bar4h


def _bars(
    *, low: float, high: float, prior_close: float, current_close: float
) -> tuple[Bar4h, ...]:
    result = [
        Bar4h(
            ts=i * FOUR_HOUR_MS,
            close_ts=(i + 1) * FOUR_HOUR_MS,
            open=prior_close,
            high=high,
            low=low,
            close=prior_close,
            volume=1.0,
            is_segment_start=i == 0,
        )
        for i in range(20)
    ]
    result.append(
        Bar4h(
            ts=20 * FOUR_HOUR_MS,
            close_ts=21 * FOUR_HOUR_MS,
            open=prior_close,
            high=max(prior_close, current_close),
            low=min(prior_close, current_close),
            close=current_close,
            volume=1.0,
            is_segment_start=False,
        )
    )
    return tuple(result)


def test_long_formula_equality_and_strict_previous_high() -> None:
    plugin = H5Strategy()
    decision_ts = 21 * FOUR_HOUR_MS
    at_boundary = _bars(low=100.0, high=104.0, prior_close=102.0, current_close=105.0)
    signal = plugin.evaluate({"BTCUSDT": at_boundary}, decision_ts=decision_ts)
    assert signal is not None
    assert signal.side == "BUY"
    assert signal.symbol == "BTCUSDT"
    assert (
        plugin.evaluate(
            {
                "BTCUSDT": _bars(
                    low=100.0, high=104.0, prior_close=102.0, current_close=105.01
                )
            },
            decision_ts=decision_ts,
        )
        is None
    )
    assert (
        plugin.evaluate(
            {
                "BTCUSDT": _bars(
                    low=100.0, high=105.0, prior_close=102.0, current_close=105.0
                )
            },
            decision_ts=decision_ts,
        )
        is None
    )


def test_short_formula_equality_and_strict_previous_low() -> None:
    plugin = H5Strategy()
    decision_ts = 21 * FOUR_HOUR_MS
    signal = plugin.evaluate(
        {"ETHUSDT": _bars(low=96.0, high=100.0, prior_close=98.0, current_close=95.0)},
        decision_ts=decision_ts,
    )
    assert signal is not None and signal.side == "SELL"
    assert (
        plugin.evaluate(
            {
                "ETHUSDT": _bars(
                    low=96.0, high=100.0, prior_close=98.0, current_close=94.99
                )
            },
            decision_ts=decision_ts,
        )
        is None
    )
    assert (
        plugin.evaluate(
            {
                "ETHUSDT": _bars(
                    low=96.0, high=100.0, prior_close=98.0, current_close=96.0
                )
            },
            decision_ts=decision_ts,
        )
        is None
    )


def test_incomplete_or_stale_segment_has_no_signal() -> None:
    plugin = H5Strategy()
    bars = _bars(low=100.0, high=104.0, prior_close=102.0, current_close=105.0)
    assert (
        plugin.evaluate({"BTCUSDT": bars[:-1]}, decision_ts=21 * FOUR_HOUR_MS) is None
    )
    gap = list(bars)
    gap[12] = Bar4h(
        ts=gap[12].ts,
        close_ts=gap[12].close_ts,
        open=gap[12].open,
        high=gap[12].high,
        low=gap[12].low,
        close=gap[12].close,
        volume=gap[12].volume,
        is_segment_start=True,
    )
    assert (
        plugin.evaluate({"BTCUSDT": tuple(gap)}, decision_ts=21 * FOUR_HOUR_MS) is None
    )


def test_signal_key_preserves_decimal_text_and_kst_minute() -> None:
    instant = dt.datetime(2026, 9, 28, 0, 0, tzinfo=dt.UTC)
    key = make_signal_key("SOLUSDT", int(instant.timestamp() * 1000), "BUY", "105.00")
    assert key == "SOLUSDT|2026-09-28 09:00|BUY|105.00"


def test_formula_retains_decimal_boundary_beyond_binary_float_precision():
    bars = _bars(low=100.0, high=104.0, prior_close=102.0, current_close=105.0)
    exact = [
        H5Bar4h(**asdict(bar), price_text=("102.00", "104.00", "100.00", "102.00"))
        for bar in bars[:-1]
    ]
    exact.append(
        H5Bar4h(
            **asdict(bars[-1]),
            price_text=(
                "102.00",
                "105.000000000000000001",
                "102.00",
                "105.000000000000000001",
            ),
        )
    )
    assert (
        H5Strategy().evaluate({"BTCUSDT": tuple(exact)}, decision_ts=21 * FOUR_HOUR_MS)
        is None
    )


def test_plugin_rejects_non_demo_origin_by_assertion() -> None:
    blocked = False
    try:
        H5Strategy(base_url="https://fapi.binance.com")
    except ValueError:
        blocked = True
    assert blocked is True
