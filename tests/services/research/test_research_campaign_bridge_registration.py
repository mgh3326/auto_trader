"""ROB-946 (H6) — campaign registration bridge: RED-first coverage.

Registering the 24 campaign experiments must go through the SAME two-gate
write guard as trial recording (ROB-946 §7/§8), and must refuse anything
other than exactly 24 UNIQUE specs — the empirical runner (H4) is not allowed
to start against a partially-registered or duplicate-masking campaign (R1
Minor-5: a 24th duplicate spec must never silently replace a missing one).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.models.research_backtest import ResearchStrategyExperiment
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services.research_campaign_bridge import (
    CampaignDuplicateSpecError,
    CampaignSpecCountError,
    register_campaign_experiments,
)
from app.services.research_db_write_guard import (
    ResearchDbPolicy,
    ResearchDbTarget,
    ResearchDbTargetRejected,
    ResearchWriteDisabled,
)

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)
_DENYING_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="some-other-host", database_name="some_other_db")
)


def _identity(config_id: str, **overrides) -> StrategyExperimentIdentity:
    base = {
        "strategy_key": "ROB946-CAMPAIGN-TEST-" + uuid.uuid4().hex[:8],
        "strategy_version": "v1",
        "hypothesis": "campaign registration bridge test",
        "strategy": {"slug": "S1"},
        "code": {"source_sha256": "0" * 64},
        "params": {"config_id": config_id},
        "dataset_manifest": {"corpus": "fixture"},
        "universe": {"symbols": ["XRPUSDT"]},
        "pit": {"window": "fixture"},
        "frozen_config": {"timeframe": "15m"},
        "policy": {"selection": "fixture"},
        "benchmark": {},
        "cost": {"primary_stress": 17.0},
        "mdd": {"role": "report_only"},
    }
    base.update(overrides)
    return StrategyExperimentIdentity(**base)


def _campaign_specs() -> list[StrategyExperimentIdentity]:
    return [
        _identity(f"S1-{i:02d}", params={"config_id": f"S1-{i:02d}"}) for i in range(24)
    ]


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registers_all_24_unique_specs(registry_tables) -> None:
    session = registry_tables
    specs = _campaign_specs()

    registered = await register_campaign_experiments(
        session,
        specs=specs,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()

    assert len(registered) == 24
    assert len({r.experiment_id for r in registered}) == 24


@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_disabled_rejects_before_any_registration_row_is_written(
    registry_tables,
) -> None:
    session = registry_tables
    specs = _campaign_specs()
    before = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )

    with pytest.raises(ResearchWriteDisabled):
        await register_campaign_experiments(
            session,
            specs=specs,
            guard_opt_in_enabled=False,
            guard_policy=_POLICY,
        )

    after = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_unauthorized_target_rejects_before_any_registration_row_is_written(
    registry_tables,
) -> None:
    session = registry_tables
    specs = _campaign_specs()
    before = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )

    with pytest.raises(ResearchDbTargetRejected):
        await register_campaign_experiments(
            session,
            specs=specs,
            guard_opt_in_enabled=True,
            guard_policy=_DENYING_POLICY,
        )

    after = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_spec_count_rejected_23(registry_tables) -> None:
    session = registry_tables
    with pytest.raises(CampaignSpecCountError):
        await register_campaign_experiments(
            session,
            specs=_campaign_specs()[:-1],
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_spec_count_rejected_25(registry_tables) -> None:
    session = registry_tables
    specs = _campaign_specs()
    extra = _identity("S1-24", params={"config_id": "S1-24"})
    with pytest.raises(CampaignSpecCountError):
        await register_campaign_experiments(
            session,
            specs=[*specs, extra],
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_runs_before_spec_count_check(registry_tables) -> None:
    session = registry_tables
    # Disabled guard + a malformed (23-item) spec list: the guard's rejection
    # must win — authorization is checked before any spec-shape inspection.
    with pytest.raises(ResearchWriteDisabled):
        await register_campaign_experiments(
            session,
            specs=_campaign_specs()[:-1],
            guard_opt_in_enabled=False,
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_spec_replacing_a_missing_one_is_rejected_before_any_write(
    registry_tables,
) -> None:
    # R1 Minor-5 reproduction: 23 distinct specs + 1 EXACT duplicate of the
    # first, instead of a 24th distinct config — must be rejected, never
    # silently accepted as "24 registered".
    session = registry_tables
    base_specs = [
        _identity(f"S1-{i:02d}", params={"config_id": f"S1-{i:02d}"}) for i in range(23)
    ]
    duplicate_of_first = base_specs[0]
    specs = [*base_specs, duplicate_of_first]
    assert len(specs) == 24

    before = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )
    with pytest.raises(CampaignDuplicateSpecError):
        await register_campaign_experiments(
            session,
            specs=specs,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    after = await session.scalar(
        select(func.count()).select_from(ResearchStrategyExperiment)
    )
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registering_same_24_twice_is_idempotent_by_identity(
    registry_tables,
) -> None:
    session = registry_tables
    specs = _campaign_specs()

    first = await register_campaign_experiments(
        session, specs=specs, guard_opt_in_enabled=True, guard_policy=_POLICY
    )
    await session.flush()
    second = await register_campaign_experiments(
        session, specs=specs, guard_opt_in_enabled=True, guard_policy=_POLICY
    )

    assert [r.experiment_id for r in first] == [r.experiment_id for r in second]
