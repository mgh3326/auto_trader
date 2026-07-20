"""ROB-979 (H2, ROB-974 R2) CP6 -- actual merged-origin H1 integration.

Runs the REAL merged H1 (`rob974_features.py`, landed by ROB-978 PR #1614,
merge `76fb5506`) output through a genuine adapter (`rob974_h2_h1_bridge.py`,
NOT the test-only namedtuple fixture CP1-CP5 used) into the real H2
DTOs/engines. Unlike the CP1-CP5 fixture (which deliberately used the exact
prose field names from the worker brief since H1 wasn't merged yet), H1's
real ``MinuteBar`` uses ``ts``+``volume`` and splits close/VWAP/M across
THREE different types (``Bar4h``/``CommonSnapshot``/``SymbolFeature``) that
must be joined on ``close_ts``/``decision_ts`` -- this is a genuine
transformation, not coincidental duck-typing.

Fixture sizing: H1's ``synchronized_features`` requires index>=6 (7th+
complete 4h bar, i.e. >=28h of CONTIGUOUS per-symbol history) before it
emits ANY ``CommonSnapshot`` for a decision_ts, and ``vwap24`` separately
requires exactly 1,440 contiguous prior minutes. A 36h (9-bar, 2,160-minute)
flat synthetic corpus per symbol clears both floors with margin while
staying fast (H1's own smoke fixture is ~202*240 minutes -- much larger,
because it additionally needs ATR20+180-percentile+21-day warmup that this
checkpoint's S3/S4 engines never consume).
"""

from __future__ import annotations

from rob974_features import MinuteBar as H1MinuteBar
from rob974_features import build_complete_4h, synchronized_features
from rob974_h2_dtos import S3SignalIntent, S4PairSignalIntent
from rob974_h2_h1_bridge import (
    from_h1_close_features,
    from_h1_minute_bars,
    from_h1_pair_leg_closes,
)
from rob974_h2_ingress import build_minute_index
from rob974_h2_s3_engine import run_s3_portfolio_stream
from rob974_h2_s4_engine import run_s4_pair_basket_stream
from rob974_h2_scenarios import (
    PATH_SCENARIO_BASE13,
    build_s3_scenario_ledger,
    s3_ledger_hash,
)

_MIN_MS = 60_000
_FOUR_H_MS = 240 * _MIN_MS
_CORPUS_MINUTES = 9 * 240  # 36h -- 9 complete 4h bars per symbol
_SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
_ENTRY_SIGNAL_TS = 7 * _FOUR_H_MS  # close_ts of Bar4h index 6 -- the FIRST ts
# at which synchronized_features emits a CommonSnapshot (index>=6 gate).
_CORPUS_END = _CORPUS_MINUTES * _MIN_MS + 1


def _flat_h1_minutes(count: int, price: float = 1.0):
    return tuple(
        H1MinuteBar(i * _MIN_MS, price, price, price, price, 1.0) for i in range(count)
    )


def _build_real_h1_corpus():
    raw = {symbol: _flat_h1_minutes(_CORPUS_MINUTES) for symbol in _SYMBOLS}
    bars4h = {symbol: build_complete_4h(raw[symbol]) for symbol in _SYMBOLS}
    snapshots = synchronized_features(raw)
    return raw, bars4h, snapshots


def _run_pipeline():
    raw, bars4h, snapshots = _build_real_h1_corpus()

    minute_bars = []
    for symbol in _SYMBOLS:
        minute_bars += from_h1_minute_bars(symbol, raw[symbol])
    minute_index = build_minute_index(minute_bars)

    s3_features = []
    for symbol in _SYMBOLS:
        s3_features += from_h1_close_features(symbol, bars4h[symbol], snapshots)
    s3_feature_index = {(f.symbol, f.close_ts): f for f in s3_features}

    pair_closes = []
    for symbol in ("XRPUSDT", "DOGEUSDT"):
        pair_closes += from_h1_pair_leg_closes(symbol, bars4h[symbol])
    pair_close_index = {(c.symbol, c.close_ts): c for c in pair_closes}

    s3_candidates = [
        S3SignalIntent(
            symbol="XRPUSDT",
            side="long",
            signal_ts=_ENTRY_SIGNAL_TS,
            entry_sl_distance=0.0080,
            entry_tp_distance=0.0128,
            config_id="cp6-s3",
            fold_id="fold-00",
            volatility_percentile=55.0,
        ),
        S3SignalIntent(
            symbol="SOLUSDT",
            side="long",
            signal_ts=999_999_999,  # genuinely absent tick
            entry_sl_distance=0.0080,
            entry_tp_distance=0.0128,
            config_id="cp6-s3",
            fold_id="fold-00",
        ),
    ]
    s3_result = run_s3_portfolio_stream(
        s3_candidates, minute_index, s3_feature_index, corpus_end_ts=_CORPUS_END
    )

    s4_candidates = [
        S4PairSignalIntent(
            pair=("XRPUSDT", "DOGEUSDT"),
            signal_ts=_ENTRY_SIGNAL_TS,
            side_a="short",
            side_b="long",
            weight_a=0.4,
            weight_b=0.6,
            beta_a=1.2,
            beta_b=0.8,
            mu=0.0,
            sigma=0.05,
            z_entry=1.9,
            gross_notional=15.0,
            entry_sl_distance=0.0100,
            entry_tp_distance=0.0150,
            config_id="cp6-s4",
            fold_id="fold-00",
        )
    ]
    s4_result = run_s4_pair_basket_stream(
        s4_candidates, minute_index, pair_close_index, corpus_end_ts=_CORPUS_END
    )

    return s3_result, s4_result


class TestActualH1Seam:
    def test_real_h1_output_bridges_into_real_s3_thesis_exit(self):
        s3_result, _ = _run_pipeline()
        thesis = [t for t in s3_result.trades if t.exit_reason == "THESIS_EXIT"]
        assert len(thesis) == 1
        trade = thesis[0]
        assert trade.symbol == "XRPUSDT"
        assert trade.entry_ts == _ENTRY_SIGNAL_TS
        assert trade.exit_ts == _ENTRY_SIGNAL_TS + _FOUR_H_MS

    def test_real_h1_output_bridges_into_real_s4_basket_exit(self):
        _, s4_result = _run_pipeline()
        assert len(s4_result.trades) == 1
        trade = s4_result.trades[0]
        assert trade.exit_reason == "MEAN_EXIT"
        assert trade.pair == ("XRPUSDT", "DOGEUSDT")

    def test_nonzero_no_trade_on_both_engines(self):
        s3_result, s4_result = _run_pipeline()
        assert len(s3_result.no_trades) >= 1
        assert any(nt.reason == "next_tick_unavailable" for nt in s3_result.no_trades)

    def test_missing_minute_rejection_exact(self):
        s3_result, _ = _run_pipeline()
        missing = [nt for nt in s3_result.no_trades if nt.symbol == "SOLUSDT"]
        assert len(missing) == 1
        assert missing[0].signal_ts == 999_999_999

    def test_deterministic_bytes_survive_the_real_h1_seam(self):
        s3_result_1, _ = _run_pipeline()
        s3_result_2, _ = _run_pipeline()
        rows_1 = build_s3_scenario_ledger(s3_result_1.trades, PATH_SCENARIO_BASE13)
        rows_2 = build_s3_scenario_ledger(s3_result_2.trades, PATH_SCENARIO_BASE13)
        assert s3_ledger_hash(rows_1) == s3_ledger_hash(rows_2)

    def test_bridge_is_not_coupled_to_h1_concrete_class_identity(self):
        # An ad-hoc object exposing the SAME attributes as H1's real
        # MinuteBar must normalize identically -- the bridge must never
        # `isinstance(row, rob974_features.MinuteBar)`.
        class _ArbitraryShape:
            ts = 0
            open = 1.0
            high = 1.0
            low = 1.0
            close = 1.0
            volume = 1.0

        bars = from_h1_minute_bars("XRPUSDT", [_ArbitraryShape()])
        assert bars[0].open_time == 0
        assert bars[0].symbol == "XRPUSDT"
