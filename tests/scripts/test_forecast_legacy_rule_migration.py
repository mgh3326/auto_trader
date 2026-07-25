# tests/scripts/test_forecast_legacy_rule_migration.py
"""Unit tests for scripts/forecast_legacy_rule_migration.py.

ROB-1038 / Forecast Recovery:
Verifies migration target selection validation, count mismatch abort, dry-run safety (no writes/commits),
superseded id 140 handling, and correct rule_version backfilling with honest provenance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.review import TradeForecast
from app.models.trading import InstrumentType
from scripts.forecast_legacy_rule_migration import (
    RULE_VERSION_TERMINAL_CLOSE,
    MigrationTargetCountMismatch,
    apply_migration_plan,
    create_snapshot,
    fetch_migration_targets,
    run_migration,
)

pytestmark = [pytest.mark.unit]


def _make_forecast(
    id_: int,
    session_label: str | None,
    direction: str = "at_or_above",
    target_price: float = 100.0,
    status: str = "open",
    rule_version: str | None = None,
) -> TradeForecast:
    f_target = {
        "kind": "price_target",
        "direction": direction,
        "target_price": target_price,
    }
    if rule_version:
        f_target["outcome_rule_version"] = rule_version

    f = TradeForecast(
        id=id_,
        symbol="005930" if id_ < 140 else "SMCI",
        instrument_type=InstrumentType.equity_kr
        if id_ < 140
        else InstrumentType.equity_us,
        session_label=session_label,
        forecast_target=f_target,
        horizon="D+5 trading sessions",
        probability=Decimal("0.65"),
        review_date=date(2026, 8, 20),
        status=status,
    )
    return f


def _build_test_targets() -> tuple[
    list[TradeForecast], TradeForecast, list[TradeForecast]
]:
    # 12 backfill targets (id 129~139, 143)
    backfill_ids = [129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 143]
    backfill = [
        _make_forecast(
            id_=i,
            session_label="directional-forecast-lab-round-1",
            direction="at_or_above" if i % 2 == 0 else "at_or_below",
        )
        for i in backfill_ids
    ]

    # 1 superseded target (id 140)
    superseded = _make_forecast(
        id_=140,
        session_label="directional-forecast-lab-round-1",
        direction="at_or_above",
    )

    # 106 closed_no_claim targets
    closed_no_claim = [
        _make_forecast(
            id_=200 + i,
            session_label="other-lab" if i % 2 == 0 else None,
            direction="at_or_above",
        )
        for i in range(106)
    ]

    return backfill, superseded, closed_no_claim


@pytest.mark.asyncio
async def test_migration_target_count_mismatch_aborts():
    """Verify that fetch_migration_targets raises MigrationTargetCountMismatch if counts differ from expected."""
    backfill, superseded, closed_no_claim = _build_test_targets()

    # Drop 2 items from backfill so count is 10 instead of 12
    short_backfill = backfill[:10]
    all_rows = short_backfill + [superseded] + closed_no_claim

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars().all.return_value = all_rows
    mock_db.execute.return_value = mock_result

    with pytest.raises(MigrationTargetCountMismatch, match="target count mismatch"):
        await fetch_migration_targets(
            mock_db,
            expected_backfill=12,
            expected_superseded=1,
            expected_closed_no_claim=106,
        )


@pytest.mark.asyncio
async def test_migration_dry_run_does_not_commit():
    """Verify that run_migration(commit=False) does not call db.commit or mutate forecast objects."""
    backfill, superseded, closed_no_claim = _build_test_targets()
    all_rows = backfill + [superseded] + closed_no_claim

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars().all.return_value = all_rows
    mock_db.execute.return_value = mock_result

    res = await run_migration(mock_db, commit=False)

    assert res["mode"] == "dry-run"
    assert res["backfill_count"] == 12
    assert res["superseded_count"] == 1
    assert res["closed_no_claim_count"] == 106
    assert mock_db.commit.call_count == 0

    # Ensure no target objects were mutated in dry run
    for f in backfill:
        assert f.forecast_target.get("outcome_rule_version") is None
        assert f.status == "open"
    assert superseded.status == "open"


def test_apply_migration_plan_mutations():
    """Verify in-place object mutations by apply_migration_plan."""
    backfill, superseded, closed_no_claim = _build_test_targets()

    # Capture raw original attributes to verify immutability of raw forecast fields
    orig_prob = backfill[0].probability
    orig_horizon = backfill[0].horizon
    orig_symbol = backfill[0].symbol

    res = apply_migration_plan(backfill, superseded, closed_no_claim)

    assert res["backfill_count"] == 12
    assert res["superseded_count"] == 1
    assert res["closed_no_claim_count"] == 106

    # 1. Backfill targets validation
    for f in backfill:
        assert f.status == "open"
        assert f.forecast_target["kind"] == "terminal_close"
        assert f.forecast_target["outcome_rule_version"] == RULE_VERSION_TERMINAL_CLOSE
        assert f.forecast_target["direction"] in ("up", "down")
        assert f.resolution_source == "legacy_rule_backfill"
        assert (
            f.resolution_detail["provenance_basis"]
            == "contemporaneous_internal_records"
        )
        assert (
            "rob-1036-smci-codex-audit.md" in f.resolution_detail["provenance_detail"]
        )

    # Raw forecast text, probability, horizon, symbol untouched
    assert backfill[0].probability == orig_prob
    assert backfill[0].horizon == orig_horizon
    assert backfill[0].symbol == orig_symbol

    # 2. Superseded target (id 140) validation
    assert superseded.id == 140
    assert superseded.status == "closed_no_claim"
    assert superseded.resolution_source == "quarantine_legacy_superseded"
    assert superseded.resolution_detail["superseded_by"] == 143
    assert (
        superseded.resolution_detail["provenance_basis"]
        == "contemporaneous_internal_records"
    )

    # 3. Closed no claim targets validation
    for f in closed_no_claim:
        assert f.status == "closed_no_claim"
        assert f.resolution_source == "quarantine_legacy_cleanup"
        assert (
            f.resolution_detail["provenance_basis"]
            == "contemporaneous_internal_records"
        )


def test_create_snapshot():
    """Verify that create_snapshot produces a JSON-serializable list of dicts."""
    backfill, superseded, closed_no_claim = _build_test_targets()
    snapshot = create_snapshot(backfill, superseded, closed_no_claim)

    assert len(snapshot) == 119
    first = snapshot[0]
    assert "id" in first
    assert "forecast_id" in first
    assert "symbol" in first
    assert "forecast_target" in first
