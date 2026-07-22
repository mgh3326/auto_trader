"""ROB-974 R3 H6-B materializer and independent post-audit shape tests."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
import rob974_r3_postaudit as postaudit
from rob974_h6a_accounting import AttemptAccountingRow

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import rob974_r3_h6a_bridge as bridge
from app.services import rob974_r3_materializer as materializer
from app.services.research_canonical_hash import (
    compute_identity_hashes,
    derive_experiment_id,
)

_CONTRACT_HASHES = {
    "S3": "0bdfc36e13057076ce0fdd242c61f13be9e9ec01d78958d426ad4a1f46e7793f",
    "S4": "75ad9550edcd1571f7b69c686095bbcda8a8163cbd43394ea376118d8be49e27",
}


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _identity(row_id: str, strategy_key: str) -> StrategyExperimentIdentity:
    slug = row_id[:2]
    strategy_version = f"rob974_r3_{slug.lower()}_gate.v1"
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version=strategy_version,
        hypothesis="rob974 r3 frozen hypothesis",
        strategy={
            "slug": slug,
            "lineage": "R3",
            "strategy_key": strategy_key,
            "strategy_version": strategy_version,
        },
        code={
            "contract_hash": _CONTRACT_HASHES[slug],
            "contract_key": strategy_key,
        },
        params={"row_id": row_id},
        dataset_manifest={"corpus": "production-shaped-fixture"},
        universe={"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
        pit={"window": "2025-07-01/2026-07-01"},
        frozen_config={"fold_count": 8},
        policy={"selection": "all_cell_train_and_oos"},
        benchmark={},
        cost={"scenarios": [13, 17, 22]},
        mdd={"role": "report_only"},
    )


def _specs() -> tuple[
    tuple[StrategyExperimentIdentity, ...], tuple[StrategyExperimentIdentity, ...]
]:
    s3 = tuple(
        _identity(f"S3-R3-{index:02d}", "rob974.r3.s3.threshold-relaxation")
        for index in range(3)
    )
    s4 = tuple(
        _identity(f"S4-R3-{index:02d}", "rob974.r3.s4.threshold-relaxation")
        for index in range(9)
    )
    return s3, s4


def _mapping() -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in (*_specs()[0], *_specs()[1]):
        result[spec.params["row_id"]] = derive_experiment_id(
            spec.strategy_key,
            spec.strategy_version,
            compute_identity_hashes(spec.components()),
        )
    return result


_FULL_HASH = _hex("rob974-r3-materializer")
_RUN_ID = bridge.derive_r3_campaign_run_id(_FULL_HASH)


def _contract() -> materializer.R3MaterializationContract:
    s3, s4 = _specs()
    return materializer.issue_r3_materialization_contract(
        full_campaign_hash=_FULL_HASH,
        campaign_run_id=_RUN_ID,
        s3_specs=s3,
        s4_specs=s4,
        row_id_to_experiment_id=_mapping(),
    )


def _bridge_attempts() -> tuple[bridge.R3AttemptBatchItem, ...]:
    return tuple(
        bridge.R3AttemptBatchItem(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex(f"folds:{row_id}"),
            run_identity=_hex(f"run:{row_id}"),
            evidence_payload={"row_id": row_id, "fold_count": 8},
        )
        for row_id, experiment_id in _mapping().items()
    )


def _accounting_attempts() -> tuple[AttemptAccountingRow, ...]:
    return tuple(
        AttemptAccountingRow(
            row_id=item.row_id,
            experiment_id=item.experiment_id,
            retry_index=item.retry_index,
            status=item.status,
            reason_code=item.reason_code,
            fold_evidence_hash=item.fold_evidence_hash,
            run_identity=item.run_identity,
        )
        for item in _bridge_attempts()
    )


def test_materializer_contract_is_issued_and_value_equal_replacement_is_rejected() -> (
    None
):
    contract = _contract()
    assert contract.expected_total == 12
    assert contract.family_counts == (("S3", 3), ("S4", 9))
    materializer.require_issued_r3_materialization_contract(contract)

    forged = dataclasses.replace(contract)
    with pytest.raises(materializer.R3MaterializerContractError):
        materializer.require_issued_r3_materialization_contract(forged)


def test_materializer_rejects_partial_registration_status_sum_and_recomputed_order() -> (
    None
):
    contract = _contract()
    mapping = tuple(_mapping().items())
    attempts = _bridge_attempts()
    valid = materializer.R3PersistedSnapshot(
        campaign_run_id=_RUN_ID,
        registered_mapping=mapping,
        attempts=attempts,
        status_counts=(
            ("completed", 12),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    seal = materializer.validate_r3_persisted_snapshot(
        contract=contract, snapshot=valid, recomputed_attempts=attempts
    )
    assert seal.primary_attempts == seal.total_attempts == 12
    assert seal.retry_attempts == 0

    partial = dataclasses.replace(valid, registered_mapping=mapping[:-1])
    bad_status = dataclasses.replace(
        valid,
        status_counts=(
            ("completed", 11),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    reordered = (attempts[1], attempts[0], *attempts[2:])
    for snapshot, recomputed in (
        (partial, attempts),
        (bad_status, attempts),
        (valid, reordered),
    ):
        with pytest.raises(materializer.R3ReplayCollisionError):
            materializer.validate_r3_persisted_snapshot(
                contract=contract,
                snapshot=snapshot,
                recomputed_attempts=recomputed,
            )


def test_independent_postaudit_rejects_partial_status_retry_and_attempt_order_drift() -> (
    None
):
    mapping = tuple(_mapping().items())
    attempts = _accounting_attempts()
    valid = postaudit.R3PostAuditRawSnapshot(
        full_campaign_hash=_FULL_HASH,
        campaign_run_id=_RUN_ID,
        registered_mapping=mapping,
        attempts=attempts,
        reported_status_counts=(
            ("completed", 12),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    seal = postaudit.build_r3_postaudit_seal(
        expected_full_campaign_hash=_FULL_HASH,
        expected_campaign_run_id=_RUN_ID,
        expected_mapping=mapping,
        snapshot=valid,
    )
    assert seal.experiments == seal.trials == 12
    assert seal.strategy_counts == (("S3", 3), ("S4", 9))

    partial = dataclasses.replace(valid, registered_mapping=mapping[:-1])
    bad_status = dataclasses.replace(
        valid,
        reported_status_counts=(
            ("completed", 11),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    retry = dataclasses.replace(attempts[0], retry_index=1)
    retried = dataclasses.replace(valid, attempts=(retry, *attempts[1:]))
    reordered = dataclasses.replace(
        valid, attempts=(attempts[1], attempts[0], *attempts[2:])
    )
    for mutant in (partial, bad_status, retried, reordered):
        with pytest.raises(postaudit.R3PostAuditMismatch):
            postaudit.build_r3_postaudit_seal(
                expected_full_campaign_hash=_FULL_HASH,
                expected_campaign_run_id=_RUN_ID,
                expected_mapping=mapping,
                snapshot=mutant,
            )


def test_independent_postaudit_rejects_mutually_agreed_forged_run_id() -> None:
    mapping = tuple(_mapping().items())
    forged_run_id = "rob974r3-mutually-agreed-but-not-derived"
    snapshot = postaudit.R3PostAuditRawSnapshot(
        full_campaign_hash=_FULL_HASH,
        campaign_run_id=forged_run_id,
        registered_mapping=mapping,
        attempts=_accounting_attempts(),
        reported_status_counts=(
            ("completed", 12),
            ("rejected", 0),
            ("crashed", 0),
            ("timeout", 0),
        ),
    )
    with pytest.raises(postaudit.R3PostAuditMismatch):
        postaudit.build_r3_postaudit_seal(
            expected_full_campaign_hash=_FULL_HASH,
            expected_campaign_run_id=forged_run_id,
            expected_mapping=mapping,
            snapshot=snapshot,
        )

    assert postaudit.derive_r3_postaudit_campaign_run_id(_FULL_HASH) == _RUN_ID
