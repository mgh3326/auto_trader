"""ROB-384 — adapter tests.

Fixtures are inline and mirror the real schemas so the suite never depends on
the gitignored result JSONs (CI has no artifact root). Numbers are taken from
the real artifacts so the assertions double as a structural contract.
"""

from __future__ import annotations

import json
import math

from external_strategy_sieve.postmortem import gatereport_io as io


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# --- validated_signal_gate.v1: single report (ROB-320 meanrev shape) --------- #

_MEANREV = {
    "schema_version": "validated_signal_gate.v1",
    "candidate": "meanrev_zscore_fade",
    "trade_count": 789,
    "results": {
        "gross": {
            "fold": "gross",
            "trades": 789,
            "net_pnl": 12.49863233,
            "expectancy": 0.015841,
        },
        "net_after_cost": {
            "fold": "net_after_cost",
            "trades": 789,
            "net_pnl": -209.70628981,
            "expectancy": -0.265787,
        },
    },
    "per_fold": [
        {"fold": "train", "trades": 394, "net_pnl": -105.96},
        {"fold": "val", "trades": 197, "net_pnl": -52.32},
        {"fold": "oos", "trades": 198, "net_pnl": -51.43},
    ],
    "baselines": {
        "micro_breakout": {"net_after_cost": -1163.86, "trades": 4025},
        "random_entry": {"net_after_cost": -688.64, "trades": 2350},
    },
    "overfit_flags": {
        "low_trades": False,
        "single_fold_edge": False,
        "param_island": False,
    },
    "verdict": "not_validated",
    "verdict_reasons": [
        "oos net-after-cost -51.43 <= 0",
        "oos profit_factor 0.37 <= 1.0",
    ],
}


def test_meanrev_reparse(tmp_path):
    [ev] = io.from_meanrev(_write(tmp_path, "meanrev.json", _MEANREV))
    assert ev.issue == "ROB-320"
    assert ev.source == "reparsed"
    assert ev.trade_count == 789
    # gross +0.16 bps (tiny positive edge), net@10 ~ -2.66 bps (fees dominate).
    assert math.isclose(ev.gross_bps, 0.1584, abs_tol=1e-3)
    assert math.isclose(ev.net_bps_by_fee["10"], -2.6579, abs_tol=1e-3)
    assert ev.net_bps_by_fee["0"] > ev.net_bps_by_fee["10"]  # decreasing in fee
    # no fee sweep -> net_after_cost IS the 10 bps ref endpoint
    assert math.isclose(ev.net_bps_by_fee["0"], ev.gross_bps, abs_tol=1e-9)
    assert ev.single_fold_edge is False  # explicit flag honoured
    # beats both (less-negative) baselines but is still net-negative
    assert ev.baseline_beat == {"micro_breakout": True, "random_entry": True}
    assert ev.n_folds == 3
    assert ev.oos_trade_count == 198


# --- validated_signal_gate.v1: phase3 wrapper with fee_sweep ---------------- #

_PHASE3 = {
    "plan": {"fee_bps": 4.0, "fee_grid_bps": [10.0, 7.5, 5.0, 2.0, 0.0]},
    "results": {
        "freqtrade_supertrend": {
            "class": "reject",
            "reasons": ["gross=799.15, net@fee=-663.25; edge appears in only one fold"],
            "trade_count": 1828,
            "verdict": "not_validated",
            "verdict_reasons": ["edge appears in only one fold"],
            "results": {
                "gross": {"net_pnl": 799.1507, "trades": 1828, "expectancy": 0.43717},
                "net_after_cost": {
                    "net_pnl": -663.2493,
                    "trades": 1828,
                    "expectancy": -0.36283,
                },
            },
            "per_fold": [
                {"fold": "train", "trades": 900, "net_pnl": -2607.6},
                {"fold": "val", "trades": 450, "net_pnl": -907.2},
                {"fold": "oos", "trades": 478, "net_pnl": 2851.6},
            ],
            "baselines": {
                "micro_breakout": {"net_after_cost": -647.9, "trades": 2226},
                "random_entry": {"net_after_cost": -1529.4, "trades": 2230},
            },
            "fee_sweep_net_pnl": {
                "10.0bps": -2856.8493,
                "7.5bps": -1942.8493,
                "5.0bps": -1028.8493,
                "2.0bps": 67.9507,
                "0.0bps": 799.1507,
            },
        },
        "tv_squeeze_momentum": {
            "class": "research_candidate",
            "reasons": [
                "gross-positive but failed gate: edge appears in only one fold"
            ],
            "trade_count": 2230,
            "verdict": "not_validated",
            "verdict_reasons": ["edge appears in only one fold"],
            "results": {
                "gross": {"net_pnl": 3033.7355, "trades": 2230, "expectancy": 1.3604},
                "net_after_cost": {
                    "net_pnl": 1249.7355,
                    "trades": 2230,
                    "expectancy": 0.5604,
                },
            },
            "per_fold": [
                {"fold": "train", "trades": 1000, "net_pnl": -185.1},
                {"fold": "val", "trades": 600, "net_pnl": -834.1},
                {"fold": "oos", "trades": 630, "net_pnl": 2268.9},
            ],
            "baselines": {
                "micro_breakout": {"net_after_cost": -100.0},
                "random_entry": {"net_after_cost": -200.0},
            },
            "fee_sweep_net_pnl": {
                "10.0bps": -1426.2645,
                "7.5bps": -311.2645,
                "5.0bps": 803.7355,
                "2.0bps": 2141.7355,
                "0.0bps": 3033.7355,
            },
            "caveat": "non_faithful_clean_room_spec: momentum simplified from LazyBear linreg to close-SMA",
        },
    },
}


def test_phase3_uses_fee_sweep_endpoint(tmp_path):
    evs = {e.candidate: e for e in io.from_phase3(_write(tmp_path, "p3.json", _PHASE3))}
    st = evs["freqtrade_supertrend"]
    # net@0 == gross; net@10 from fee_sweep[10.0bps], NOT from net_after_cost (4 bps).
    assert math.isclose(st.net_bps_by_fee["0"], st.gross_bps, abs_tol=1e-9)
    # gross 799.15/1828 -> 0.437 expectancy -> 4.37 bps
    assert math.isclose(st.gross_bps, 4.371, abs_tol=1e-2)
    # net@10 = -2856.85/1828 -> -15.62 bps  (sweep endpoint, not -3.63 from 4 bps)
    assert math.isclose(st.net_bps_by_fee["10"], -15.628, abs_tol=1e-2)
    # interpolated 4 bps point reproduces net_after_cost (-663.25/1828 -> -3.628 bps)
    assert math.isclose(st.net_bps_by_fee["4"], -3.628, abs_tol=1e-2)
    assert st.single_fold_edge is True  # "one fold" reason
    assert "sieve_class=reject" in st.verdict

    sq = evs["tv_squeeze_momentum"]
    assert sq.single_fold_edge is True  # gross+ but only OOS fold positive
    assert "non_faithful_clean_room_spec" in sq.notes


def test_phase3_single_fold_heuristic_without_text(tmp_path):
    # Strip the explicit "one fold" reason -> must still infer from fold signs.
    obj = json.loads(json.dumps(_PHASE3))
    obj["results"]["freqtrade_supertrend"]["reasons"] = [
        "gross=799.15, net@fee=-663.25"
    ]
    obj["results"]["freqtrade_supertrend"]["verdict_reasons"] = []
    evs = {e.candidate: e for e in io.from_phase3(_write(tmp_path, "p3b.json", obj))}
    assert evs["freqtrade_supertrend"].single_fold_edge is True  # 1 of 3 folds positive


# --- rob382_falsification.v1 ------------------------------------------------- #

_FALS = {
    "schema_version": "rob382_falsification.v1",
    "overall_verdict": "no_decisive_survivor — ...",
    "candidates": [
        {
            "name": "ichi",
            "native_timeframe": "5m",
            "family_shape": "trend-following ichimoku",
            "trade_count": 830,
            "oos_trade_count": 385,
            "our_gross_bps": 15.258,
            "our_oos_gross_bps": 13.73,
            "our_net_bps_frozen_taker": 7.258,
            "our_net_bps_retail_ref": -4.742,
            "our_oos_net_bps_retail_ref": -6.27,
            "our_t_stat_gross": 1.903,
            "our_t_stat_oos_gross": 1.185,
            "target_t": 2.0,
            "gate_verdict": "validated",
            "our_verdict": "gross_edge_present_AND_oos_validated",
            "meets_decisive_survivor_bar": False,
            "beats_micro_breakout_baseline": True,
            "beats_random_baseline": True,
        },
        {
            "name": "elliot",
            "native_timeframe": "5m",
            "trade_count": 18,
            "oos_trade_count": 5,
            "our_gross_bps": 129.7298,
            "our_net_bps_retail_ref": 109.7298,
            "our_t_stat_gross": 3.647,
            "gate_verdict": "insufficient_data",
            "our_verdict": "gross_edge_present_but_underpowered",
            "meets_decisive_survivor_bar": False,
            "beats_micro_breakout_baseline": True,
            "beats_random_baseline": True,
        },
    ],
}


def test_falsification_reparse(tmp_path):
    p = _write(tmp_path, "fals.json", _FALS)
    evs = {e.candidate: e for e in io.from_falsification(p)}
    ichi = evs["ichi"]
    assert ichi.issue == "ROB-382"
    assert ichi.schema == "rob382_falsification.v1"
    assert math.isclose(ichi.gross_bps, 15.258, abs_tol=1e-9)
    assert math.isclose(ichi.net_bps_by_fee["0"], 15.258, abs_tol=1e-9)
    assert math.isclose(ichi.net_bps_by_fee["10"], -4.742, abs_tol=1e-9)
    # interpolated 4 bps must reproduce the artifact's recorded frozen-taker value
    assert math.isclose(ichi.net_bps_by_fee["4"], 7.258, abs_tol=1e-6)
    assert ichi.t_stat_gross == 1.903
    assert ichi.t_stat_oos == 1.185  # our_t_stat_oos_gross
    assert ichi.trade_count == 830 and ichi.oos_trade_count == 385
    assert (
        "gate=validated" in ichi.verdict and "decisive_survivor=False" in ichi.verdict
    )
    assert ichi.baseline_beat == {"micro_breakout": True, "random_same_turnover": True}
    assert io.falsification_overall_verdict(p).startswith("no_decisive_survivor")


# --- rob351_campaign.v1 ------------------------------------------------------ #

_CAMPAIGN = {
    "verdict_table": {
        "schema_version": "rob351_campaign.v1",
        "families": [
            {
                "name": "family1_breakout_continuation",
                "screen": "screened_out",
                "cost_binding_screen": False,
                "screen_reason": "OOS gross expectancy -70.99bps <= 0 (in-sample edge does not hold OOS)",
                "gate_verdict": None,
                "label_343": None,
            },
            {
                "name": "family2_ts_trend_basket",
                "screen": "screened_out",
                "cost_binding_screen": False,
                "screen_reason": "gross expectancy -27.53bps <= triviality floor 0.50bps",
                "gate_verdict": None,
                "label_343": None,
            },
        ],
    },
    "controls": {"btc_buy_hold_bps": 35938.60, "universe_size": 37},
    "spec_sample_counts": {
        "family1_breakout_continuation": 1366,
        "family2_ts_trend_basket": 58,
    },
}


def test_campaign_reparse(tmp_path):
    evs = {
        e.candidate: e
        for e in io.from_campaign(_write(tmp_path, "camp.json", _CAMPAIGN))
    }
    f1 = evs["family1_breakout_continuation"]
    assert f1.issue == "ROB-353"
    assert math.isclose(f1.gross_bps, -70.99, abs_tol=1e-9)
    assert f1.net_bps_by_fee == {}  # gross <= 0 -> net moot
    assert "cost_binding_screen=False" in f1.net_moot_reason
    assert "NOT the bottleneck" in f1.net_moot_reason
    assert f1.trade_count == 1366
    assert f1.baseline_beat == {"buy_and_hold_btc": False}  # gross far below buy&hold
    assert evs["family2_ts_trend_basket"].gross_bps == -27.53
