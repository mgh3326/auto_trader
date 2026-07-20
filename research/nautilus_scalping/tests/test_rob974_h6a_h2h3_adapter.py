"""ROB-981 (ROB-974 R2 H6-A) CP8 -- production H2/H3 adapter.

Proves ``rob974_h6a_h2h3_adapter`` independently reconstructs/verifies the
REAL merged H2 (ROB-979)/H3 (ROB-980) production surfaces before ever
constructing a provenance="production" row/contract, and that any drift in
those real predecessors (roster row, contract hash, research-document SHA,
H2 engine seam) fails closed as ``ContractDriftError`` rather than silently
building a substitute identity.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
import rob974_h3_manifest as h3_manifest
import rob974_h6a_h2h3_adapter as adapter
import rob974_h6a_identity as h6a


class TestVerifyH2H3Contract:
    def test_verify_h2h3_contract_passes_against_real_merged_predecessors(self):
        adapter.verify_h2h3_contract()  # must not raise

    def test_research_document_sha_pinned_to_authority_value(self):
        assert (
            h3_manifest.RESEARCH_DOCUMENT_SHA256
            == adapter.RESEARCH_DOCUMENT_SHA256
            == "2f535196cf0f0a03292e8f4c1806794ffbf8282ba7b5c3f564a930763577a009"
        )

    def test_research_document_sha_drift_fails_closed(self, monkeypatch):
        # This exercises the full defense-in-depth chain -- both this
        # adapter's own explicit pin AND H3's own validate_contract_seals
        # independently reject a bare RESEARCH_DOCUMENT_SHA256 tamper (the
        # module value no longer agrees with either StrategyContract's own
        # frozen source_research_sha256 field). Layered, not redundant: see
        # TestVerifyH2H3ContractLayerIsolation below for each layer alone.
        monkeypatch.setattr(
            h3_manifest, "RESEARCH_DOCUMENT_SHA256", hashlib.sha256(b"tampered").hexdigest()
        )
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()

    def test_contract_hash_drift_fails_closed(self, monkeypatch):
        drifted = dataclasses.replace(
            h3_manifest.S3_STRATEGY_CONTRACT,
            contract_hash=hashlib.sha256(b"tampered-contract").hexdigest(),
        )
        monkeypatch.setattr(h3_manifest, "S3_STRATEGY_CONTRACT", drifted)
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()

    def test_roster_row_mutation_fails_closed(self, monkeypatch):
        mutated_first = dataclasses.replace(h3_manifest.FROZEN_S3_CONFIGS[0], k_SL=9.99)
        mutated_roster = (mutated_first,) + h3_manifest.FROZEN_H3_ROSTER[1:]
        monkeypatch.setattr(h3_manifest, "FROZEN_H3_ROSTER", mutated_roster)
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()

    def test_h2_engine_max_hold_bars_drift_fails_closed(self, monkeypatch):
        import rob974_h2_s3_engine as h2_s3_engine

        monkeypatch.setattr(h2_s3_engine, "MAX_HOLD_BARS", 999)
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()

    def test_h2_signature_drift_fails_closed(self, monkeypatch):
        import rob974_h3_h2_adapter as h3_h2_adapter

        def _drifted_verify():
            raise h3_h2_adapter.ContractDriftError("simulated drift")

        monkeypatch.setattr(h3_h2_adapter, "verify_h2_contract", _drifted_verify)
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()


class TestVerifyH2H3ContractLayerIsolation:
    """Fully self-consistent H3-internal state (own SHA + both contracts'
    source_research_sha256 + recomputed contract_hash all agree with EACH
    OTHER) but disagreeing with this adapter's own hardcoded authority pin --
    isolates that THIS adapter's explicit pin, not H3's internal self-check,
    is what refuses a coordinated/self-consistent tamper."""

    def test_fully_self_consistent_but_wrong_research_sha_still_fails_closed(
        self, monkeypatch
    ):
        tampered = hashlib.sha256(b"coordinated-self-consistent-tamper").hexdigest()
        monkeypatch.setattr(h3_manifest, "RESEARCH_DOCUMENT_SHA256", tampered)
        for slug, attr in (("S3", "S3_STRATEGY_CONTRACT"), ("S4", "S4_STRATEGY_CONTRACT")):
            recomputed_hash = h3_manifest.hash_contract_payload(
                h3_manifest.strategy_contract_payload(slug)
            )
            monkeypatch.setattr(
                h3_manifest,
                attr,
                dataclasses.replace(
                    getattr(h3_manifest, attr),
                    source_research_sha256=tampered,
                    contract_hash=recomputed_hash,
                ),
            )
        # H3's own validate_contract_seals passes (everything agrees
        # internally); this adapter's own pin against the ORIGINAL approved
        # authority SHA is the only thing left that can catch this.
        h3_manifest.validate_contract_seals(
            h3_manifest.S3_STRATEGY_CONTRACT, h3_manifest.S4_STRATEGY_CONTRACT
        )
        with pytest.raises(adapter.ContractDriftError):
            adapter.verify_h2h3_contract()


class TestBuildProductionRows:
    def test_returns_exactly_48_production_rows(self):
        rows = adapter.build_production_rows()
        assert len(rows) == 48
        assert all(row.provenance == "production" for row in rows)

    def test_row_ids_are_exactly_canonical_order(self):
        rows = adapter.build_production_rows()
        assert tuple(row.row_id for row in rows) == h6a.CANONICAL_ROW_ORDER

    def test_s3_rows_carry_s3_param_fields(self):
        rows = adapter.build_production_rows()
        s3_row = next(row for row in rows if row.row_id == "S3-00")
        assert set(s3_row.params) == {"L", "q_min", "ER_min", "k_SL", "R_TP", "design_type"}

    def test_s4_rows_carry_s4_param_fields(self):
        rows = adapter.build_production_rows()
        s4_row = next(row for row in rows if row.row_id == "S4-00")
        assert set(s4_row.params) == {"W", "z_entry", "d_min_bp", "k_SL", "R_TP", "design_type"}

    def test_hypothesis_matches_the_real_h3_hypothesis_text(self):
        rows = adapter.build_production_rows()
        s3_row = next(row for row in rows if row.row_id == "S3-00")
        assert s3_row.hypothesis == h3_manifest.S3_HYPOTHESIS_UTF8.decode("utf-8")

    def test_drift_before_row_build_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            h3_manifest, "RESEARCH_DOCUMENT_SHA256", hashlib.sha256(b"tampered").hexdigest()
        )
        with pytest.raises(adapter.ContractDriftError):
            adapter.build_production_rows()


class TestBuildProductionContracts:
    def test_returns_s3_and_s4_production_contracts(self):
        contracts = adapter.build_production_contracts()
        assert set(contracts) == {"S3", "S4"}
        assert contracts["S3"].provenance == "production"
        assert contracts["S4"].provenance == "production"

    def test_contract_hash_matches_independently_recomputed_value(self):
        contracts = adapter.build_production_contracts()
        recomputed = h3_manifest.hash_contract_payload(
            h3_manifest.strategy_contract_payload("S3")
        )
        assert contracts["S3"].contract_hash == recomputed
        assert contracts["S3"].verified_contract_hash() == recomputed

    def test_strategy_keys_are_the_real_h3_contract_keys(self):
        contracts = adapter.build_production_contracts()
        assert contracts["S3"].strategy_key == "rob974.s3.rpt-4h"
        assert contracts["S4"].strategy_key == "rob974.s4.brc-4h"

    def test_stale_pin_raises_if_declared_hash_ever_drifts_from_recomputed(
        self, monkeypatch
    ):
        drifted = dataclasses.replace(
            h3_manifest.S3_STRATEGY_CONTRACT,
            contract_hash=hashlib.sha256(b"tampered-contract").hexdigest(),
        )
        monkeypatch.setattr(h3_manifest, "S3_STRATEGY_CONTRACT", drifted)
        with pytest.raises(adapter.ContractDriftError):
            adapter.build_production_contracts()


_SYNTHETIC_SUFFIX = "SYNTHETIC-NOT-REAL-EMPIRICAL-IDENTITY"


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _synthetic_components() -> dict:
    """Explicitly SYNTHETIC-marked stand-ins for the H1/H4/PBO-sourced shared
    components -- proves the H2/H3-sourced wiring end to end WITHOUT ever
    claiming this is a real empirical full-campaign identity (H4 is a
    separate, still-open predecessor -- see
    PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS)."""
    return {
        "dataset_manifest": {
            "h1_lineage_hash": _hex64(f"h1-lineage-{_SYNTHETIC_SUFFIX}"),
            "parent_corpus_hash": _hex64(f"parent-corpus-{_SYNTHETIC_SUFFIX}"),
        },
        "universe": {"symbols": list(h3_manifest.SYMBOLS)},
        "benchmark": {"kind": "none_explicit_sentinel"},
        "mdd": {
            "h2_engine_contract_hash": _hex64(f"h2-engine-{_SYNTHETIC_SUFFIX}"),
            "role": "report_only",
        },
    }


def _synthetic_pit(slug: str) -> dict:
    return {"folds": 8, "embargo_hours": 3, "s4_tri_state": "historical_only"}


def _synthetic_frozen_config(slug: str) -> dict:
    return {"h3_manifest_contract_hash": _hex64(f"{slug}-{_SYNTHETIC_SUFFIX}")}


def _synthetic_policy(slug: str) -> dict:
    return {
        "selection_authority": f"{_SYNTHETIC_SUFFIX}_selection_authority",
        "path_membership": ["base13", "primary_stress17", "upward_stress22"],
        "pair_order": ["XRP-DOGE", "XRP-SOL", "DOGE-SOL"],
    }


def _synthetic_cost(slug: str) -> dict:
    return {"pbo_contract": {"primary_stress_bps": 17, "slices": 4}}


class TestBuildProductionCampaignRowSpecs:
    def _build(self):
        return adapter.build_production_campaign_row_specs(
            shared_components=_synthetic_components(),
            pit_component_by_slug={"S3": _synthetic_pit("S3"), "S4": _synthetic_pit("S4")},
            frozen_config_component_by_slug={
                "S3": _synthetic_frozen_config("S3"),
                "S4": _synthetic_frozen_config("S4"),
            },
            policy_component_by_slug={
                "S3": _synthetic_policy("S3"),
                "S4": _synthetic_policy("S4"),
            },
            cost_component_by_slug={"S3": _synthetic_cost("S3"), "S4": _synthetic_cost("S4")},
        )

    def test_builds_exactly_48_production_specs_in_canonical_order(self):
        specs = self._build()
        assert len(specs) == 48
        assert tuple(spec.row_id for spec in specs) == h6a.CANONICAL_ROW_ORDER
        assert all(spec.provenance == "production" for spec in specs)

    def test_every_experiment_id_independently_reverifies(self):
        specs = self._build()
        for spec in specs:
            h6a.verify_row_experiment_id(spec, envelope_experiment_id=spec.experiment_id)

    def test_experiment_ids_are_all_distinct(self):
        specs = self._build()
        ids = [spec.experiment_id for spec in specs]
        assert len(set(ids)) == 48

    def test_deterministic_across_two_independent_builds(self):
        specs_a = self._build()
        specs_b = self._build()
        assert [s.experiment_id for s in specs_a] == [s.experiment_id for s in specs_b]

    def test_drift_before_any_row_construction_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            h3_manifest, "RESEARCH_DOCUMENT_SHA256", hashlib.sha256(b"tampered").hexdigest()
        )
        with pytest.raises(adapter.ContractDriftError):
            self._build()

    def test_status_constant_is_deferred_until_h4(self):
        assert (
            adapter.PRODUCTION_FULL_CAMPAIGN_IDENTITY_STATUS
            == "DEFERRED_UNTIL_H4_SOURCE_PINS"
        )
