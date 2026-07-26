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
    with pytest.raises(m.SourceMismatchError, match="source SHA-256 mismatch"):
        prov.verified_source_sha256()


def test_ap_a1_and_ap_a2_have_distinct_default_formula_provenance():
    import identity as m

    a1 = m.default_formula_provenance("AP-A1")
    a2 = m.default_formula_provenance("AP-A2")
    assert a1.strategy_key != a2.strategy_key
    assert a1.verified_source_sha256() != a2.verified_source_sha256()


def test_build_components_for_config_has_exactly_the_11_rob846_keys():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    from research_contracts.canonical_hash import IDENTITY_COMPONENTS

    assert set(components) == set(IDENTITY_COMPONENTS)
    for name in IDENTITY_COMPONENTS:
        assert components[name] is not None


def test_params_component_is_the_only_thing_that_varies_within_one_family():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    a1_configs = cfg.build_ap_a1_configs()
    specs = [m.build_components_for_config(c, seal) for c in a1_configs]
    m.validate_same_family_components_are_identical(
        list(zip(a1_configs, specs, strict=True))
    )
    # And the params component DOES differ across the 8 configs (it is the
    # one legitimate axis of variation).
    params_values = {tuple(sorted(s["params"].items())) for s in specs}
    assert len(params_values) == 8


def test_cross_family_component_divergence_is_expected_and_detected():
    """AP-A1 and AP-A2 legitimately differ on frozen_config/strategy/code —
    the validator only enforces identity WITHIN one strategy_key, never
    across families."""
    import configs as cfg
    import identity as m
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
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["dataset_manifest"]["status"] == "pending_h1_corpus_manifest"


def test_universe_component_reflects_the_sealed_20_symbol_universe():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["universe"]["sealed_effective_n"] == 20
    assert "PEPE/USD" not in components["universe"]["symbols"]


def test_cost_component_carries_exactly_the_4_sealed_scenarios():
    import configs as cfg
    import identity as m
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


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock adversarial-verification Finding 2 (2026-07-26): belt-and-  #
# braces literal pins for the exact fields 4 mutations survived on            #
# (`cost.primary` C120->C50, `code.kind` formula-spec->real-implementation,   #
# `strategy_version` ...-v1->...-v2, `strategy_key` ...->..._relaxed). The    #
# root-cause fix is the digest coverage extension (see artifact.py /          #
# test_artifact.py); these are GRANULAR, so a future coverage regression in   #
# the digest doesn't silently reopen this specific hole.                     #
# --------------------------------------------------------------------------- #


def test_cost_component_primary_and_upward_are_pinned_literally():
    """`cost.primary`/`cost.upward` were only pinned on the `params.py`
    `CostScenarios` object (test_params.py), which a mutation hardcoding a
    DIFFERENT value directly in `_build_cost_component` bypasses entirely --
    pin them on the COMPONENT BUILDER'S OUTPUT too."""
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    for config in (cfg.build_ap_a1_configs()[0], cfg.build_ap_a2_configs()[0]):
        components = m.build_components_for_config(config, seal)
        assert components["cost"]["primary"] == "C120"
        assert components["cost"]["upward"] == "C150"


def test_strategy_component_literal_content_for_ap_a1_and_ap_a2():
    """The `strategy` component's `strategy_key`/`strategy_version` were
    never literally pinned -- only existence-checked (`is not None`) and
    cross-family-divergence-checked. A mutation bumping `strategy_version`
    to `...-v2` or `strategy_key` to `..._relaxed` survived the green suite."""
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    a1 = m.build_components_for_config(cfg.build_ap_a1_configs()[0], seal)
    assert a1["strategy"] == {
        "family": "AP-A1",
        "strategy_key": "alpaca_track_ap_a1",
        "strategy_version": "2026-07-25-h2-formula-seal-v1",
    }
    a2 = m.build_components_for_config(cfg.build_ap_a2_configs()[0], seal)
    assert a2["strategy"] == {
        "family": "AP-A2",
        "strategy_key": "alpaca_track_ap_a2",
        "strategy_version": "2026-07-25-h2-formula-seal-v1",
    }


def test_code_component_kind_is_pinned_as_formula_specification_not_implementation():
    """H3 (the real DATS/WCM-B implementation, ROB-1061) does not exist yet --
    the `code.kind` identity component must be the honest
    `formula_specification_not_implementation` sentinel, never
    `real_implementation` (that value belongs to H3's eventual registration,
    which MUST derive a different `code_hash`/`experiment_id`, per this
    module's own docstring). A mutation flipping this value silently
    masquerades the H2 formula-spec seal as H3's implementation identity."""
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["code"]["kind"] == "formula_specification_not_implementation"
    assert components["code"]["source_sha256"] == (
        m.default_formula_provenance("AP-A1").verified_source_sha256()
    )


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 3/4: literal content assertions.                      #
# `assert components[name] is not None` cannot fail for a dict -- emptying    #
# pit/policy/benchmark to {} (and dropping quote_mode from universe) survived #
# a green suite. These pin the FULL literal content, not mere existence.      #
# --------------------------------------------------------------------------- #


def test_pit_component_literal_content():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["pit"] == {
        "warmup_days": 180,
        "n_t_minimum": 18,
        "alpaca_first_daily_is_pit_proxy": True,
        "universe_source_sha256": (
            "512285ebf67bb49dc1844d7c76dda4ea09dc19cbfb5968d32caee4a688cae8b2"
        ),
    }


def test_policy_component_literal_content():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["policy"] == {
        "walk_forward": {
            "oos_folds": 8,
            "oos_days": 28,
            "train_days": 365,
            "embargo_days": 7,
            "roll_days": 28,
        },
        "min_modeled_entries_per_fold": 5,
        "dry_count_gate": "pnl_blind_dry_count_before_oos_unmask",
        "no_threshold_relaxation": True,
        "no_post_pnl_config_addition": True,
        "order_type": "LIMIT_ONLY",
        "economic_execution": "TAKER_TAKER",
        "min_broker_order_usd": 10,
    }


def test_benchmark_component_literal_content():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["benchmark"] == {
        "benchmarks": ["BTC", "ETH", "cash", "pit_equal_weight"],
        "role": "reported_alongside_not_a_pass_authority",
    }


def test_mdd_component_literal_content_including_hard_gate():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["mdd"] == {
        "definition": "peak_to_trough_oos_window",
        "max_oos_dd_pct": 20,
        "hard_gate": True,
    }


def test_frozen_config_component_ap_a1_literal_content():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["frozen_config"] == {
        "evaluation": "daily_00:05_utc",
        "initial_equity_usd": 2000,
        "base_slot_usd": 62.50,
        "min_strategy_target_usd": 25,
        "gross_ev_floor_bp": 180,
        "e120_floor_bp": 60,
        "annual_stress_cost_cap_pct": 6,
        "fixed_tp": "NONE",
        "future_tp_min_bp": 240,
    }


def test_frozen_config_component_ap_a2_literal_content():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a2_configs()[0]
    components = m.build_components_for_config(config, seal)
    assert components["frozen_config"] == {
        "evaluation": "weekly_monday_00:05_utc",
        "initial_equity_usd": 2000,
        "min_strategy_target_usd": 25,
        "gross_ev_floor_bp": 200,
        "e120_floor_bp": 80,
        "annual_stress_cost_cap_pct": 18,
        "turnover_band": [0.2083, 0.2885],
        "fixed_tp": "NONE",
        "future_tp_min_bp": 240,
    }


def test_universe_component_literal_symbols_and_quote_mode_for_all_20():
    """AC6 requires ``quote_mode`` in the identity universe component --
    dropping it survived because prior coverage only checked
    ``sealed_effective_n`` and a single exclusion. Pins the full 20-symbol
    list and every quote_mode literally."""
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    components = m.build_components_for_config(config, seal)
    u = components["universe"]
    assert u["sealed_effective_n"] == 20
    assert u["excluded_symbols"] == ["PEPE", "SHIB"]
    assert u["exclusion_reason"] == "basis_red_grade_hv_p95_ge_28bp"
    assert u["exclusion_authority"] == "operator_decision_2026-07-25"
    assert u["symbols"] == [
        "AAVE/USD",
        "AVAX/USD",
        "BAT/USD",
        "BCH/USD",
        "BTC/USD",
        "CRV/USD",
        "DOGE/USD",
        "DOT/USD",
        "ETH/USD",
        "GRT/USD",
        "LINK/USD",
        "LTC/USD",
        "SKY/USD",
        "SOL/USD",
        "SUSHI/USD",
        "TRUMP/USD",
        "UNI/USD",
        "XRP/USD",
        "XTZ/USD",
        "YFI/USD",
    ]
    assert u["quote_mode"] == {
        "AAVE/USD": "SYNTH_USDC",
        "AVAX/USD": "USDC",
        "BAT/USD": "USDT_PROXY",
        "BCH/USD": "USDC",
        "BTC/USD": "USDC",
        "CRV/USD": "USDC",
        "DOGE/USD": "USDC",
        "DOT/USD": "USDC",
        "ETH/USD": "USDC",
        "GRT/USD": "SYNTH_USDC",
        "LINK/USD": "USDC",
        "LTC/USD": "USDC",
        "SKY/USD": "SYNTH_USDC",
        "SOL/USD": "USDC",
        "SUSHI/USD": "SYNTH_USDC",
        "TRUMP/USD": "SYNTH_USDC",
        "UNI/USD": "USDC",
        "XRP/USD": "USDC",
        "XTZ/USD": "SYNTH_USDC",
        "YFI/USD": "USDT_PROXY",
    }


def test_ap_a1_formula_spec_source_text_is_the_literal_preregistration_text():
    """A 10x entry-threshold relaxation (D >= +0.005 -> D >= +0.05) in this
    text survived because the ``code`` identity component only hashes it --
    nothing pinned the literal spec text itself."""
    import identity as m

    prov = m.default_formula_provenance("AP-A1")
    assert prov.source_text == (
        "R[i,m,t] = C[i,t]/C[i,t-m] - 1; D[i,f,s,t] = EMA_f(C)/EMA_s(C) - 1. "
        "entry: flat AND D >= +0.005 AND R > 0. "
        "exit: long AND (D <= -0.005 OR R <= 0). "
        "hysteresis: -0.005 < D < +0.005 keeps existing state."
    )


def test_ap_a2_formula_spec_source_text_is_the_literal_preregistration_text():
    import identity as m

    prov = m.default_formula_provenance("AP-A2")
    assert prov.source_text == (
        "Score[i,L,t] = C[i,t]/C[i,t-L] - 1, descending sort, ties by symbol "
        "ascending. order: (1) held with Score<=0 OR rank>k+buffer -> exit "
        "queued; (2) exits submitted first; (3) after exits, from remaining "
        "cash buy Score>0 unheld symbols in rank order; (4) stop once k held; "
        "(5) held symbols with rank<=k+buffer AND Score>0 -> no trade (hold); "
        "(6) fewer than k positive-Score symbols -> remainder stays cash. no "
        "restoration of existing holdings' weight (rank buffer suppresses "
        "turnover)."
    )


# --------------------------------------------------------------------------- #
# ROB-1060 H2-lock item 9: supersession must preserve every sealed component  #
# except `code`.                                                              #
# --------------------------------------------------------------------------- #


def test_supersession_allows_identical_sealed_components_with_only_code_differing():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    parent = m.build_components_for_config(config, seal)
    child = dict(parent)
    child["code"] = {"kind": "real_implementation", "source_sha256": "1" * 64}
    m.assert_supersession_preserves_sealed_components(
        child_components=child, parent_components=parent
    )  # must not raise


def test_supersession_rejects_a_changed_sealed_component_even_if_code_also_changed():
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    parent = m.build_components_for_config(config, seal)
    child = dict(parent)
    child["code"] = {"kind": "real_implementation", "source_sha256": "1" * 64}
    child["mdd"] = {**parent["mdd"], "max_oos_dd_pct": 50}
    with pytest.raises(m.SupersessionSealedComponentDivergenceError, match="mdd"):
        m.assert_supersession_preserves_sealed_components(
            child_components=child, parent_components=parent
        )


def test_supersession_rejects_divergence_in_any_single_sealed_component():
    """Parametrized-by-hand sweep: tampering ANY one non-code component must
    be caught, not just 'mdd'."""
    import configs as cfg
    import identity as m
    import params as prm

    seal = prm.build_sealed_params()
    config = cfg.build_ap_a1_configs()[0]
    parent = m.build_components_for_config(config, seal)
    from research_contracts.canonical_hash import IDENTITY_COMPONENTS

    # ROB-1060 H2-lock adversarial-verification note: this sweep loops
    # `IDENTITY_COMPONENTS`, the SAME tuple `assert_supersession_preserves_
    # sealed_components` itself iterates -- a lockstep shrink of that tuple
    # (dropping a component) would silently shrink this sweep too, with the
    # loop-count check below (a hardcoded literal, not the constant under
    # test) as an independent, self-standing guard against exactly that.
    assert len(IDENTITY_COMPONENTS) == 11
    for name in IDENTITY_COMPONENTS:
        if name == "code":
            continue
        child = dict(parent)
        child["code"] = {"kind": "real_implementation", "source_sha256": "2" * 64}
        child[name] = {"__tampered__": True}
        with pytest.raises(m.SupersessionSealedComponentDivergenceError, match=name):
            m.assert_supersession_preserves_sealed_components(
                child_components=child, parent_components=parent
            )
