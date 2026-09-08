"""Proof that nothing in the event gate reads a bar after the decision bar.

Method: take a real-shaped random frame, run the scan, then for every event
truncate the frame *at* the event bar, pad it with bars that are deliberately
absurd (a 10x crash, then a 10x melt-up) and re-derive the gate fields.  If a
gate field changed, the gate read the future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.underwater_spike_trim_study.corpora import SymbolBars
from research.underwater_spike_trim_study.events import scan_symbol
from research.underwater_spike_trim_study.levels import compute_levels, rsi_series
from research.underwater_spike_trim_study.spec import LEVEL_WINDOW


def _bars(seed: int, rows: int = 700) -> SymbolBars:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.001, 0.05, rows)
    # inject spikes so the +12% / RSI>=75 gate actually fires
    steps[rng.choice(rows, size=rows // 25, replace=False)] += 0.18
    close = 1000 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0, 0.02, rows)) * close
    frame = pd.DataFrame(
        {
            "session": pd.date_range("2018-01-01", periods=rows, freq="D"),
            "open": close * (1 + rng.normal(0, 0.001, rows)),
            "high": close + spread,
            "low": np.maximum(close - spread, close * 0.5),
            "close": close,
            "volume": rng.integers(1, 10_000, rows).astype(float),
        }
    )
    frame["contiguous_prev"] = True
    frame.loc[0, "contiguous_prev"] = False
    frame["limit_locked"] = 0
    return SymbolBars("test", f"SYN-{seed}", frame, 0, 0, group="syn", segment="syn")


def _poison(frame: pd.DataFrame, upto: int) -> pd.DataFrame:
    """Bars 0..upto kept verbatim; everything after replaced by nonsense."""
    head = frame.iloc[: upto + 1].copy()
    tail = frame.iloc[upto + 1 :].copy()
    if not tail.empty:
        factor = np.where(np.arange(len(tail)) % 2 == 0, 0.1, 10.0)
        for column in ("open", "high", "low", "close"):
            tail[column] = tail[column].to_numpy() * factor
        tail["volume"] = 1.0
    return pd.concat([head, tail], ignore_index=True)


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_gate_fields_are_invariant_to_the_future(seed: int):
    bars = _bars(seed)
    result = scan_symbol(bars)
    events = [o for o in result.observations if o.kind == "event"]
    assert events, "fixture must produce at least one event"

    for observation in events:
        i = observation.index
        poisoned = _poison(bars.frame, i)
        closes = poisoned["close"]
        ret = float(closes.iloc[i] / closes.iloc[i - 1] - 1.0)
        rsi = float(rsi_series(closes).iloc[i])
        view = compute_levels(poisoned.iloc[i - LEVEL_WINDOW + 1 : i + 1])

        assert ret == pytest.approx(observation.ret_24h)
        assert rsi == pytest.approx(observation.rsi)
        assert view.resistance_count == observation.resistance_count
        assert view.named_resistance_count == observation.named_resistance_count
        if observation.rebuy_price is None:
            assert view.nearest_support("strong") is None
        else:
            assert view.nearest_support("strong") == pytest.approx(
                observation.rebuy_price
            )


def test_control_sampling_is_reproducible():
    first = scan_symbol(_bars(21))
    second = scan_symbol(_bars(21))
    assert [(o.kind, o.index) for o in first.observations] == [
        (o.kind, o.index) for o in second.observations
    ]


def test_controls_are_never_events():
    result = scan_symbol(_bars(22))
    controls = [o for o in result.observations if o.kind == "control"]
    assert controls
    # a control fails the cheap gate outright, so it can never be an event
    assert all(o.ret_24h < 0.12 or o.rsi < 75.0 for o in controls)


def test_forward_window_never_reaches_past_the_frame():
    bars = _bars(23)
    result = scan_symbol(bars)
    last = len(bars.frame) - 1
    for observation in result.observations:
        for block in observation.forward.values():
            assert observation.index + block["horizon"] + 1 <= last
