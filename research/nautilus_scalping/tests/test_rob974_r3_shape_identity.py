"""ROB-974 R3 M0 exact-12 lineage and accounting contract tests."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
import rob974_r3_accounting as accounting
import rob974_r3_identity as identity
import rob974_r3_shape as shape
from rob974_h6a_accounting import AttemptAccountingRow


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _mapping() -> tuple[tuple[str, str], ...]:
    return tuple((row_id, _hex(row_id)) for row_id in shape.R3_CANONICAL_ROW_ORDER)


def _rows() -> tuple[identity.R3CampaignConfigRow, ...]:
    return tuple(
        identity.R3CampaignConfigRow(
            row_id=row_id,
            params={"ordinal": index},
            hypothesis=f"{row_id[:2]} frozen hypothesis",
            authority_label="rob974-r3-preregistration",
            provenance="production",
        )
        for index, row_id in enumerate(shape.R3_CANONICAL_ROW_ORDER)
    )


def _contracts() -> dict[str, identity.R3StrategyContractProvenance]:
    return {
        slug: identity.R3StrategyContractProvenance(
            strategy_slug=slug,
            strategy_key=f"rob974.r3.{slug.lower()}",
            strategy_version="r3.v1",
            contract_hash=_hex(f"{slug}-contract"),
            contract_key=f"rob974.r3.{slug.lower()}.contract",
            provenance="production",
            expected_contract_hash=_hex(f"{slug}-contract"),
        )
        for slug in ("S3", "S4")
    }


def _specs() -> tuple[identity.R3RowSpec, ...]:
    return identity.build_r3_campaign_row_specs(
        _rows(),
        contracts=_contracts(),
        shared_components={
            "dataset_manifest": {"sha256": _hex("dataset")},
            "universe": {"symbols": ["XRPUSDT", "DOGEUSDT", "SOLUSDT"]},
            "benchmark": {"name": "rob974-r3"},
            "mdd": {"role": "report_only"},
        },
        pit_component_by_slug={"S3": {"pit": "s3"}, "S4": {"pit": "s4"}},
        frozen_config_component_by_slug={
            "S3": {"folds": 8},
            "S4": {"folds": 8},
        },
        policy_component_by_slug={
            "S3": {"selection": "all_cell"},
            "S4": {"selection": "all_cell"},
        },
        cost_component_by_slug={
            "S3": {"scenarios": [13, 17, 22]},
            "S4": {"scenarios": [13, 17, 22]},
        },
    )


def _attempts(
    mapping: tuple[tuple[str, str], ...] | None = None,
) -> tuple[AttemptAccountingRow, ...]:
    mapping = _mapping() if mapping is None else mapping
    return tuple(
        AttemptAccountingRow(
            row_id=row_id,
            experiment_id=experiment_id,
            retry_index=0,
            status="completed",
            reason_code=None,
            fold_evidence_hash=_hex(f"folds:{row_id}"),
            run_identity=_hex(f"run:{row_id}"),
        )
        for row_id, experiment_id in mapping
    )


def test_code_issued_shapes_are_literal_and_value_equal_forgery_is_rejected() -> None:
    assert shape.R2_SHAPE.total_rows == 48
    assert shape.R3_SHAPE.family_counts == (("S3", 3), ("S4", 9))
    assert shape.R3_SHAPE.row_order == tuple(
        [f"S3-R3-{index:02d}" for index in range(3)]
        + [f"S4-R3-{index:02d}" for index in range(9)]
    )

    forged = dataclasses.replace(shape.R3_SHAPE)
    with pytest.raises(shape.CampaignShapeForgeryError):
        shape.require_issued_shape(forged)


@pytest.mark.parametrize("remove", (0, 5, 11))
def test_exact_12_mapping_rejects_11_rows(remove: int) -> None:
    rows = list(_mapping())
    rows.pop(remove)
    with pytest.raises(shape.Exact12MappingError):
        shape.compute_exact_12_mapping_hash(tuple(rows))


def test_exact_12_mapping_rejects_13_reorder_duplicate_r2_and_container_drift() -> None:
    mapping = _mapping()
    mutants: tuple[object, ...] = (
        mapping + (("S4-R3-09", _hex("extra")),),
        (mapping[1], mapping[0], *mapping[2:]),
        (*mapping[:-1], (mapping[-1][0], mapping[0][1])),
        (("S3-00", mapping[0][1]), *mapping[1:]),
        list(mapping),
    )
    for mutant in mutants:
        with pytest.raises(shape.Exact12MappingError):
            shape.compute_exact_12_mapping_hash(mutant)  # type: ignore[arg-type]


def test_r3_identity_rejects_11_13_split_reorder_duplicate_and_r2_injection() -> None:
    rows = _rows()
    with pytest.raises(identity.Exact12IdentityError):
        dataclasses.replace(rows[0], row_id="S3-00")

    mutants = (
        rows[:-1],
        rows + (dataclasses.replace(rows[-1], row_id="S4-R3-09"),),
        tuple(
            dataclasses.replace(row, row_id=f"S3-R3-{index:02d}") if index == 3 else row
            for index, row in enumerate(rows)
        ),
        (rows[1], rows[0], *rows[2:]),
        (*rows[:-1], rows[0]),
    )
    for mutant in mutants:
        with pytest.raises(identity.Exact12IdentityError):
            identity.validate_r3_campaign_rows(mutant)


def test_r3_identity_builds_exact_order_unique_64_hex_experiment_ids() -> None:
    specs = _specs()
    assert tuple(spec.row_id for spec in specs) == shape.R3_CANONICAL_ROW_ORDER
    assert len({spec.experiment_id for spec in specs}) == 12
    assert all(len(spec.experiment_id) == 64 for spec in specs)
    assert shape.compute_exact_12_mapping_hash(
        tuple((spec.row_id, spec.experiment_id) for spec in specs)
    )


def test_exact_12_accounting_complete_partial_retry_and_order_contracts() -> None:
    report = accounting.build_exact_12_accounting(
        campaign_run_id="rob974r3-fixture",
        ordered_mapping=_mapping(),
        registered_total=12,
        attempts=_attempts(),
    )
    assert report.expected_total == report.registered_total == 12
    assert report.primary_attempts == report.total_attempts == 12
    assert sum(report.status_counts.values()) == 12
    assert report.accounting_complete is True

    partial = accounting.build_exact_12_accounting(
        campaign_run_id="rob974r3-fixture",
        ordered_mapping=_mapping(),
        registered_total=11,
        attempts=_attempts()[:-1],
    )
    assert partial.accounting_complete is False
    assert partial.missing_row_ids == (shape.R3_CANONICAL_ROW_ORDER[-1],)

    retry = dataclasses.replace(_attempts()[0], retry_index=1)
    with pytest.raises(accounting.Exact12AccountingError):
        accounting.build_exact_12_accounting(
            campaign_run_id="rob974r3-fixture",
            ordered_mapping=_mapping(),
            registered_total=12,
            attempts=(retry, *_attempts()[1:]),
        )

    with pytest.raises(accounting.Exact12AccountingError):
        accounting.build_exact_12_accounting(
            campaign_run_id="rob974r3-fixture",
            ordered_mapping=_mapping(),
            registered_total=12,
            attempts=(_attempts()[1], _attempts()[0], *_attempts()[2:]),
        )
