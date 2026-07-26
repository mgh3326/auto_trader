from __future__ import annotations

import artifact as art
import configs as cfg
import pytest
import seal_consumption as sc


def test_min_strategy_target_usd_is_read_from_the_seal_not_hardcoded():
    # Cross-check directly against the H2 seal's own accessor path -- this
    # test would fail if seal_consumption ever hardcoded a copy that drifts
    # from the seal itself.
    bundle = sc.load_sealed_configs_and_params()
    assert sc.min_strategy_target_usd() == float(
        bundle.params.run_status.min_strategy_target_usd
    )


def test_min_broker_order_usd_is_strictly_below_the_strategy_floor():
    assert sc.min_broker_order_usd() < sc.min_strategy_target_usd()


def test_load_sealed_configs_and_params_returns_all_16():
    bundle = sc.load_sealed_configs_and_params()
    assert len(bundle.configs) == 16


def test_sealed_config_by_id_round_trips_a_real_config():
    bundle = sc.load_sealed_configs_and_params()
    any_id = bundle.configs[0].config_id
    fetched = sc.sealed_config_by_id(any_id)
    assert fetched.config_id == any_id
    assert fetched.canonical_hash == bundle.configs[0].canonical_hash


def test_sealed_config_by_id_rejects_an_unknown_id():
    with pytest.raises(sc.UnknownConfigIdError, match="AP-A1-99"):
        sc.sealed_config_by_id("AP-A1-99")


def test_seal_drift_error_fires_when_the_pinned_hash_no_longer_matches(monkeypatch):
    monkeypatch.setattr(art, "SEALED_ARTIFACT_SEMANTIC_HASH", "0" * 64)
    with pytest.raises(sc.SealDriftError, match="drifted seal"):
        sc.load_sealed_configs_and_params()


# --------------------------------------------------------------------------- #
# assert_sealed_config -- the actual NO_THRESHOLD_RELAXATION enforcement
# point (ROB-1061 adversarial-verification SPEC DEFECT 3): neither engine
# previously checked more than `config.family`, so a forged config reusing a
# real config_id with a relaxed threshold (or any other param) passed
# straight through.
# --------------------------------------------------------------------------- #


def test_assert_sealed_config_accepts_every_genuine_sealed_config():
    bundle = sc.load_sealed_configs_and_params()
    for config in bundle.configs:
        sc.assert_sealed_config(config)  # must not raise


def test_assert_sealed_config_rejects_a_relaxed_threshold_reusing_a_real_id():
    bundle = sc.load_sealed_configs_and_params()
    real = next(c for c in bundle.configs if c.family == "AP-A1")
    forged_params = dict(real.params)
    forged_params["threshold"] = 0.0001  # relaxed from the sealed 0.005
    forged = cfg.ConfigSpec(
        config_id=real.config_id,
        family=real.family,
        params=forged_params,
        canonical_hash=cfg.canonical_config_hash(
            real.config_id, real.family, forged_params
        ),
    )
    with pytest.raises(sc.ConfigNotSealedError, match=real.config_id):
        sc.assert_sealed_config(forged)


def test_assert_sealed_config_rejects_an_unknown_config_id():
    bundle = sc.load_sealed_configs_and_params()
    real = bundle.configs[0]
    forged = cfg.ConfigSpec(
        config_id="AP-A1-99-RELAXED",
        family=real.family,
        params=dict(real.params),
        canonical_hash="0" * 64,
    )
    with pytest.raises(sc.ConfigNotSealedError, match="AP-A1-99-RELAXED"):
        sc.assert_sealed_config(forged)


def test_assert_sealed_config_rejects_a_real_id_with_tampered_canonical_hash_only():
    # Params byte-identical to the sealed row, but the canonical_hash field
    # itself was tampered -- still must fail closed (never trust the caller's
    # own canonical_hash claim over a byte comparison of params).
    bundle = sc.load_sealed_configs_and_params()
    real = bundle.configs[0]
    forged = cfg.ConfigSpec(
        config_id=real.config_id,
        family=real.family,
        params=dict(real.params),
        canonical_hash="0" * 64,
    )
    with pytest.raises(sc.ConfigNotSealedError, match=real.config_id):
        sc.assert_sealed_config(forged)
