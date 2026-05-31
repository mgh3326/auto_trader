from external_strategy_sieve.schema import CandidateCard, validate


def _good_card(**overrides):
    base = {
        "candidate_id": "freqtrade_bbrsi",
        "source_url": "https://github.com/freqtrade/freqtrade-strategies",
        "source_bucket": "freqtrade_github",
        "license": "GPL-3.0",
        "code_availability": "open",
        "strategy_family": "mean_reversion",
        "spot_or_futures": "spot",
        "long_short": "long_only",
        "timeframe": "5m",
        "holding_horizon": "intraday",
        "entry_exit_summary": "BB lower touch + RSI<30 entry, BB mid exit",
        "data_requirements": ("ohlcv",),
        "tail_risk_flags": (),
        "lookahead_repaint_risk": "low",
        "implementation_complexity": "low",
        "novelty_vs_failed_families": "adjacent",
        "expected_cost_sensitivity": "high",
        "source_verified": False,
        "score_status": "unverified_seed",
        "recommended_disposition_pre_validation": "shadow_only",
    }
    base.update(overrides)
    return CandidateCard(**base)


def test_good_card_validates_clean():
    assert validate(_good_card()) == []


def test_bad_enum_is_reported():
    errors = validate(_good_card(source_bucket="reddit"))
    assert any("source_bucket" in e for e in errors)


def test_bad_data_requirement_is_reported():
    errors = validate(_good_card(data_requirements=("ohlcv", "twitter")))
    assert any("data_requirements" in e for e in errors)


def test_verified_card_missing_source_url_is_reported():
    # R2: a `verified` card must carry the evidence fields.
    errors = validate(
        _good_card(score_status="verified", source_verified=True, source_url="")
    )
    assert any("source_url" in e and "verified" in e for e in errors)


def test_unverified_seed_with_blank_url_is_allowed():
    # Seeds may carry a tentative pointer; they are simply not promotable yet.
    assert validate(_good_card(source_url="")) == []
