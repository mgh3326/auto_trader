from __future__ import annotations

import artifact as art
import configs as cfg
import identity
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


def test_assert_sealed_config_rejects_a_stolen_genuine_hash_with_tampered_params():
    # B8 (ROB-1061 adversarial-verification): both existing forgery tests
    # above recompute `canonical_hash` from the FORGED params, so they trip
    # the `canonical_hash != sealed.canonical_hash` disjunct first -- the
    # `dict(config.params) != dict(sealed.params)` clause (the actual
    # NO_THRESHOLD_RELAXATION property this guard exists to enforce) is
    # never reached by either. A forger who STEALS the sealed row's genuine
    # `canonical_hash` string verbatim (rather than recomputing one from
    # tampered params) defeats the canonical_hash check entirely -- only the
    # params comparison can still catch this. Dropping that comparison would
    # let a 50x-relaxed threshold (0.005 -> 0.0001) pass as "sealed".
    bundle = sc.load_sealed_configs_and_params()
    real = next(c for c in bundle.configs if c.family == "AP-A1")
    forged_params = dict(real.params)
    forged_params["threshold"] = 0.0001  # relaxed from the sealed 0.005
    forged = cfg.ConfigSpec(
        config_id=real.config_id,
        family=real.family,
        params=forged_params,
        canonical_hash=real.canonical_hash,  # STOLEN, genuine, byte-identical
    )
    assert (
        forged.canonical_hash == real.canonical_hash
    )  # sanity: hash check alone would pass
    with pytest.raises(sc.ConfigNotSealedError, match=real.config_id):
        sc.assert_sealed_config(forged)


# --------------------------------------------------------------------------- #
# B14 (ROB-1061 adversarial-verification remediation, 2026-07-26):
# `initial_equity_usd()`'s ``a1 != a2`` divergence check was only ever
# exercised with a1 == a2 (``test_available_cash_and_base_slot_track_a_reseal_
# of_equity_and_base_slot`` in ``test_sizing.py`` resealed BOTH families
# identically via the same monkeypatched function) -- the actual mismatch-
# detection branch itself was never proven to fire. One-sided: only "the
# families agree" was tested, never "a genuine disagreement is caught".
# --------------------------------------------------------------------------- #


def test_initial_equity_usd_raises_seal_drift_error_on_a_genuine_family_divergence(
    monkeypatch,
):
    original = identity._build_frozen_config_component

    def _diverged(config, seal):
        component = dict(original(config, seal))
        # Force AP-A1 and AP-A2's frozen_config initial_equity_usd apart --
        # a genuine cross-family disagreement, not a uniform reseal.
        component["initial_equity_usd"] = 2000 if config.family == "AP-A1" else 1600
        return component

    monkeypatch.setattr(identity, "_build_frozen_config_component", _diverged)
    resealed_artifact = art.build_sealed_artifact()
    monkeypatch.setattr(
        art, "SEALED_ARTIFACT_SEMANTIC_HASH", resealed_artifact.semantic_hash()
    )
    with pytest.raises(sc.SealDriftError, match="diverges"):
        sc.initial_equity_usd()
