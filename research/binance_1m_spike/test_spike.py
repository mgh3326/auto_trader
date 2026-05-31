"""Pure unit tests for the 1m gross-edge spike (no network, no repo deps).

Run: ``python3 -m pytest test_spike.py`` from this directory, or
``python3 test_spike.py`` for a dependency-free smoke check.
"""

from __future__ import annotations

import fees
from meanrev_probe import (
    MIN_TRADES,
    ProbeParams,
    _rolling_z,
    _verdict,
    probe_symbol,
    trade_records,
)


def test_fee_envelope_matches_frozen_config():
    # Mirrors research/nautilus_scalping/frozen_config.py (taker 4 / maker 2 / floor 0.5).
    assert fees.TAKER_BPS_PER_LEG == 4.0
    assert fees.MAKER_BPS_PER_LEG == 2.0
    assert fees.ECONOMIC_TRIVIALITY_FLOOR_BPS == 0.5
    assert fees.TAKER_ROUND_TRIP_BPS == 8.0
    assert fees.MAKER_ROUND_TRIP_BPS == 4.0


def test_net_bps_subtracts_round_trip():
    assert fees.net_bps(10.0, round_trip_bps=8.0) == 2.0
    assert fees.net_bps(3.0, round_trip_bps=8.0) == -5.0


def test_rolling_z_warmup_is_none():
    z = _rolling_z([1.0] * 10, lookback=5)
    assert z[:5] == [None] * 5
    # flat series -> zero stdev -> None (no signal)
    assert all(v is None for v in z)


def test_nonoverlapping_trades_do_not_double_count():
    # V-shape: a steady decline then recovery guarantees z crossings both ways,
    # so multiple fade trades fire. The key property under test is that no two
    # trades overlap (each entry is more than `hold` bars after the previous).
    closes = [100.0 - i for i in range(40)] + [60.0 + i for i in range(40)]
    z = _rolling_z(closes, lookback=20)
    params = ProbeParams(lookback=20, z_entry=1.5, hold=5)
    records = trade_records(closes, z, params)
    assert len(records) >= 1
    entries = [entry for (entry, _exit, _ret) in records]
    # Index iteration over consecutive pairs (no zip): avoids the B905
    # explicit-strict rule and keeps the standalone runner working on the
    # system's older python3.
    for k in range(len(entries) - 1):
        assert entries[k + 1] - entries[k] > params.hold  # non-overlapping
    # entry/exit indices are consistent with the hold horizon
    for entry, exit_i, _ret in records:
        assert exit_i == entry + params.hold


def test_verdict_needs_more_data_on_tiny_sample():
    r = probe_symbol(
        "TESTUSDT",
        [100.0, 101.0, 102.0],
        demo_executable=True,
        params=ProbeParams(),
    )
    assert r.n_trades == 0
    assert r.verdict == "needs_more_data"


def test_verdict_screens_out_at_or_below_floor_else_promotes():
    # Deterministic check of the screening rule itself (no data dependence).
    # With enough trades, the floor (0.5 bps, inclusive) decides screened_out vs
    # promote; below MIN_TRADES it is always needs_more_data.
    assert _verdict(100, 0.4) == "screened_out_gross"  # below floor
    assert (
        _verdict(100, fees.ECONOMIC_TRIVIALITY_FLOOR_BPS) == "screened_out_gross"
    )  # AT floor (<=)
    assert (
        _verdict(100, 0.6) == "gross_edge_present_needs_full_validation"
    )  # above floor
    assert _verdict(MIN_TRADES - 1, 5.0) == "needs_more_data"  # too few trades wins
    assert (
        _verdict(MIN_TRADES, 0.4) == "screened_out_gross"
    )  # at threshold, floor applies


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
