from __future__ import annotations

import artifact as art
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
