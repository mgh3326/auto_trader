from unittest.mock import MagicMock

import pytest

from app.models.investment_snapshots import InvestmentSnapshot


def _check_sql():
    target_names = {
        "ck_investment_snapshots_snapshot_kind",
        "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind",
    }
    for c in InvestmentSnapshot.__table__.constraints:
        if getattr(c, "name", "") in target_names:
            return str(c.sqltext)
    raise AssertionError("snapshot_kind CHECK not found")


@pytest.mark.unit
def test_model_check_has_investor_flow_and_preserves_old():
    sql = _check_sql()
    assert "investor_flow" in sql
    for kind in (
        "portfolio", "market", "news", "symbol", "candidate_universe",
        "validated_run_card", "kr_market_ranking",
    ):
        assert kind in sql


@pytest.mark.unit
def test_schema_literal_has_investor_flow():
    from typing import get_args

    from app.schemas.investment_snapshots import SnapshotKind

    assert "investor_flow" in get_args(SnapshotKind)


@pytest.mark.unit
def test_drift_guard_contract_matches_runtime_registry():
    from app.services.action_report.snapshot_backed.collectors.registry import (
        production_collector_registry,
    )
    from app.services.invest_data_source_contract import collector_wired_kinds

    runtime = production_collector_registry(MagicMock()).list_kinds()
    contract = collector_wired_kinds()
    assert "investor_flow" in runtime
    assert "investor_flow" in contract
    assert contract == runtime
