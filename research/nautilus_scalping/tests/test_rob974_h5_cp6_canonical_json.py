"""ROB-983 (H5, CP6) -- canonical semantic JSON and presentation-independent
hash.

Permutation invariance (rebuilding the same semantic content via a
differently-ordered upstream mapping produces byte-identical output),
profit-factor sanitization (``+inf``/``nan`` -> ``null``+reason, never a raw
non-finite float), exact-type rejection (bool-as-float, ``Decimal``,
rounded-string re-entry), the ``NaN``/``Inf`` JSON boundary
(``allow_nan=False``), fixture chronological-key collision detection, and
every semantic-subtree mutation changing the hash.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal

import pytest
from rob974_h5_canonical import (
    ACTUAL_H4_LEDGER_KEY_NOT_EVALUATED,
    StrategyCanonicalInputs,
    build_canonical_scorecard,
    canonical_json_bytes,
    chronological_key,
    hash_canonical_bytes,
    sanitize_bucket,
    sort_trades_chronologically,
)
from rob974_h5_contracts import (
    FOLD_IDS,
    CampaignEnvelope,
    H5InputError,
    H6AAccountingSeal,
    MetricTrade,
)
from rob974_h5_gates import evaluate_common_gates
from rob974_h5_s3 import evaluate_s3_falsification
from rob974_h5_s4 import (
    S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    CampaignDecisionResult,
    evaluate_s4_falsification,
)

_HEX64_A = "a" * 64
_HEX64_B = "b" * 64


def _envelope(**overrides) -> CampaignEnvelope:
    ids = tuple(f"S3-{i:02d}" for i in range(24)) + tuple(
        f"S4-{i:02d}" for i in range(24)
    )
    fields = {
        "full_campaign_hash": _HEX64_A,
        "campaign_run_id": "run-1",
        "parent_corpus_hash": _HEX64_A,
        "parent_projection_hash": _HEX64_A,
        "feature_contract_hash": _HEX64_A,
        "strategy_contract_hashes": {"S3": _HEX64_A, "S4": _HEX64_B},
        "h4_runner_source_hash": _HEX64_A,
        "h4_pbo_source_hash": _HEX64_A,
        "h2_engine_source_hash": _HEX64_A,
        "h3_generator_source_hash": _HEX64_A,
        "run_schema_version": "v1",
        "generator_version": "g1",
        "expected_experiment_ids": ids,
        "h6a_trial_accounting_hash": _HEX64_B,
    }
    fields.update(overrides)
    return CampaignEnvelope(**fields)


def _seal(**overrides) -> H6AAccountingSeal:
    fields = {
        "expected_total": 48,
        "registered_total": 48,
        "primary_attempts": 48,
        "status_counts": {"completed": 48, "rejected": 0, "crashed": 0, "timeout": 0},
        "retry_attempts": 0,
        "accounting_complete": True,
        "performance_usable": True,
        "trial_accounting_hash": _HEX64_B,
        "reason_codes": (),
    }
    fields.update(overrides)
    return H6AAccountingSeal(**fields)


def _s3_trade(fold_id, net_bps, *, exit_ts=0, exit_reason="TP", dimension="XRPUSDT"):
    return MetricTrade(
        strategy="S3",
        config_id="S3-00",
        fold_id=fold_id,
        path_scenario="primary_stress17",
        dimension=dimension,
        direction="long",
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=10.0,
        exit_reason=exit_reason,
        gross_bps=net_bps + 5.0,
        net_bps=net_bps,
        tp_bps=68.0,
        sl_bps=40.0,
        gross_notional=None,
        market_return_4h=0.01,
        volatility_percentile=50.0,
    )


def _s4_trade(fold_id, net_bps, *, exit_ts=0, exit_reason="TP", dimension="XRP-DOGE"):
    return MetricTrade(
        strategy="S4",
        config_id="S4-00",
        fold_id=fold_id,
        path_scenario="primary_stress17",
        dimension=dimension,
        direction="long",
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=10.0,
        exit_reason=exit_reason,
        gross_bps=net_bps + 5.0,
        net_bps=net_bps,
        tp_bps=68.0,
        sl_bps=40.0,
        gross_notional=None,
        market_return_4h=0.01,
        volatility_percentile=None,
    )


def _s3_primary_trades(n: int = 40) -> tuple:
    return tuple(_s3_trade(FOLD_IDS[i % 8], 10.0, exit_ts=i) for i in range(n))


def _s4_primary_trades(n: int = 40) -> tuple:
    return tuple(_s4_trade(FOLD_IDS[i % 8], 10.0, exit_ts=i) for i in range(n))


def _build_strategy_inputs(strategy: str) -> StrategyCanonicalInputs:
    if strategy == "S3":
        primary = _s3_primary_trades()
        common_gates = evaluate_common_gates(
            primary_trades=primary, upward_trades=_s3_primary_trades(10)
        )
        falsification = evaluate_s3_falsification(
            primary_trades=primary, upward_trades=_s3_primary_trades(10)
        )
        return StrategyCanonicalInputs(
            strategy="S3",
            common_gates=common_gates,
            falsification=falsification,
            direct_verdict="historical_pass",
            exit_reason_order=("TP", "SL", "THESIS_EXIT", "TIMEOUT"),
            dimension_order=("XRPUSDT", "DOGEUSDT", "SOLUSDT"),
            unique_by_key={},
            paths_by_key={},
            pbo=None,
        )
    primary = _s4_primary_trades()
    common_gates = evaluate_common_gates(
        primary_trades=primary, upward_trades=_s4_primary_trades(10)
    )
    falsification = evaluate_s4_falsification(
        primary_trades=primary, upward_trades=_s4_primary_trades(10)
    )
    return StrategyCanonicalInputs(
        strategy="S4",
        common_gates=common_gates,
        falsification=falsification,
        direct_verdict="historical_pass",
        exit_reason_order=("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"),
        dimension_order=("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
        unique_by_key={},
        paths_by_key={},
        pbo=None,
        pair_executor_state=S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    )


def _build_scorecard() -> dict:
    return build_canonical_scorecard(
        envelope=_envelope(),
        h6a_seal=_seal(),
        envelope_ok=True,
        envelope_incomplete_reasons=(),
        s3_inputs=_build_strategy_inputs("S3"),
        s4_inputs=_build_strategy_inputs("S4"),
        campaign_decision=CampaignDecisionResult(
            campaign_decision="both_pass_s3_demo_candidate",
            s3_direct_verdict="historical_pass",
            s4_direct_verdict="historical_pass",
            demo_candidate="S3",
            s4_observable_superiority=False,
        ),
    )


class TestSanitizeBucket:
    def test_finite_pf_passes_through(self):
        bucket = {
            "trades": 10,
            "e17_bps": 5.0,
            "e0_bps": 8.0,
            "pf": 1.5,
            "avg_holding_minutes": 12.0,
        }
        result = sanitize_bucket(bucket)
        assert result["pf"] == pytest.approx(1.5)
        assert result["pf_reason"] is None

    def test_infinite_pf_becomes_null_with_reason(self):
        bucket = {
            "trades": 10,
            "e17_bps": 5.0,
            "e0_bps": 8.0,
            "pf": math.inf,
            "avg_holding_minutes": 12.0,
        }
        result = sanitize_bucket(bucket)
        assert result["pf"] is None
        assert result["pf_reason"] == "pf_infinite_zero_loss_with_profit"

    def test_nan_pf_becomes_null_with_reason(self):
        bucket = {
            "trades": 10,
            "e17_bps": 0.0,
            "e0_bps": 0.0,
            "pf": float("nan"),
            "avg_holding_minutes": 12.0,
        }
        result = sanitize_bucket(bucket)
        assert result["pf"] is None
        assert result["pf_reason"] == "pf_undefined_zero_loss_and_zero_profit"


class TestChronologicalKeyAndSort:
    def test_sorts_by_fold_config_entry_exit(self):
        t1 = _s3_trade("fold-01", 1.0, exit_ts=100)
        t2 = _s3_trade("fold-00", 2.0, exit_ts=50)
        result = sort_trades_chronologically((t1, t2))
        assert result == (t2, t1)

    def test_collision_raises(self):
        t1 = _s3_trade("fold-00", 1.0, exit_ts=100)
        t2 = _s3_trade("fold-00", 2.0, exit_ts=100)
        assert chronological_key(t1) == chronological_key(t2)
        with pytest.raises(H5InputError):
            sort_trades_chronologically((t1, t2))


class TestExactTypeRejection:
    def test_bool_as_float_rejected_in_envelope(self):
        with pytest.raises(H5InputError):
            _envelope(full_campaign_hash=True)

    def test_decimal_rejected_in_seal(self):
        with pytest.raises(H5InputError):
            _seal(registered_total=Decimal(48))

    def test_rounded_string_reentry_rejected(self):
        with pytest.raises(H5InputError):
            _seal(retry_attempts="0")

    def test_nan_rejected_by_generic_canonical_validator(self):
        with pytest.raises(H5InputError):
            from rob974_h5_canonical import _validate_canonical_value

            _validate_canonical_value({"x": float("nan")}, "root")

    def test_raw_json_dumps_with_allow_nan_false_rejects_nan(self):
        with pytest.raises(ValueError):
            canonical_json_bytes({"x": float("nan")})


class TestPermutationInvariance:
    def test_status_counts_key_order_does_not_change_hash(self):
        seal_a = _seal(
            status_counts={"completed": 48, "rejected": 0, "crashed": 0, "timeout": 0}
        )
        seal_b = _seal(
            status_counts={"timeout": 0, "crashed": 0, "rejected": 0, "completed": 48}
        )
        scorecard_a = build_canonical_scorecard(
            envelope=_envelope(),
            h6a_seal=seal_a,
            envelope_ok=True,
            envelope_incomplete_reasons=(),
            s3_inputs=_build_strategy_inputs("S3"),
            s4_inputs=_build_strategy_inputs("S4"),
            campaign_decision=CampaignDecisionResult(
                campaign_decision="both_pass_s3_demo_candidate",
                s3_direct_verdict="historical_pass",
                s4_direct_verdict="historical_pass",
                demo_candidate="S3",
                s4_observable_superiority=False,
            ),
        )
        scorecard_b = build_canonical_scorecard(
            envelope=_envelope(),
            h6a_seal=seal_b,
            envelope_ok=True,
            envelope_incomplete_reasons=(),
            s3_inputs=_build_strategy_inputs("S3"),
            s4_inputs=_build_strategy_inputs("S4"),
            campaign_decision=CampaignDecisionResult(
                campaign_decision="both_pass_s3_demo_candidate",
                s3_direct_verdict="historical_pass",
                s4_direct_verdict="historical_pass",
                demo_candidate="S3",
                s4_observable_superiority=False,
            ),
        )
        bytes_a = canonical_json_bytes(scorecard_a)
        bytes_b = canonical_json_bytes(scorecard_b)
        assert bytes_a == bytes_b
        assert hash_canonical_bytes(bytes_a) == hash_canonical_bytes(bytes_b)

    def test_attribution_dict_construction_order_does_not_change_hash(self):
        scorecard_a = _build_scorecard()
        # Rebuild S3 falsification's by_symbol attribution dict with keys
        # inserted in a DIFFERENT order -- must not move the hash.
        s3_inputs = _build_strategy_inputs("S3")
        by_symbol = s3_inputs.falsification.attribution["by_symbol"]
        reordered = dict(reversed(list(by_symbol.items())))
        object.__setattr__(
            s3_inputs.falsification,
            "attribution",
            {**s3_inputs.falsification.attribution, "by_symbol": reordered},
        )
        scorecard_b = build_canonical_scorecard(
            envelope=_envelope(),
            h6a_seal=_seal(),
            envelope_ok=True,
            envelope_incomplete_reasons=(),
            s3_inputs=s3_inputs,
            s4_inputs=_build_strategy_inputs("S4"),
            campaign_decision=CampaignDecisionResult(
                campaign_decision="both_pass_s3_demo_candidate",
                s3_direct_verdict="historical_pass",
                s4_direct_verdict="historical_pass",
                demo_candidate="S3",
                s4_observable_superiority=False,
            ),
        )
        assert canonical_json_bytes(scorecard_a) == canonical_json_bytes(scorecard_b)


class TestActualH4LedgerKeySentinel:
    def test_sentinel_present_in_lineage(self):
        scorecard = _build_scorecard()
        assert (
            scorecard["lineage"]["actual_h4_ledger_key"]
            == ACTUAL_H4_LEDGER_KEY_NOT_EVALUATED
            == "NOT_EVALUATED"
        )


class TestHashSensitivity:
    def test_hash_is_deterministic_for_identical_input(self):
        bytes_a = canonical_json_bytes(_build_scorecard())
        bytes_b = canonical_json_bytes(_build_scorecard())
        assert bytes_a == bytes_b
        assert hash_canonical_bytes(bytes_a) == hash_canonical_bytes(bytes_b)

    def test_changing_campaign_decision_changes_hash(self):
        base = _build_scorecard()
        mutated = json.loads(canonical_json_bytes(base))
        mutated["campaign_decision"]["campaign_decision"] = "both_fail"
        assert hash_canonical_bytes(canonical_json_bytes(base)) != hash_canonical_bytes(
            canonical_json_bytes(mutated)
        )

    def test_changing_a_reason_code_changes_hash(self):
        base = _build_scorecard()
        mutated = json.loads(canonical_json_bytes(base))
        mutated["strategies"]["S3"]["falsification"]["reasons"] = [
            "s3_symbol_dependence"
        ]
        assert hash_canonical_bytes(canonical_json_bytes(base)) != hash_canonical_bytes(
            canonical_json_bytes(mutated)
        )

    def test_changing_lineage_hash_changes_top_hash(self):
        base = _build_scorecard()
        mutated = json.loads(canonical_json_bytes(base))
        mutated["lineage"]["full_campaign_hash"] = "c" * 64
        assert hash_canonical_bytes(canonical_json_bytes(base)) != hash_canonical_bytes(
            canonical_json_bytes(mutated)
        )
