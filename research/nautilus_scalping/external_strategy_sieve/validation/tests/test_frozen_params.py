from external_strategy_sieve.validation.frozen_params import (
    FROZEN_PARAMS,
    params_hash,
)


def test_five_candidates_each_with_one_param_set():
    assert set(FROZEN_PARAMS) == {
        "freqtrade_supertrend",
        "freqtrade_bbrsi_naive",
        "tv_squeeze_momentum",
        "tv_range_filter",
        "tv_chandelier_exit",
    }
    for spec in FROZEN_PARAMS.values():
        assert "signal" in spec and "interval" in spec and "params" in spec


def test_params_hash_is_deterministic():
    assert params_hash() == params_hash()
