import dataclasses

from external_strategy_sieve.rubric import RUBRIC, classify_license, derive_scores
from external_strategy_sieve.tests.test_schema import _good_card


def test_classify_license():
    assert classify_license("MIT") == "permissive"
    assert classify_license("Apache-2.0") == "permissive"
    assert classify_license("GPL-3.0") == "strong_copyleft"
    assert classify_license("AGPLv3") == "strong_copyleft"
    assert classify_license("LGPL-2.1") == "weak_copyleft"
    assert classify_license("proprietary, source hidden") == "unknown"
    assert classify_license("") == "unknown"


def test_derive_scores_open_ohlcv_card():
    scores = derive_scores(_good_card(
        license="MIT", code_availability="open",
        data_requirements=("ohlcv",), expected_cost_sensitivity="low",
        spot_or_futures="both", novelty_vs_failed_families="novel",
        holding_horizon="intraday", implementation_complexity="low",
        lookahead_repaint_risk="none", tail_risk_flags=(),
    ))
    assert scores["positive"]["license_safety"] == 3
    assert scores["positive"]["source_hygiene_reproducibility"] == 3
    assert scores["positive"]["faithful_port_feasibility"] == 3
    assert scores["positive"]["data_availability_auto_trader"] == 3
    assert scores["positive"]["cost_fee_survivability_potential"] == 3
    assert scores["positive"]["market_fit_binance_demo"] == 3
    assert scores["positive"]["novelty_vs_failed_families"] == 3
    assert scores["positive"]["expected_daily_review_usefulness"] == 3
    assert scores["tail_severity"] == 0


def test_derive_scores_penalises_complexity_and_repaint_for_port():
    scores = derive_scores(_good_card(
        code_availability="open", implementation_complexity="high",
        lookahead_repaint_risk="high",
    ))
    # open(3) - complexity_high(2) - repaint_high(1) floored at 0
    assert scores["positive"]["faithful_port_feasibility"] == 0


def test_derive_scores_data_availability_tiers():
    ohlcv_only = derive_scores(_good_card(data_requirements=("ohlcv",)))
    funding_oi = derive_scores(_good_card(data_requirements=("ohlcv", "funding", "oi")))
    orderbook = derive_scores(_good_card(data_requirements=("ohlcv", "orderbook")))
    fundamentals = derive_scores(_good_card(data_requirements=("ohlcv", "fundamentals")))
    assert ohlcv_only["positive"]["data_availability_auto_trader"] == 3
    assert funding_oi["positive"]["data_availability_auto_trader"] == 2
    assert orderbook["positive"]["data_availability_auto_trader"] == 1
    assert fundamentals["positive"]["data_availability_auto_trader"] == 0


def test_tail_severity_takes_max_flag():
    s = derive_scores(_good_card(tail_risk_flags=("leverage", "martingale")))
    assert s["tail_severity"] == 3  # martingale dominates


def test_rubric_hash_is_deterministic():
    assert RUBRIC.config_hash() == RUBRIC.config_hash()


def test_rubric_hash_changes_when_a_weight_is_tweaked():
    tweaked = dataclasses.replace(RUBRIC, keep_threshold=70.0)
    assert tweaked.config_hash() != RUBRIC.config_hash()
