"""ROB-351 (eng-review Issue 6 + Codex as-of-rebalance PIT) — lazy PIT cross-sections.

Cross-sectional momentum needs, at EACH rebalance, the lookback return of every
symbol tradeable AS OF that rebalance. The generator yields one rebalance at a
time (memory-bounded, no full time x symbol matrix) and consults the PIT manifest
per rebalance so delisted/unlisted symbols never leak in.
"""

import panel
from pit_universe import PITManifest


def _closes():
    # ts -> close per symbol; AAA & BBB present early, CCC lists late
    return {
        "AAA": [(0, 100.0), (10, 110.0), (20, 121.0), (30, 133.0)],
        "BBB": [(0, 50.0), (10, 49.0), (20, 48.0), (30, 47.0)],
        "CCC": [(20, 10.0), (30, 12.0)],
    }


def test_iter_is_lazy_generator():
    import types
    g = panel.iter_rebalance_cross_sections(_closes(), rebalances=[20, 30], lookback=10)
    assert isinstance(g, types.GeneratorType)


def test_cross_section_lookback_return():
    out = dict(panel.iter_rebalance_cross_sections(_closes(), rebalances=[20], lookback=10))
    # at ts=20, lookback 10: AAA 121/110-1=0.10, BBB 48/49-1<0
    xs = out[20]
    assert abs(xs["AAA"] - (121.0 / 110.0 - 1.0)) < 1e-9
    assert xs["BBB"] < 0.0


def test_pit_excludes_unlisted_symbol():
    m = PITManifest.from_records([
        {"symbol": "AAA", "listed_from": 0, "delisted_at": None},
        {"symbol": "BBB", "listed_from": 0, "delisted_at": None},
        {"symbol": "CCC", "listed_from": 25, "delisted_at": None},  # lists after ts=20
    ])
    out = dict(panel.iter_rebalance_cross_sections(
        _closes(), rebalances=[20], lookback=10, manifest=m))
    assert "CCC" not in out[20]   # unlisted as-of ts=20 -> leak guard
    assert "AAA" in out[20]


def test_symbol_without_enough_lookback_history_skipped():
    # CCC has no bar at ts<=10, so a lookback-10 return at ts=20 is impossible
    out = dict(panel.iter_rebalance_cross_sections(_closes(), rebalances=[20], lookback=10))
    assert "CCC" not in out[20]
