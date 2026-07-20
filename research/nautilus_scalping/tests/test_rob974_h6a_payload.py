"""ROB-981 (ROB-974 R2 H6-A) CP2 -- canonical campaign payload, plan purity,
and immutable seals.

Pure -- no DB/network/app/broker/random/current-time. Builds ONE canonical
envelope over the 48 row specs (CP1) plus campaign-wide policy (folds/
embargo/horizons/selection authority/base13-primary17-upward22 path
membership/funding policy/gates+bins/full-window PBO contract/S4 pair
order+tri-state), and derives a deterministic ``full_campaign_hash`` +
primary ``campaign_run_id``. Every fixture below is fixture-marked; the
"production_plan" mode is exercised only with synthetic, explicitly
fixture-marked source pins -- this checkpoint never claims a real empirical
full-campaign identity (that is CP8/H4 territory, deferred by design).
"""

from __future__ import annotations

import hashlib

import pytest
import rob974_h6a_identity as h6a_identity
import rob974_h6a_payload as h6a


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _row(row_id: str, *, hypothesis: str, **params) -> h6a_identity.CampaignConfigRow:
    return h6a_identity.CampaignConfigRow(
        row_id=row_id,
        params=params,
        hypothesis=hypothesis,
        authority_label="baseline",
        provenance="fixture_identity",
    )


_S3_HYPOTHESIS = "fixture S3 hypothesis line\n"
_S4_HYPOTHESIS = "fixture S4 hypothesis line\n"


def _rows() -> list[h6a_identity.CampaignConfigRow]:
    return [
        _row(f"S3-{i:02d}", hypothesis=_S3_HYPOTHESIS, L=12, q_min=0.35)
        for i in range(24)
    ] + [
        _row(f"S4-{i:02d}", hypothesis=_S4_HYPOTHESIS, W=180, z_entry=1.8)
        for i in range(24)
    ]


def _contracts() -> dict[str, h6a_identity.StrategyContractProvenance]:
    return {
        slug: h6a_identity.StrategyContractProvenance(
            strategy_slug=slug,
            strategy_key=f"ROB974-{slug}-FIXTURE",
            strategy_version=f"{slug.lower()}-v1",
            contract_hash=_hex64(f"{slug}-contract"),
            contract_key=f"{slug}-fixture-key",
            provenance="fixture_identity",
        )
        for slug in ("S3", "S4")
    }


def _row_specs() -> tuple:
    shared = {
        "dataset_manifest": {"h1_lineage_hash": _hex64("h1-lineage")},
        "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
        "benchmark": {"kind": "none_explicit_sentinel"},
        "mdd": {"h2_engine_contract_hash": _hex64("h2-engine")},
    }
    per_slug = lambda key: {  # noqa: E731
        "S3": {key: f"S3-{key}"},
        "S4": {key: f"S4-{key}"},
    }
    return h6a_identity.build_campaign_row_specs(
        _rows(),
        contracts=_contracts(),
        shared_components=shared,
        pit_component_by_slug=per_slug("pit"),
        frozen_config_component_by_slug=per_slug("frozen_config"),
        policy_component_by_slug=per_slug("policy"),
        cost_component_by_slug=per_slug("cost"),
    )


def _campaign_policy(**overrides) -> h6a.CampaignPolicy:
    base = {
        "folds": tuple({"fold_id": f"fold-{i:02d}"} for i in range(8)),
        "embargo_hours": 3,
        "horizons": {"s3_max_hold_bars": 12, "s4_max_hold_bars": 9},
        "selection_authority": "fixture_selection_authority",
        "path_membership": {
            "base13": {"cost_bps": 13},
            "primary_stress17": {"cost_bps": 17},
            "upward_stress22": {"cost_bps": 22},
        },
        "funding_policy": {"gate": "post_arbitration_pre_entry"},
        "gates_bins": {"vol_percentile": [20, 90]},
        "pbo_contract": {"primary_stress_bps": 17, "window": "24x365", "slices": 4},
        "pair_order": ("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
        "s4_tri_state_policy": "historical_only_pair_exec_not_evaluated",
    }
    base.update(overrides)
    return h6a.CampaignPolicy(**base)


def _parent_corpus() -> dict:
    return {
        "content_hash": _hex64("parent-corpus"),
        "universe": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"],
    }


def _fixture_pins() -> h6a.RequiredSourcePins:
    return h6a.RequiredSourcePins(
        feature_source_sha256=_hex64("h1-feature-source"),
        engine_source_sha256=_hex64("h2-engine-source"),
        runner_source_sha256=_hex64("fixture-h4-runner-source"),
        pbo_implementation_sha256=_hex64("fixture-pbo-impl-source"),
    )


def _build_envelope(**overrides) -> h6a.H6ACampaignEnvelope:
    kwargs = {
        "row_specs": _row_specs(),
        "parent_corpus": _parent_corpus(),
        "campaign_policy": _campaign_policy(),
        "source_pins": h6a.EMPTY_SOURCE_PINS,
        "mode": "fixture_plan",
    }
    kwargs.update(overrides)
    return h6a.build_campaign_envelope(**kwargs)


class TestDeterminism:
    def test_same_input_same_hash(self):
        a = _build_envelope()
        b = _build_envelope()
        assert a.full_campaign_hash() == b.full_campaign_hash()

    def test_hash_is_hex64(self):
        env = _build_envelope()
        h = env.full_campaign_hash()
        assert isinstance(h, str) and len(h) == 64
        int(h, 16)  # does not raise


class TestMutationsChangeHash:
    def test_one_ulp_row_param_mutation_changes_hash(self):
        base = _build_envelope()
        rows = _rows()
        rows[0] = _row("S3-00", hypothesis=_S3_HYPOTHESIS, L=12, q_min=0.35 + 2**-52)
        shared = {
            "dataset_manifest": {"h1_lineage_hash": _hex64("h1-lineage")},
            "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
            "benchmark": {"kind": "none_explicit_sentinel"},
            "mdd": {"h2_engine_contract_hash": _hex64("h2-engine")},
        }
        per_slug = lambda key: {"S3": {key: f"S3-{key}"}, "S4": {key: f"S4-{key}"}}  # noqa: E731
        mutated_specs = h6a_identity.build_campaign_row_specs(
            rows,
            contracts=_contracts(),
            shared_components=shared,
            pit_component_by_slug=per_slug("pit"),
            frozen_config_component_by_slug=per_slug("frozen_config"),
            policy_component_by_slug=per_slug("policy"),
            cost_component_by_slug=per_slug("cost"),
        )
        mutated = _build_envelope(row_specs=mutated_specs)
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_fold_schedule_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(
                folds=tuple({"fold_id": f"fold-{i:02d}"} for i in range(7))
            )
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_embargo_hours_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(campaign_policy=_campaign_policy(embargo_hours=4))
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_selection_authority_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(selection_authority="different_authority")
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_path_membership_gate_mutation_changes_hash(self):
        base = _build_envelope()
        policy = _campaign_policy()
        mutated_membership = dict(policy.path_membership)
        mutated_membership["upward_stress22"] = {"cost_bps": 999}
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(path_membership=mutated_membership)
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_gates_bins_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(gates_bins={"vol_percentile": [10, 90]})
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_pbo_contract_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(
                pbo_contract={"primary_stress_bps": 17, "window": "24x365", "slices": 5}
            )
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_pair_order_reorder_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(
                pair_order=("XRP-SOL", "XRP-DOGE", "DOGE-SOL")
            )
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_s4_tri_state_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            campaign_policy=_campaign_policy(s4_tri_state_policy="something_else")
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()

    def test_parent_corpus_mutation_changes_hash(self):
        base = _build_envelope()
        mutated = _build_envelope(
            parent_corpus={**_parent_corpus(), "content_hash": _hex64("x")}
        )
        assert base.full_campaign_hash() != mutated.full_campaign_hash()


class TestIrrelevantPermutationInvariance:
    def test_dict_key_order_does_not_change_hash(self):
        policy_a = _campaign_policy()
        policy_b = h6a.CampaignPolicy(
            s4_tri_state_policy=policy_a.s4_tri_state_policy,
            pair_order=policy_a.pair_order,
            pbo_contract=dict(reversed(list(policy_a.pbo_contract.items()))),
            gates_bins=policy_a.gates_bins,
            funding_policy=policy_a.funding_policy,
            path_membership=policy_a.path_membership,
            selection_authority=policy_a.selection_authority,
            horizons=policy_a.horizons,
            embargo_hours=policy_a.embargo_hours,
            folds=policy_a.folds,
        )
        a = _build_envelope(campaign_policy=policy_a)
        b = _build_envelope(campaign_policy=policy_b)
        assert a.full_campaign_hash() == b.full_campaign_hash()


class TestRowOrderIsNeverSilentlyReordered:
    def test_shuffled_row_specs_rejected(self):
        specs = _row_specs()
        shuffled = (specs[1], specs[0]) + tuple(specs[2:])
        with pytest.raises(h6a_identity.RowIdError):
            _build_envelope(row_specs=shuffled)


class TestPrimaryRunId:
    def test_deterministic_and_derived_from_hash(self):
        env = _build_envelope()
        run_id_a = h6a.derive_primary_run_id(env.full_campaign_hash())
        run_id_b = h6a.derive_primary_run_id(env.full_campaign_hash())
        assert run_id_a == run_id_b
        assert run_id_a.startswith("rob974h6a-")

    def test_run_id_changes_when_hash_changes(self):
        env_a = _build_envelope()
        env_b = _build_envelope(campaign_policy=_campaign_policy(embargo_hours=99))
        run_id_a = h6a.derive_primary_run_id(env_a.full_campaign_hash())
        run_id_b = h6a.derive_primary_run_id(env_b.full_campaign_hash())
        assert run_id_a != run_id_b

    def test_arbitrary_run_id_is_never_accepted_as_derivation(self):
        env = _build_envelope()
        real = h6a.derive_primary_run_id(env.full_campaign_hash())
        assert real != "arbitrary-uuid-1234"
        with pytest.raises(h6a.RunIdDerivationError):
            h6a.verify_primary_run_id(
                "arbitrary-uuid-1234", full_campaign_hash=env.full_campaign_hash()
            )


class TestImmutability:
    def test_nested_policy_mutation_raises(self):
        env = _build_envelope()
        with pytest.raises(TypeError):
            env.campaign_policy_frozen["gates_bins"]["vol_percentile"] = [1, 2]

    def test_to_dict_mutation_does_not_affect_hash(self):
        env = _build_envelope()
        before = env.full_campaign_hash()
        d = env.to_dict()
        d["campaign_policy"]["gates_bins"] = {"tampered": True}
        after = env.full_campaign_hash()
        assert before == after


class TestModeIsMetadataNotSemanticIdentity:
    def test_fixture_and_production_mode_same_content_same_hash(self):
        fixture_env = _build_envelope(mode="fixture_plan", source_pins=_fixture_pins())
        production_env = _build_envelope(
            mode="production_plan", source_pins=_fixture_pins()
        )
        assert fixture_env.full_campaign_hash() == production_env.full_campaign_hash()


class TestProductionSourcePinGate:
    def test_fixture_mode_does_not_require_pins(self):
        _build_envelope(mode="fixture_plan", source_pins=h6a.EMPTY_SOURCE_PINS)

    def test_production_mode_requires_all_pins_present(self):
        with pytest.raises(h6a.MissingSourcePinError):
            _build_envelope(mode="production_plan", source_pins=h6a.EMPTY_SOURCE_PINS)

    def test_production_mode_rejects_single_missing_pin(self):
        pins = h6a.RequiredSourcePins(
            feature_source_sha256=_hex64("h1"),
            engine_source_sha256=_hex64("h2"),
            runner_source_sha256=None,
            pbo_implementation_sha256=_hex64("pbo"),
        )
        with pytest.raises(h6a.MissingSourcePinError):
            _build_envelope(mode="production_plan", source_pins=pins)

    def test_production_mode_rejects_all_zero_placeholder_pin(self):
        pins = h6a.RequiredSourcePins(
            feature_source_sha256=_hex64("h1"),
            engine_source_sha256=_hex64("h2"),
            runner_source_sha256="0" * 64,
            pbo_implementation_sha256=_hex64("pbo"),
        )
        with pytest.raises(h6a.MissingSourcePinError):
            _build_envelope(mode="production_plan", source_pins=pins)

    def test_production_mode_rejects_non_hex_pin(self):
        pins = h6a.RequiredSourcePins(
            feature_source_sha256=_hex64("h1"),
            engine_source_sha256=_hex64("h2"),
            runner_source_sha256="not-a-hash",
            pbo_implementation_sha256=_hex64("pbo"),
        )
        with pytest.raises(h6a.MissingSourcePinError):
            _build_envelope(mode="production_plan", source_pins=pins)

    def test_production_mode_accepts_valid_pins(self):
        env = _build_envelope(mode="production_plan", source_pins=_fixture_pins())
        assert env.mode == "production_plan"

    def test_pin_gate_runs_before_identity_derivation(self):
        # A malformed pin set must fail BEFORE full_campaign_hash is ever
        # computable -- the constructor itself raises, not a later call.
        pins = h6a.RequiredSourcePins(
            feature_source_sha256=None,
            engine_source_sha256=None,
            runner_source_sha256=None,
            pbo_implementation_sha256=None,
        )
        with pytest.raises(h6a.MissingSourcePinError):
            _build_envelope(mode="production_plan", source_pins=pins)


class TestPurePlanCall:
    def test_repeated_builds_are_side_effect_free(self):
        hashes = {_build_envelope().full_campaign_hash() for _ in range(5)}
        assert len(hashes) == 1
