"""ROB-1023 production backtest metadata width regression coverage."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    BACKTEST_RUNNER_MAX_LENGTH,
    ResearchBacktestRun,
)
from app.services.rob974_h6b_materializer import (
    ROB974_R2_PRODUCTION_RUNNER,
    ROB974_R2_PRODUCTION_STRATEGY_NAME,
    ROB974_R2_PRODUCTION_TIMEFRAME,
    ProductionCampaignInput,
    build_production_identity_plan,
)

# Exact values passed by scripts/run_rob974_r2_campaign.py in the sealed
# production launcher.  Keep the lengths visible: CP9/CP10 previously used
# ProductionCampaignInput defaults whose runner was only 10 characters long.
_PRODUCTION_METADATA = {
    "strategy_name": ROB974_R2_PRODUCTION_STRATEGY_NAME,
    "timeframe": ROB974_R2_PRODUCTION_TIMEFRAME,
    "runner": ROB974_R2_PRODUCTION_RUNNER,
}

_REPO = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO / "alembic/versions/20260722_rob1023_widen_runner.py"
_FULL_CAMPAIGN_HASH = "2c47864c7ab661f16be6c414a1140944ec36832bb268e86183555b56c6f85f53"
_CAMPAIGN_RUN_ID = "rob974h6a-CvcCOcAO3hRQDUPzHdVBJFmkXi_dN6NmngCOBLk82lI"
_RUNNER_SOURCE_SHA256 = (
    "5314d2429801a2df78d7bbac852a3ae1c19567bf9be22c74704b907eaf818c55"
)


def test_production_campaign_metadata_values_and_lengths_are_exact() -> None:
    assert {
        name: (value, len(value)) for name, value in _PRODUCTION_METADATA.items()
    } == {
        "strategy_name": ("rob974-r2", 9),
        "timeframe": ("1m_to_4h_pit", 12),
        "runner": ("rob974-h6b-all-folds", 20),
    }

    defaults = {field.name: field.default for field in fields(ProductionCampaignInput)}
    assert {name: defaults[name] for name in _PRODUCTION_METADATA} == (
        _PRODUCTION_METADATA
    )


def test_runner_width_contract_and_alembic_head_are_pinned() -> None:
    assert BACKTEST_RUNNER_MAX_LENGTH == 64
    assert ResearchBacktestRun.__table__.c.runner.type.length == 64

    source = _MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260722_rob1023_widen_runner"' in source
    assert 'down_revision: str = "20260720_rob976_support"' in source
    assert "sa.String(length=64)" in source

    config = Config(str(_REPO / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260722_rob1017_missed"
    ]


def test_schema_only_repair_preserves_h6b_production_identity_pins() -> None:
    first = build_production_identity_plan()
    second = build_production_identity_plan()

    assert first.to_payload() == second.to_payload()
    assert first.full_campaign_hash == _FULL_CAMPAIGN_HASH
    assert first.campaign_run_id == _CAMPAIGN_RUN_ID
    assert first.source_pins.runner_source_sha256 == _RUNNER_SOURCE_SHA256


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backtest_run_insert_accepts_production_campaign_metadata(
    db_session: AsyncSession,
) -> None:
    runner_width = await db_session.scalar(
        text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = 'research' AND table_name = 'backtest_runs' "
            "AND column_name = 'runner'"
        )
    )
    assert runner_width == BACKTEST_RUNNER_MAX_LENGTH

    row = ResearchBacktestRun(
        run_id=f"rob1023-width-{uuid4().hex}",
        strategy_version=None,
        exchange="binance",
        market="spot",
        timerange=None,
        **_PRODUCTION_METADATA,
    )
    db_session.add(row)

    await db_session.flush()

    assert row.runner == "rob974-h6b-all-folds"
