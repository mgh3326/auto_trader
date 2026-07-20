"""ROB-981 (ROB-974 R2 H6-A) CP1 — exact-48 canonical identity kernel.

Pure, generic, predecessor-port-injected (mirrors rob946_campaign_identity's
discipline for the OLD 24-row S1/S2 campaign, generalized to the NEW 48-row
S3/S4 campaign). Every port/row fixture in this file is explicitly marked
``provenance="fixture_identity"`` -- this module has no production H2/H3
adapter yet (CP8-only); a fixture can never silently stand in for a real
manifest.
"""

from __future__ import annotations

import copy

import pytest
import rob974_h6a_identity as h6a


def _hex64(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode()).hexdigest()


def _row(
    row_id: str, *, hypothesis: str, authority_label: str, **params
) -> h6a.CampaignConfigRow:
    return h6a.CampaignConfigRow(
        row_id=row_id,
        params=params,
        hypothesis=hypothesis,
        authority_label=authority_label,
        provenance="fixture_identity",
    )


_S3_HYPOTHESIS = "fixture S3 hypothesis line\n"
_S4_HYPOTHESIS = "fixture S4 hypothesis line\n"


def _s3_rows() -> list[h6a.CampaignConfigRow]:
    return [
        _row(
            f"S3-{i:02d}",
            hypothesis=_S3_HYPOTHESIS,
            authority_label="baseline",
            L=12,
            q_min=0.35,
        )
        for i in range(24)
    ]


def _s4_rows() -> list[h6a.CampaignConfigRow]:
    return [
        _row(
            f"S4-{i:02d}",
            hypothesis=_S4_HYPOTHESIS,
            authority_label="baseline",
            W=180,
            z_entry=1.8,
        )
        for i in range(24)
    ]


def _all_48_rows() -> list[h6a.CampaignConfigRow]:
    return _s3_rows() + _s4_rows()


def _shared_components() -> dict:
    return {
        "dataset_manifest": {
            "h1_lineage_hash": _hex64("h1-lineage"),
            "parent_corpus_hash": _hex64("parent-corpus"),
        },
        "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
        "benchmark": {"kind": "none_explicit_sentinel"},
        "mdd": {"h2_engine_contract_hash": _hex64("h2-engine"), "role": "report_only"},
    }


def _strategy_contract(
    slug: str, *, contract_hash: str | None = None
) -> h6a.StrategyContractProvenance:
    return h6a.StrategyContractProvenance(
        strategy_slug=slug,
        strategy_key=f"ROB974-{slug}-FIXTURE",
        strategy_version=f"{slug.lower()}-v1",
        contract_hash=contract_hash or _hex64(f"{slug}-contract"),
        contract_key=f"{slug}-fixture-key",
        provenance="fixture_identity",
    )


def _contracts() -> dict[str, h6a.StrategyContractProvenance]:
    return {"S3": _strategy_contract("S3"), "S4": _strategy_contract("S4")}


def _pit_component(slug: str) -> dict:
    return {"folds": 8, "embargo_hours": 3, "s4_tri_state": "historical_only"}


def _frozen_config_component(slug: str) -> dict:
    return {"h3_manifest_contract_hash": _hex64(f"{slug}-h3-manifest")}


def _policy_component(slug: str) -> dict:
    return {
        "selection_authority": "fixture_selection_authority",
        "path_membership": ["base13", "primary_stress17", "upward_stress22"],
        "pair_order": ["XRP-DOGE", "XRP-SOL", "DOGE-SOL"],
    }


def _cost_component(slug: str) -> dict:
    return {"pbo_contract": {"primary_stress_bps": 17, "slices": 4}}


def _build_specs(rows=None, contracts=None) -> tuple:
    rows = rows if rows is not None else _all_48_rows()
    contracts = contracts if contracts is not None else _contracts()
    return h6a.build_campaign_row_specs(
        rows,
        contracts=contracts,
        shared_components=_shared_components(),
        pit_component_by_slug={"S3": _pit_component("S3"), "S4": _pit_component("S4")},
        frozen_config_component_by_slug={
            "S3": _frozen_config_component("S3"),
            "S4": _frozen_config_component("S4"),
        },
        policy_component_by_slug={
            "S3": _policy_component("S3"),
            "S4": _policy_component("S4"),
        },
        cost_component_by_slug={
            "S3": _cost_component("S3"),
            "S4": _cost_component("S4"),
        },
    )


class TestCanonicalOrder:
    def test_canonical_row_order_is_exact_48(self):
        assert h6a.CANONICAL_ROW_ORDER == tuple(
            [f"S3-{i:02d}" for i in range(24)] + [f"S4-{i:02d}" for i in range(24)]
        )
        assert len(h6a.CANONICAL_ROW_ORDER) == 48
        assert len(set(h6a.CANONICAL_ROW_ORDER)) == 48

    def test_specs_are_returned_in_canonical_order(self):
        specs = _build_specs()
        assert tuple(s.row_id for s in specs) == h6a.CANONICAL_ROW_ORDER


class TestRosterCount:
    def test_47_rows_rejected(self):
        rows = _all_48_rows()[:-1]
        with pytest.raises(h6a.RowCountError):
            h6a.validate_campaign_rows(rows)

    def test_49_rows_rejected(self):
        rows = _all_48_rows() + [
            _row("S4-24", hypothesis=_S4_HYPOTHESIS, authority_label="x", W=1)
        ]
        with pytest.raises((h6a.RowCountError, h6a.RowIdError)):
            h6a.validate_campaign_rows(rows)

    def test_exact_48_passes(self):
        h6a.validate_campaign_rows(_all_48_rows())

    def test_missing_row_rejected(self):
        rows = _s3_rows()[1:] + _s4_rows()
        with pytest.raises((h6a.RowCountError, h6a.RowIdError)):
            h6a.validate_campaign_rows(rows)

    def test_missing_row_with_replacement_padding_rejected(self):
        # Same total count (48) but one S3 id is missing while another S3 id
        # is duplicated in its place -- this must be caught by shape, not count.
        rows = _s3_rows()
        rows[0] = rows[1]
        rows = rows + _s4_rows()
        with pytest.raises(h6a.RowIdError):
            h6a.validate_campaign_rows(rows)

    def test_duplicate_row_rejected(self):
        rows = _all_48_rows()
        rows[1] = rows[0]
        with pytest.raises(h6a.RowIdError):
            h6a.validate_campaign_rows(rows)

    def test_cross_strategy_overlap_rejected(self):
        # S4 row masquerading with an S3 id shape collision (24 S3 + 24 S4,
        # but one S3 id duplicated in place of a legitimate S4 id).
        rows = _s3_rows() + _s3_rows()[:24]
        with pytest.raises(h6a.RowIdError):
            h6a.validate_campaign_rows(rows)

    def test_malformed_row_id_rejected(self):
        rows = _all_48_rows()
        rows[0] = _row("S3-99", hypothesis=_S3_HYPOTHESIS, authority_label="x", L=1)
        with pytest.raises(h6a.RowIdError):
            h6a.validate_campaign_rows(rows)


class TestSameStrategyComponentsIdentical:
    def test_within_strategy_only_params_may_differ(self):
        specs = _build_specs()
        h6a.validate_same_strategy_components_identical(specs)

    def test_hypothesis_drift_within_strategy_rejected(self):
        rows = _s3_rows()
        rows[5] = _row(
            "S3-05",
            hypothesis="a different hypothesis\n",
            authority_label="baseline",
            L=12,
        )
        rows = rows + _s4_rows()
        with pytest.raises(h6a.ComponentDriftError):
            _build_specs(rows=rows)

    def test_frozen_config_drift_within_strategy_rejected(self):
        specs = list(_build_specs())
        # Corrupt one S3 row's frozen_config component in place.
        drifted = specs[3]
        mutated_components = dict(drifted.components)
        mutated_components["frozen_config"] = {"tampered": True}
        specs[3] = h6a.H6ARowSpec(
            row_id=drifted.row_id,
            strategy_key=drifted.strategy_key,
            strategy_version=drifted.strategy_version,
            hypothesis=drifted.hypothesis,
            components=mutated_components,
            provenance=drifted.provenance,
            experiment_id=drifted.experiment_id,
        )
        with pytest.raises(h6a.ComponentDriftError):
            h6a.validate_same_strategy_components_identical(specs)

    def test_params_are_allowed_to_differ(self):
        specs = _build_specs()
        s3_specs = [s for s in specs if s.row_id.startswith("S3")]
        assert s3_specs[0].components["params"] != s3_specs[1].components["params"]


class TestExperimentIdDerivation:
    def test_derivation_is_deterministic_and_unique(self):
        specs = _build_specs()
        ids = [s.experiment_id for s in specs]
        assert len(ids) == 48
        assert len(set(ids)) == 48
        # Re-deriving from the same components reproduces the same id.
        for spec in specs:
            recomputed = h6a.derive_row_experiment_id(
                spec.strategy_key, spec.strategy_version, spec.components
            )
            assert recomputed == spec.experiment_id

    def test_one_ulp_param_mutation_changes_experiment_id(self):
        rows = _s3_rows()
        base_specs = _build_specs(rows=rows + _s4_rows())
        mutated_rows = copy.deepcopy(rows)
        mutated_rows[0] = _row(
            "S3-00",
            hypothesis=_S3_HYPOTHESIS,
            authority_label="baseline",
            L=12,
            q_min=0.35 + 2**-52,  # one ULP nudge
        )
        mutated_rows = mutated_rows + _s4_rows()
        mutated_specs = _build_specs(rows=mutated_rows)
        assert base_specs[0].experiment_id != mutated_specs[0].experiment_id

    def test_envelope_id_match_accepted(self):
        specs = _build_specs()
        spec = specs[0]
        h6a.verify_row_experiment_id(spec, envelope_experiment_id=spec.experiment_id)

    def test_envelope_id_mismatch_rejected(self):
        specs = _build_specs()
        spec = specs[0]
        with pytest.raises(h6a.EnvelopeIdMismatchError):
            h6a.verify_row_experiment_id(spec, envelope_experiment_id=_hex64("forged"))

    def test_arbitrary_run_id_never_accepted_as_experiment_id(self):
        specs = _build_specs()
        spec = specs[0]
        with pytest.raises(h6a.EnvelopeIdMismatchError):
            h6a.verify_row_experiment_id(
                spec, envelope_experiment_id="arbitrary-uuid-1234"
            )

    def test_reordered_ids_detected_via_canonical_order_check(self):
        specs = _build_specs()
        shuffled = (specs[1], specs[0]) + tuple(specs[2:])
        with pytest.raises(h6a.RowIdError):
            h6a.assert_specs_in_canonical_order(shuffled)

    def test_stale_source_pin_rejected(self):
        contracts = _contracts()
        contracts["S3"] = h6a.StrategyContractProvenance(
            strategy_slug="S3",
            strategy_key="ROB974-S3-FIXTURE",
            strategy_version="s3-v1",
            contract_hash=_hex64("S3-contract"),
            contract_key="S3-fixture-key",
            expected_contract_hash=_hex64("some-other-stale-value"),
            provenance="fixture_identity",
        )
        with pytest.raises(h6a.StaleSourcePinError):
            _build_specs(contracts=contracts)


class TestTypedCanonicalPassthrough:
    def test_bool_is_not_int_in_identity(self):
        rows = _s3_rows()
        rows[0] = _row(
            "S3-00", hypothesis=_S3_HYPOTHESIS, authority_label="baseline", flag=True
        )
        rows_int = _s3_rows()
        rows_int[0] = _row(
            "S3-00", hypothesis=_S3_HYPOTHESIS, authority_label="baseline", flag=1
        )
        specs_bool = _build_specs(rows=rows + _s4_rows())
        specs_int = _build_specs(rows=rows_int + _s4_rows())
        assert specs_bool[0].experiment_id != specs_int[0].experiment_id

    def test_nan_rejected(self):
        rows = _s3_rows()
        rows[0] = _row(
            "S3-00",
            hypothesis=_S3_HYPOTHESIS,
            authority_label="baseline",
            x=float("nan"),
        )
        with pytest.raises((ValueError, TypeError)):
            _build_specs(rows=rows + _s4_rows())

    def test_mapping_key_order_does_not_change_experiment_id(self):
        rows_a = _s3_rows()
        rows_a[0] = _row(
            "S3-00", hypothesis=_S3_HYPOTHESIS, authority_label="baseline", a=1, b=2
        )
        rows_b = _s3_rows()
        rows_b[0] = h6a.CampaignConfigRow(
            row_id="S3-00",
            params={"b": 2, "a": 1},
            hypothesis=_S3_HYPOTHESIS,
            authority_label="baseline",
            provenance="fixture_identity",
        )
        specs_a = _build_specs(rows=rows_a + _s4_rows())
        specs_b = _build_specs(rows=rows_b + _s4_rows())
        assert specs_a[0].experiment_id == specs_b[0].experiment_id


class TestFixtureProvenanceIsolation:
    def test_fixture_rows_are_marked(self):
        for row in _all_48_rows():
            assert row.provenance == "fixture_identity"

    def test_non_fixture_provenance_literal_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            h6a.CampaignConfigRow(
                row_id="S3-00",
                params={},
                hypothesis=_S3_HYPOTHESIS,
                authority_label="baseline",
                provenance="production",  # no production builder exists yet (CP8-only)
            )

    def test_no_production_builder_exported(self):
        assert not hasattr(h6a, "build_production_campaign_row_specs")
