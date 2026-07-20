"""ROB-979 (H2, ROB-974 R2) CP5 -- frozen-contract fixture path smoke.

Runs the REAL H2 path end to end:

    test-only frozen H1 semantic fixture (duck-typed namedtuples)
    -> rob974_h2_ingress normalizers -> real immutable H2 DTOs
    -> real H3-compatible S3SignalIntent/S4PairSignalIntent (authored
       directly here, since H3 itself is not built in ROB-979 -- H2 owns
       these types per the worker brief)
    -> real rob974_h2_s3_engine / rob974_h2_s4_engine
    -> real rob974_h2_scenarios ledger builders, under TWO independent
       path scenarios

No behavior callback ever substitutes for an engine decision anywhere in
this file -- every exit/no-trade/ledger value below is produced by the
actual engine and scenario code, not asserted-into-existence by a stub.

Per the ROB-979 worker brief, this checkpoint's status line is exactly:

    fixture_contract_smoke=PASS
    actual_h1_integration=NOT_EVALUATED

(ROB-978 is unmerged; CP6 performs actual H1 integration once orch supplies
a verified merge SHA.)
"""

from __future__ import annotations

from collections import namedtuple

from rob974_h2_dtos import S3SignalIntent, S4PairSignalIntent
from rob974_h2_ingress import (
    build_minute_index,
    normalize_minute_bar,
    normalize_s3_close_feature,
    normalize_s4_pair_leg_close,
)
from rob974_h2_s3_engine import FOUR_H_MS, run_s3_portfolio_stream
from rob974_h2_s4_engine import run_s4_pair_basket_stream
from rob974_h2_scenarios import (
    PATH_SCENARIO_BASE13,
    PATH_SCENARIO_PRIMARY_STRESS17,
    build_s3_scenario_ledger,
    build_s4_scenario_ledger,
    s3_ledger_hash,
    s4_ledger_hash,
)

_MIN_MS = 60_000
_CORPUS_END = 10_000_000_000

_RawMinute = namedtuple("_RawMinute", "symbol open_time open high low close")
_RawS3Close = namedtuple("_RawS3Close", "symbol close_ts close VWAP24 M")
_RawPairClose = namedtuple("_RawPairClose", "symbol close_ts close")


def _flat_raw_minutes(symbol, start_ts, count, price=1.0, overrides=None):
    overrides = overrides or {}
    out = []
    for i in range(count):
        ts = start_ts + i * _MIN_MS
        o, h, low, c = overrides.get(i, (price, price, price, price))
        out.append(_RawMinute(symbol, ts, o, h, low, c))
    return out


def _build_fixture():
    """The test-only frozen H1 semantic fixture: raw 1m bars + S3 4h-close
    features + S4 pair-leg 4h closes, in the EXACT field shapes the ROB-979
    worker brief specifies (``open_time/open/high/low/close`` for 1m;
    ``close_ts/symbol/close/VWAP24/M`` for S3; synchronized pair-leg closes
    for S4)."""
    raw_minutes = []

    # XRPUSDT: flat through the first 4h boundary (S3 long thesis-exit entry
    # AND S4 candidate-1 leg a), then a favorable gap for S4 candidate-2.
    raw_minutes += _flat_raw_minutes("XRPUSDT", 0, 241, price=1.0)
    raw_minutes += [_RawMinute("XRPUSDT", FOUR_H_MS + _MIN_MS, 1.05, 1.05, 1.05, 1.05)]

    # DOGEUSDT: flat through the first 4h boundary (S4 candidate-1 leg b),
    # then an adverse move for S4 candidate-2 (long a / short b this time,
    # so DOGE moving DOWN is what makes leg b -- now short -- gap-TP).
    raw_minutes += _flat_raw_minutes("DOGEUSDT", 0, 241, price=1.0)
    raw_minutes += [_RawMinute("DOGEUSDT", FOUR_H_MS + _MIN_MS, 0.6, 0.6, 0.6, 0.6)]

    # SOLUSDT: entry at the first boundary for the S3 SHORT candidate
    # (same-tick arbitration with XRP's THESIS_EXIT), quick favorable gap TP.
    raw_minutes += [
        _RawMinute("SOLUSDT", FOUR_H_MS, 1.0, 1.0, 1.0, 1.0),
        _RawMinute("SOLUSDT", FOUR_H_MS + _MIN_MS, 0.90, 0.90, 0.90, 0.90),
    ]

    raw_s3_closes = [
        # thesis condition true for a LONG: M_t <= 0.
        _RawS3Close("XRPUSDT", FOUR_H_MS, 1.0, 1.0, -0.01),
    ]

    raw_pair_closes = [
        # s_ab = 0.4*ln(1.0) - 0.6*ln(1.0) = 0 -> z_frozen = 0 -> MEAN_EXIT
        _RawPairClose("XRPUSDT", FOUR_H_MS, 1.0),
        _RawPairClose("DOGEUSDT", FOUR_H_MS, 1.0),
    ]

    return raw_minutes, raw_s3_closes, raw_pair_closes


def _s3_intent(**overrides):
    fields = {
        "symbol": "XRPUSDT",
        "side": "long",
        "signal_ts": 0,
        "entry_sl_distance": 0.0080,
        "entry_tp_distance": 0.0128,
        "config_id": "s3-smoke",
        "fold_id": "fold-00",
        "volatility_percentile": 55.0,
    }
    fields.update(overrides)
    return S3SignalIntent(**fields)


def _s4_intent(**overrides):
    fields = {
        "pair": ("XRPUSDT", "DOGEUSDT"),
        "signal_ts": 0,
        "side_a": "short",
        "side_b": "long",
        "weight_a": 0.4,
        "weight_b": 0.6,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": 0.0,
        "sigma": 0.05,
        "z_entry": 1.9,
        "gross_notional": 15.0,  # max(6/0.4, 6/0.6) == 15.0
        "entry_sl_distance": 0.0100,
        "entry_tp_distance": 0.0150,
        "config_id": "s4-smoke",
        "fold_id": "fold-00",
    }
    fields.update(overrides)
    return S4PairSignalIntent(**fields)


def _run_pipeline():
    """The full real H2 path -- returns everything the smoke assertions need."""
    raw_minutes, raw_s3_closes, raw_pair_closes = _build_fixture()

    minute_bars = [normalize_minute_bar(r) for r in raw_minutes]
    s3_close_features = [normalize_s3_close_feature(r) for r in raw_s3_closes]
    pair_leg_closes = [normalize_s4_pair_leg_close(r) for r in raw_pair_closes]

    minute_index = build_minute_index(minute_bars)
    s3_feature_index = {(f.symbol, f.close_ts): f for f in s3_close_features}
    pair_close_index = {(c.symbol, c.close_ts): c for c in pair_leg_closes}

    s3_candidates = [
        _s3_intent(symbol="XRPUSDT", side="long", signal_ts=0),
        _s3_intent(symbol="SOLUSDT", side="short", signal_ts=FOUR_H_MS),
        # missing exact tick anywhere in the fixture -> real NO_TRADE
        _s3_intent(symbol="DOGEUSDT", side="long", signal_ts=999_999_999),
    ]
    s3_result = run_s3_portfolio_stream(
        s3_candidates, minute_index, s3_feature_index, corpus_end_ts=_CORPUS_END
    )

    s4_candidates = [
        _s4_intent(
            pair=("XRPUSDT", "DOGEUSDT"),
            signal_ts=0,
            side_a="short",
            side_b="long",
            z_entry=1.9,
        ),
        _s4_intent(
            pair=("XRPUSDT", "DOGEUSDT"),
            signal_ts=FOUR_H_MS,
            side_a="long",
            side_b="short",
            z_entry=-1.9,
        ),
        # missing exact tick for both legs -> real NO_TRADE
        _s4_intent(pair=("DOGEUSDT", "SOLUSDT"), signal_ts=999_999_999),
    ]
    s4_result = run_s4_pair_basket_stream(
        s4_candidates, minute_index, pair_close_index, corpus_end_ts=_CORPUS_END
    )

    return s3_result, s4_result


class TestFixtureContractSmoke:
    def test_real_s3_thesis_exit_occurs(self):
        s3_result, _ = _run_pipeline()
        thesis_trades = [t for t in s3_result.trades if t.exit_reason == "THESIS_EXIT"]
        assert len(thesis_trades) == 1
        trade = thesis_trades[0]
        assert trade.symbol == "XRPUSDT"
        assert trade.side == "long"
        assert trade.exit_ts == FOUR_H_MS
        assert trade.entry_ts == 0

    def test_real_s3_short_direction_also_resolves(self):
        s3_result, _ = _run_pipeline()
        sol_trades = [t for t in s3_result.trades if t.symbol == "SOLUSDT"]
        assert len(sol_trades) == 1
        assert sol_trades[0].side == "short"
        assert sol_trades[0].exit_reason == "TP"

    def test_real_s3_nonzero_no_trade(self):
        s3_result, _ = _run_pipeline()
        assert len(s3_result.no_trades) >= 1
        assert any(nt.reason == "next_tick_unavailable" for nt in s3_result.no_trades)

    def test_real_s4_basket_exit_occurs(self):
        _, s4_result = _run_pipeline()
        mean_trades = [t for t in s4_result.trades if t.exit_reason == "MEAN_EXIT"]
        assert len(mean_trades) == 1
        trade = mean_trades[0]
        assert trade.pair == ("XRPUSDT", "DOGEUSDT")
        assert trade.side_a == "short"
        assert trade.side_b == "long"

    def test_real_s4_opposite_pair_direction_also_resolves(self):
        _, s4_result = _run_pipeline()
        second_leg_trades = [t for t in s4_result.trades if t.entry_ts == FOUR_H_MS]
        assert len(second_leg_trades) == 1
        trade = second_leg_trades[0]
        assert trade.side_a == "long"
        assert trade.side_b == "short"
        assert trade.exit_reason == "TP"

    def test_real_s4_nonzero_no_trade(self):
        _, s4_result = _run_pipeline()
        assert len(s4_result.no_trades) >= 1
        assert any(nt.reason == "next_tick_unavailable" for nt in s4_result.no_trades)

    def test_missing_minute_rejection_is_exact_not_scanned_ahead(self):
        s3_result, s4_result = _run_pipeline()
        s3_missing = [nt for nt in s3_result.no_trades if nt.symbol == "DOGEUSDT"]
        assert len(s3_missing) == 1
        assert s3_missing[0].signal_ts == 999_999_999

    def test_fresh_engine_state_per_scenario_no_shared_ledger_revaluation(self):
        """verify-R1 finding 3: reproduces H4's actual invocation pattern --
        THREE separate, independent `_run_pipeline()` calls (fresh engine
        state each time, exactly as AC27 requires H4 to do), one per path
        scenario. Confirms (a) no shared/aliased state leaks between the
        three independent engine runs, (b) each scenario's ledger is built
        from ITS OWN freshly-computed trades (never one trades object reused
        across ledger calls), and (c) the cost columns genuinely differ
        within a row via real inequality (no `or True`)."""
        base_s3, base_s4 = _run_pipeline()
        stress_s3, stress_s4 = _run_pipeline()
        upward_s3, upward_s4 = _run_pipeline()

        # Fresh engine state: three independent calls never return the same
        # object, even though (documented ultrathink decision) ROB-979 v1's
        # S3/S4 execution mechanics are NOT cost-scenario-gated, so identical
        # input legitimately produces identical CONTENT every time.
        assert base_s3.trades is not stress_s3.trades
        assert stress_s3.trades is not upward_s3.trades
        assert base_s3.trades == stress_s3.trades == upward_s3.trades
        assert base_s4.trades is not stress_s4.trades
        assert base_s4.trades == stress_s4.trades == upward_s4.trades

        base_rows = build_s3_scenario_ledger(base_s3.trades, PATH_SCENARIO_BASE13)
        stress_rows = build_s3_scenario_ledger(
            stress_s3.trades, PATH_SCENARIO_PRIMARY_STRESS17
        )
        upward_rows = build_s3_scenario_ledger(upward_s3.trades, "upward_stress22")
        assert (
            len(base_rows)
            == len(stress_rows)
            == len(upward_rows)
            == len(base_s3.trades)
        )
        for base_row, stress_row, upward_row in zip(
            base_rows, stress_rows, upward_rows, strict=True
        ):
            assert base_row.path_scenario == PATH_SCENARIO_BASE13
            assert stress_row.path_scenario == PATH_SCENARIO_PRIMARY_STRESS17
            assert upward_row.path_scenario == "upward_stress22"
            # same gross/E13 regardless of which scenario call produced the
            # row (E13/E17/E22 are all three always present on every row,
            # AC29) -- a REAL equality check, not a tautology.
            assert abs(base_row.e13_bps - stress_row.e13_bps) < 1e-9
            assert abs(base_row.e13_bps - upward_row.e13_bps) < 1e-9
            # the cost columns genuinely differ WITHIN a row (real
            # inequality, replacing the prior `... or True` tautology).
            assert base_row.e13_bps != base_row.e17_bps
            assert base_row.e17_bps != base_row.e22_bps
            assert base_row.e13_bps != base_row.e22_bps

        s4_base_rows = build_s4_scenario_ledger(base_s4.trades, PATH_SCENARIO_BASE13)
        s4_upward_rows = build_s4_scenario_ledger(upward_s4.trades, "upward_stress22")
        assert len(s4_base_rows) == len(s4_upward_rows) == len(base_s4.trades)
        for base_row, upward_row in zip(s4_base_rows, s4_upward_rows, strict=True):
            assert base_row.e13_bps != base_row.e22_bps
            assert abs(base_row.e13_bps - upward_row.e13_bps) < 1e-9

    def test_same_input_byte_hash_equality_across_reruns(self):
        s3_result_1, s4_result_1 = _run_pipeline()
        s3_result_2, s4_result_2 = _run_pipeline()

        rows_1 = build_s3_scenario_ledger(s3_result_1.trades, PATH_SCENARIO_BASE13)
        rows_2 = build_s3_scenario_ledger(s3_result_2.trades, PATH_SCENARIO_BASE13)
        assert s3_ledger_hash(rows_1) == s3_ledger_hash(rows_2)

        s4_rows_1 = build_s4_scenario_ledger(s4_result_1.trades, PATH_SCENARIO_BASE13)
        s4_rows_2 = build_s4_scenario_ledger(s4_result_2.trades, PATH_SCENARIO_BASE13)
        assert s4_ledger_hash(s4_rows_1) == s4_ledger_hash(s4_rows_2)

    def test_exact_float_zero_funding_when_no_funding_source_supplied(self):
        s3_result, _ = _run_pipeline()
        rows = build_s3_scenario_ledger(s3_result.trades, PATH_SCENARIO_BASE13)
        for row in rows:
            assert type(row.funding_bps) is float
            assert row.funding_bps == 0.0

    def test_s4_historical_null_posture_survives_the_full_pipeline(self):
        _, s4_result = _run_pipeline()
        for trade in s4_result.trades:
            assert trade.order_id_a is None
            assert trade.order_id_b is None
            assert trade.demo_eligible is False
            assert trade.pair_executor_validated is False
            assert trade.volatility_percentile is None
            assert trade.volatility_percentile_provenance == "not_defined_for_s4"
