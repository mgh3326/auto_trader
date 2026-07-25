"""ROB-1060 H2 — RED-first: pure ROB-846 identity-component builders.

Mirrors ``research/nautilus_scalping/rob946_campaign_identity.py``'s
precedent exactly: this module builds the 11 ROB-846
``IDENTITY_COMPONENTS`` (strategy/code/params/dataset_manifest/universe/pit/
frozen_config/policy/benchmark/cost/mdd) as PLAIN DATA — it never imports
``app.*`` or ``StrategyExperimentIdentity`` itself. The app-side CLI
(``registry_cli.py``) is the only place that constructs the real Pydantic
identity, using this module's output as pure input data.
"""

from __future__ import annotations

import hashlib

import pytest


def test_strategy_source_provenance_verifies_its_own_hash():
    import identity as m

    prov = m.StrategySourceProvenance(
        strategy_key="alpaca_track_ap_a1",
        strategy_version="2026-07-25-seal-v1",
        source_text="entry: D>=0.005 and R>0",
    )
    expected = hashlib.sha256(b"entry: D>=0.005 and R>0").hexdigest()
    assert prov.verified_source_sha256() == expected


def test_strategy_source_provenance_rejects_a_stale_asserted_hash():
    import identity as m

    prov = m.StrategySourceProvenance(
        strategy_key="alpaca_track_ap_a1",
        strategy_version="2026-07-25-seal-v1",
        source_text="entry: D>=0.005 and R>0",
        expected_source_sha256="0" * 64,
    )
    with pytest.raises(m.SourceMismatchError):
        prov.verified_source_sha256()


def test_ap_a1_and_ap_a2_have_distinct_default_formula_provenance():
    import identity as m

    a1 = m.default_formula_provenance("AP-A1")
    a2 = m.default_formula_provenance("AP-A2")
    assert a1.strategy_key != a2.strategy_key
    assert a1.verified_source_sha256() != a2.verified_source_sha256()


def test_build_components_for_config_has_exactly_the_11_rob846_keys():
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    from research_contracts.canonical_hash import IDENTITY_COMPONENTS

    assert set(components) == set(IDENTITY_COMPONENTS)
    for name in IDENTITY_COMPONENTS:
        assert components[name] is not None


def test_params_component_is_the_only_thing_that_varies_within_one_family():
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    a1_configs = cfg.build_ap_a1_configs()
    specs = [m.build_components_for_config(c, seal) for c in a1_configs]
    m.validate_same_family_components_are_identical(
        list(zip(a1_configs, specs, strict=True))
    )
    # And the params component DOES differ across the 8 configs (it is the
    # one legitimate axis of variation).
    params_values = {
        tuple(sorted(s["params"].items())) for s in specs
    }
    assert len(params_values) == 8


def test_cross_family_component_divergence_is_expected_and_detected():
    """AP-A1 and AP-A2 legitimately differ on frozen_config/strategy/code —
    the validator only enforces identity WITHIN one strategy_key, never
    across families."""
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    a1 = cfg.build_ap_a1_configs()[0]
    a2 = cfg.build_ap_a2_configs()[0]
    c_a1 = m.build_components_for_config(a1, seal)
    c_a2 = m.build_components_for_config(a2, seal)
    assert c_a1["strategy"] != c_a2["strategy"]
    assert c_a1["frozen_config"] != c_a2["frozen_config"]


def test_dataset_manifest_component_is_an_explicit_pending_sentinel_by_default():
    """H1's real corpus manifest does not exist in this environment — the
    component must be an explicit, honest "pending" fact, never a fabricated
    hash standing in for missing data."""
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["dataset_manifest"]["status"] == "pending_h1_corpus_manifest"


def test_universe_component_reflects_the_sealed_20_symbol_universe():
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["universe"]["sealed_effective_n"] == 20
    assert "PEPE/USD" not in components["universe"]["symbols"]


def test_cost_component_carries_exactly_the_4_sealed_scenarios():
    import identity as m
    import configs as cfg
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a2_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["cost"]["scenarios_bp"] == {
        "C50": 50,
        "C100": 100,
        "C120": 120,
        "C150": 150,
    }
