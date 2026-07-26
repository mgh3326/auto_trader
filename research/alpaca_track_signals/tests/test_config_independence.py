"""ROB-1061 H3 (AC24) — all 16 sealed configs run independently over the
SAME corpus snapshot, with no candidate-buffer aliasing across configs.

ROB-1012 lesson (a real regression in the sibling nautilus_scalping H3
generator, cited by this issue): a bug that only fires against a REAL
corpus let candidate buffers alias ACROSS configs during a PBO run — one
config's per-symbol candidate list silently became another's. This test
proves the absence of that class of bug for THIS package's engines: run
every AP-A1 config (and every AP-A2 config) over the exact same
``bars_by_symbol``/``universe`` snapshot, in ARBITRARY order, and verify
each config's OWN output depends ONLY on its OWN (f,s,m,threshold) /
(L,k,b) parameters — cross-checked against an independent direct
recomputation via ``indicators`` for every config, not merely "the 8/8
results looked plausible".
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import configs as cfg
import dats_engine
import decision_calendar as dc
import indicators as ind
import pit_universe_alpaca as pu
import wcmb_engine
import wcmb_ranking as wr
from daily_bars import DAY_MS, DailyBar


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime(y, m, d, hh, mm, ss, tzinfo=UTC).timestamp() * 1000)


DECISION_TS = _ms(2026, 7, 20, 0, 5, 0)  # a Monday
WINDOW_END = dc.prior_completed_day_window(DECISION_TS)[1]


def _bars_ending_at_window(closes: list[float]) -> tuple[DailyBar, ...]:
    n = len(closes)
    bars = []
    for i, close in enumerate(closes):
        day_start = WINDOW_END - (n - i) * DAY_MS
        bars.append(
            DailyBar(
                day_start_ms=day_start,
                day_end_ms=day_start + DAY_MS,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=0.0,
                minute_count_observed=1440,
                imputed_minutes=0,
                max_gap_minutes=0,
                gap_in_last_60min=False,
                is_valid=True,
                is_segment_start=(i == 0),
            )
        )
    return tuple(bars)


def _snapshot(eligible: tuple[str, ...]) -> pu.UniverseSnapshot:
    # Pad with filler symbols (no bars supplied for them -- harmless
    # INVALID_DECISION_DAY no-ops) so N_t >= 18 by default (Run A §6 rule 7):
    # this file is about AC24 config-independence, not the restricted-entry
    # gate, so an unrealistically tiny fixture universe must not trip it.
    padding = tuple(f"PAD{i}/USD" for i in range(max(0, 18 - len(eligible))))
    all_eligible = tuple(sorted(set(eligible) | set(padding)))
    return pu.UniverseSnapshot(
        decision_ts_ms=DECISION_TS,
        eligible_symbols=all_eligible,
        per_symbol=(),
        n_t=len(all_eligible),
        meets_min_universe_size=len(all_eligible) >= 18,
    )


def _piecewise_closes(
    flat_days: int, spike_days: int, spike_step: float
) -> list[float]:
    flat = [100.0] * flat_days
    spike = [100.0 * (1 + spike_step) ** i for i in range(1, spike_days + 1)]
    return flat + spike


def _uptrend_closes(n: int, start: float = 100.0, step: float = 0.01) -> list[float]:
    return [start * (1 + step) ** i for i in range(n)]


# A handful of distinct per-symbol shapes so D/R/Score genuinely differ by
# lookback window, not just by construction accident.
_SYMBOL_SHAPES = {
    "AAA/USD": _piecewise_closes(150, 60, 0.02),
    "BBB/USD": _piecewise_closes(100, 90, 0.006),
    "CCC/USD": _piecewise_closes(180, 20, 0.03),
}


def test_all_8_ap_a1_configs_are_independent_over_the_same_corpus_snapshot():
    bars_by_symbol = {
        symbol: _bars_ending_at_window(closes)
        for symbol, closes in _SYMBOL_SHAPES.items()
    }
    universe = _snapshot(tuple(_SYMBOL_SHAPES))
    configs = list(cfg.build_ap_a1_configs())
    random.Random(7).shuffle(configs)  # arbitrary run order

    results = {}
    for config in configs:
        results[config.config_id] = dats_engine.run_ap_a1_decision(
            decision_ts_ms=DECISION_TS,
            config=config,
            universe=universe,
            bars_by_symbol=bars_by_symbol,
            prior_state={},
        )

    # No shared object identity across configs' returned containers.
    all_new_state_ids = {id(r.new_state) for r in results.values()}
    assert len(all_new_state_ids) == len(results)

    # Every config's own D (hence its own accept/reject verdict) is cross-
    # checked against an INDEPENDENT direct recomputation from `indicators`
    # using THAT config's own (f, s, m, threshold) -- if a candidate buffer
    # ever aliased across configs, at least one config's verdict would
    # reflect ANOTHER config's D/R instead of its own, and this would catch
    # it as a mismatch.
    for config in configs:
        f, s, m, threshold = (
            config.params["f"],
            config.params["s"],
            config.params["m"],
            config.params["threshold"],
        )
        result = results[config.config_id]
        for symbol, closes in _SYMBOL_SHAPES.items():
            d = ind.compute_trend_d(closes, f=f, s=s)
            r = ind.compute_momentum_r(closes, m=m)
            expected_entry = d >= threshold and r > 0.0
            record = next(rec for rec in result.records if rec.symbol == symbol)
            if expected_entry:
                assert record.reason_code == "ENTRY_ACCEPTED", (
                    config.config_id,
                    symbol,
                    d,
                    r,
                )
            else:
                assert record.reason_code != "ENTRY_ACCEPTED", (
                    config.config_id,
                    symbol,
                    d,
                    r,
                )

    # Distinct D values ACROSS configs prove no shared/overwritten buffer:
    # different (f, s) must simply produce different EMA ratios.
    d_values = {
        config.config_id: ind.compute_trend_d(
            _SYMBOL_SHAPES["AAA/USD"], f=config.params["f"], s=config.params["s"]
        )
        for config in configs
    }
    assert len(set(d_values.values())) == len(
        {(c.params["f"], c.params["s"]) for c in configs}
    )


def test_all_8_ap_a2_configs_are_independent_over_the_same_corpus_snapshot():
    bars_by_symbol = {
        symbol: _bars_ending_at_window(closes)
        for symbol, closes in _SYMBOL_SHAPES.items()
    }
    universe = _snapshot(tuple(_SYMBOL_SHAPES))
    configs = list(cfg.build_ap_a2_configs())
    random.Random(11).shuffle(configs)

    results = {}
    for config in configs:
        results[config.config_id] = wcmb_engine.run_ap_a2_decision(
            decision_ts_ms=DECISION_TS,
            config=config,
            universe=universe,
            bars_by_symbol=bars_by_symbol,
            prior_held={},
        )

    all_new_held_ids = {id(r.new_held) for r in results.values()}
    assert len(all_new_held_ids) == len(results)

    for config in configs:
        ell, k = config.params["L"], config.params["k"]
        result = results[config.config_id]
        scores = {
            symbol: ind.compute_score(closes, ell=ell)
            for symbol, closes in _SYMBOL_SHAPES.items()
        }
        ranks = wr.rank_symbols(scores)
        for symbol in _SYMBOL_SHAPES:
            record = next(rec for rec in result.records if rec.symbol == symbol)
            if scores[symbol] > 0.0 and ranks[symbol] <= k:
                assert record.reason_code == "RANK_BUY_ACCEPTED", (
                    config.config_id,
                    symbol,
                    scores[symbol],
                    ranks[symbol],
                )
            elif scores[symbol] <= 0.0:
                assert record.reason_code == "SCORE_NOT_POSITIVE", (
                    config.config_id,
                    symbol,
                )

    score_values = {
        config.config_id: ind.compute_score(
            _SYMBOL_SHAPES["BBB/USD"], ell=config.params["L"]
        )
        for config in configs
    }
    distinct_l_values = {c.params["L"] for c in configs}
    assert len(set(score_values.values())) == len(distinct_l_values)


# --------------------------------------------------------------------------- #
# V1/V2 (ROB-1061 adversarial-verification): both `d_values`/`score_values`
# checks above never reference `results` at all -- they independently
# recompute D/Score from each config's own (f,s)/L and assert those values
# are merely DISTINCT across configs, a property that holds even if the
# ENGINE itself silently hardcoded a single f/k and ignored the config it
# was handed (V1: AP-A1 hardcodes f=14; V2: AP-A2 hardcodes k=5) -- the whole
# 16-config grid would then be secretly evaluating only 1 config, and these
# tests would still pass. The two tests below force the ENGINE'S OWN VERDICT
# to differ across two configs that share every parameter except the one
# under test, on a fixture specifically engineered so the verdict actually
# flips -- an oracle that distinguishes configs by their real output.
# --------------------------------------------------------------------------- #


def test_ap_a1_f_parameter_actually_changes_the_engine_verdict_not_just_the_grid_shape():
    # A sharp decline followed by a recent sharp recovery: the FAST EMA
    # (f=14) recovers above the +0.005 threshold quickly; the slower EMA
    # (f=21, same s=56/m=28/threshold otherwise) has not caught up yet and
    # stays negative. If the engine silently hardcoded f=14 regardless of
    # `config.params["f"]`, AP-A1-04 (f=21) would wrongly ALSO enter.
    closes = []
    price = 100.0
    for _ in range(60):
        price *= 1 - 0.006
        closes.append(price)
    for _ in range(10):
        price *= 1 + 0.04
        closes.append(price)
    d14 = ind.compute_trend_d(closes, f=14, s=56)
    d21 = ind.compute_trend_d(closes, f=21, s=56)
    r28 = ind.compute_momentum_r(closes, m=28)
    assert d14 >= 0.005 > d21 and r28 > 0.0  # sanity: the fixture discriminates

    config_f14 = cfg.build_ap_a1_configs()[0]  # AP-A1-00: f=14, s=56, m=28
    config_f21 = cfg.build_ap_a1_configs()[4]  # AP-A1-04: f=21, s=56, m=28
    assert config_f14.params["s"] == config_f21.params["s"]
    assert config_f14.params["m"] == config_f21.params["m"]
    assert config_f14.params["f"] != config_f21.params["f"]

    universe = _snapshot(("XXX/USD",))
    bars_by_symbol = {"XXX/USD": _bars_ending_at_window(closes)}
    result_f14 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=config_f14,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_state={},
    )
    result_f21 = dats_engine.run_ap_a1_decision(
        decision_ts_ms=DECISION_TS,
        config=config_f21,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_state={},
    )
    record_f14 = next(r for r in result_f14.records if r.symbol == "XXX/USD")
    record_f21 = next(r for r in result_f21.records if r.symbol == "XXX/USD")
    assert record_f14.reason_code == "ENTRY_ACCEPTED"
    assert record_f21.reason_code != "ENTRY_ACCEPTED"


def test_ap_a2_k_parameter_actually_changes_the_engine_verdict_not_just_the_grid_shape():
    # 6 unheld candidates, all Score>0, strictly ranked 1..6 by construction
    # (distinct step sizes). Under k=5 the weakest (rank 6) is
    # RANK_SLOTS_FULL; under k=6 (same L/b otherwise) that SAME symbol must
    # be bought instead. If the engine silently hardcoded k=5 regardless of
    # `config.params["k"]`, AP-A2-02 (k=6) would wrongly ALSO reject it.
    n = 30
    bars_by_symbol = {
        f"C{i}/USD": _bars_ending_at_window(_uptrend_closes(n, step=0.05 - i * 0.005))
        for i in range(6)
    }
    universe = _snapshot(tuple(bars_by_symbol))

    config_k5 = cfg.build_ap_a2_configs()[0]  # AP-A2-00: L=14, k=5, b=1
    config_k6 = cfg.build_ap_a2_configs()[2]  # AP-A2-02: L=14, k=6, b=1
    assert config_k5.params["L"] == config_k6.params["L"]
    assert config_k5.params["b"] == config_k6.params["b"]
    assert config_k5.params["k"] != config_k6.params["k"]

    result_k5 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=config_k5,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held={},
    )
    result_k6 = wcmb_engine.run_ap_a2_decision(
        decision_ts_ms=DECISION_TS,
        config=config_k6,
        universe=universe,
        bars_by_symbol=bars_by_symbol,
        prior_held={},
    )
    record_k5 = next(r for r in result_k5.records if r.symbol == "C5/USD")
    record_k6 = next(r for r in result_k6.records if r.symbol == "C5/USD")
    assert record_k5.reason_code == "RANK_SLOTS_FULL"
    assert record_k6.reason_code == "RANK_BUY_ACCEPTED"
