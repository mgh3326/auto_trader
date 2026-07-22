"""ROB-974 R3 M4/CP6 scorecard and paired-artifact contract tests.

The first slice deliberately uses the production plan's real exact-12 row
identities and all eight real folds.  It exercises only pure, in-memory
surfaces: no corpus, database, network, broker, order, fill, or publication
boundary is reachable from this test module.
"""

from __future__ import annotations

import dataclasses

import pytest

from rob974_r3_accounting import build_exact_12_accounting
from rob974_r3_evidence_context import issue_r3_production_evidence_context
from rob974_r3_manifest import FROZEN_R3_ROSTER
from rob974_r3_plan import build_production_r3_plan
from rob974_r3_relaxation import CellFoldLedger
from rob974_r3_scorecard import (
    R3_SCORECARD_SCHEMA_VERSION,
    R3CellOOSLedger,
    R3FoldOOSLedger,
    build_r3_artifact_pair,
    build_r3_scorecard,
    canonical_r3_json_bytes,
    hash_r3_canonical_bytes,
    issue_r3_all_cell_oos_ledger,
    verify_r3_artifact_pair,
)


def _production_context():
    return issue_r3_production_evidence_context(build_production_r3_plan())


def _empty_cells() -> tuple[R3CellOOSLedger, ...]:
    plan = build_production_r3_plan()
    return tuple(
        R3CellOOSLedger(
            config_id=config.config_id,
            folds=tuple(
                R3FoldOOSLedger(
                    source_ledger=CellFoldLedger(
                        config_id=config.config_id,
                        fold_id=fold.fold_id,
                        basket_trade_count=0,
                        trades=(),
                    ),
                    accepted_count=0,
                    trade_attributions=(),
                    terminal_incomplete=None,
                )
                for fold in plan.folds
            ),
        )
        for config in FROZEN_R3_ROSTER
    )


def test_production_real_incomplete_scorecard_is_exact_12_and_research_null() -> None:
    context = _production_context()
    ledger = issue_r3_all_cell_oos_ledger(
        evidence_context=context,
        cells=_empty_cells(),
    )
    accounting = build_exact_12_accounting(
        campaign_run_id=context.campaign_run_id,
        ordered_mapping=context.ordered_mapping,
        registered_total=0,
        attempts=(),
    )

    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=accounting,
        oos_ledger=ledger,
        gate_evidence=None,
        relaxation_evidence=None,
    )

    assert scorecard["schema_version"] == R3_SCORECARD_SCHEMA_VERSION
    assert [cell["config_id"] for cell in scorecard["cells"]] == [
        row.config_id for row in FROZEN_R3_ROSTER
    ]
    assert len(scorecard["cells"]) == 12
    assert scorecard["lineage"]["campaign_identity_sha256"] == (
        context.campaign_identity_sha256
    )
    assert len(scorecard["lineage"]["campaign_identity_sha256"]) == 64
    assert all(len(experiment_id) == 64 for _, experiment_id in context.ordered_mapping)
    assert len(context.folds) == 8
    assert scorecard["campaign_verdict"]["operational_status"] == "INCOMPLETE"
    assert scorecard["campaign_verdict"]["research_decision"] is None
    assert all(
        row["status"] == "INCOMPLETE"
        for row in scorecard["section3_falsification"]
    )


def test_issued_all_cell_ledger_rejects_reordered_production_cells() -> None:
    context = _production_context()
    cells = _empty_cells()
    with pytest.raises(ValueError, match="canonical exact-12 order"):
        issue_r3_all_cell_oos_ledger(
            evidence_context=context,
            cells=(cells[1], cells[0], *cells[2:]),
        )


def test_canonical_json_and_markdown_are_semantically_paired_in_memory() -> None:
    context = _production_context()
    scorecard = build_r3_scorecard(
        evidence_context=context,
        accounting=build_exact_12_accounting(
            campaign_run_id=context.campaign_run_id,
            ordered_mapping=context.ordered_mapping,
            registered_total=0,
            attempts=(),
        ),
        oos_ledger=issue_r3_all_cell_oos_ledger(
            evidence_context=context,
            cells=_empty_cells(),
        ),
        gate_evidence=None,
        relaxation_evidence=None,
    )

    canonical = canonical_r3_json_bytes(scorecard)
    semantic_sha256 = hash_r3_canonical_bytes(canonical)
    pair = build_r3_artifact_pair(scorecard)

    assert pair.json_bytes == canonical
    assert pair.semantic_sha256 == semantic_sha256
    assert len(pair.semantic_sha256) == len(pair.markdown_sha256) == 64
    assert verify_r3_artifact_pair(
        json_bytes=pair.json_bytes,
        markdown_bytes=pair.markdown_bytes,
    )["schema_version"] == R3_SCORECARD_SCHEMA_VERSION

    mismatched = dataclasses.replace(
        pair,
        markdown_bytes=pair.markdown_bytes.replace(
            b"research_decision: null", b"research_decision: CONTINUE"
        ),
    )
    assert mismatched.markdown_bytes != pair.markdown_bytes
    with pytest.raises(ValueError, match="Markdown semantic mismatch"):
        verify_r3_artifact_pair(
            json_bytes=mismatched.json_bytes,
            markdown_bytes=mismatched.markdown_bytes,
        )
