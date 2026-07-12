"""ROB-846 — strategy experiment registry: unit + DB integration.

Covers the acceptance criteria:
* Canonical identity + idempotent registration (AC#1).
* Monotonic complete trial accounting incl crash/timeout/reject, idempotency
  replay + race (AC#2, AC#4).
* Append-only / no-update: DB mutation of experiment & trial rows fails, and
  corrections create new lineage without changing old hashes (AC#3).
* Promotion candidate hash matching (AC#5).
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.research_backtest import ResearchBacktestRun
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    PromotionLinkRequest,
    StrategyExperimentIdentity,
)
from app.services import strategy_experiment_registry as reg


def _identity(**overrides) -> StrategyExperimentIdentity:
    base = {
        "strategy_key": "NFIX",
        "strategy_version": "v-" + uuid.uuid4().hex[:10],
        "hypothesis": "mean reversion on 5m",
        "strategy": {"name": "NostalgiaForInfinity"},
        "code": "def populate_entry_trend(): ...",
        "params": {"roi": {"0": 0.05}, "stoploss": -0.1},
        "dataset_manifest": {"pairs": ["BTC/USDT"], "candles": 200_000},
        "universe": ["BTC/USDT", "ETH/USDT"],
        "pit": {"information_cutoff": "2026-01-01T00:00:00Z"},
        "frozen_config": {"max_open_trades": 5, "timeframe": "5m"},
        "policy": {"gate": "honest_offline_v1"},
        "benchmark": {"symbol": "BTC/USDT", "kind": "buy_and_hold"},
        "cost": {"maker_bps": 2, "taker_bps": 4},
        "mdd": {"definition": "peak_to_trough"},
    }
    base.update(overrides)
    return StrategyExperimentIdentity(**base)


def _trial(status: str = "completed", **overrides) -> BacktestTrialRequest:
    base = {
        "status": status,
        "strategy_name": "NFIX",
        "timeframe": "5m",
        "runner": "mac",
    }
    base.update(overrides)
    return BacktestTrialRequest(**base)


# --------------------------------------------------------------------------- #
# Unit (no DB) — status validation guard                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_trial_rejects_unknown_status_before_touching_db() -> None:
    session = AsyncMock()
    # Bypass schema validation to hit the service-layer guard directly.
    bad = BacktestTrialRequest.model_construct(
        status="winner", strategy_name="k", timeframe="5m", runner="mac"
    )
    with pytest.raises(reg.InvalidTrialStatus):
        await reg.record_trial(session, experiment_id="e", request=bad)
    session.scalar.assert_not_called()


# --------------------------------------------------------------------------- #
# DB integration                                                               #
# --------------------------------------------------------------------------- #


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
async def test_register_experiment_is_idempotent_by_identity(registry_tables) -> None:
    session = registry_tables
    identity = _identity()

    first = await reg.register_experiment(session, identity)
    await session.flush()
    second = await reg.register_experiment(session, identity)

    assert first.id == second.id
    assert first.experiment_id == second.experiment_id
    assert len(first.experiment_id) == 64
    # Component identities recorded (AC#1).
    assert first.params_hash and first.frozen_config_hash
    assert first.dataset_manifest_hash and first.code_hash


@pytest.mark.integration
@pytest.mark.asyncio
async def test_component_change_yields_new_experiment_id(registry_tables) -> None:
    session = registry_tables
    key = "NFIX-" + uuid.uuid4().hex[:8]
    a = await reg.register_experiment(
        session, _identity(strategy_key=key, strategy_version="v1")
    )
    await session.flush()
    b = await reg.register_experiment(
        session,
        _identity(
            strategy_key=key,
            strategy_version="v1",
            params={"roi": {"0": 0.99}, "stoploss": -0.1},
        ),
    )
    assert a.experiment_id != b.experiment_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_invocation_gets_monotonic_index_all_statuses(
    registry_tables,
) -> None:
    session = registry_tables
    exp = await reg.register_experiment(session, _identity())
    await session.flush()

    statuses = ["completed", "rejected", "crashed", "timeout", "completed"]
    indices = []
    for status in statuses:
        row = await reg.record_trial(
            session, experiment_id=exp.experiment_id, request=_trial(status)
        )
        indices.append(row.trial_index)

    assert indices == [1, 2, 3, 4, 5]

    accounting = await reg.get_trial_accounting(session, exp.experiment_id)
    assert accounting.total_trials == 5
    # No winner-only filter: crash/timeout/reject are all counted (AC#4).
    assert accounting.outcome_counts == {
        "completed": 2,
        "rejected": 1,
        "crashed": 1,
        "timeout": 1,
    }

    trials = await reg.list_trials(session, exp.experiment_id)
    assert [t.trial_index for t in trials] == [1, 2, 3, 4, 5]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_idempotency_returns_original_row(registry_tables) -> None:
    session = registry_tables
    exp = await reg.register_experiment(session, _identity())
    await session.flush()

    key = "idem-" + uuid.uuid4().hex[:8]
    first = await reg.record_trial(
        session,
        experiment_id=exp.experiment_id,
        request=_trial("completed", idempotency_key=key),
    )
    second = await reg.record_trial(
        session,
        experiment_id=exp.experiment_id,
        request=_trial("crashed", idempotency_key=key),
    )

    assert first.id == second.id
    assert second.trial_index == first.trial_index
    # Original status preserved — the replay did not overwrite anything.
    assert second.trial_status == "completed"

    total = await session.scalar(
        select(func.count())
        .select_from(ResearchBacktestRun)
        .where(ResearchBacktestRun.strategy_experiment_id == exp.id)
    )
    assert total == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_trial_rejects_unregistered_experiment(registry_tables) -> None:
    session = registry_tables
    with pytest.raises(reg.ExperimentNotFound):
        await reg.record_trial(session, experiment_id="deadbeef" * 8, request=_trial())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersedes_unknown_experiment_fails(registry_tables) -> None:
    session = registry_tables
    with pytest.raises(reg.SupersedesNotFound):
        await reg.register_experiment(
            session,
            _identity(supersedes_experiment_id="00" * 32),
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correction_creates_new_lineage_without_changing_old_hashes(
    registry_tables,
) -> None:
    session = registry_tables
    key = "NFIX-" + uuid.uuid4().hex[:8]
    base = await reg.register_experiment(
        session, _identity(strategy_key=key, strategy_version="v1")
    )
    await session.flush()
    base_hashes = (base.params_hash, base.frozen_config_hash, base.code_hash)

    corrected = await reg.register_experiment(
        session,
        _identity(
            strategy_key=key,
            strategy_version="v2",
            params={"roi": {"0": 0.07}, "stoploss": -0.08},
            supersedes_experiment_id=base.experiment_id,
        ),
    )
    await session.flush()

    assert corrected.supersedes_experiment_id == base.experiment_id
    assert corrected.experiment_id != base.experiment_id
    # Old row's persisted hashes are untouched by the amendment (AC#3).
    persisted = (
        await session.execute(
            text(
                "SELECT params_hash, frozen_config_hash, code_hash "
                "FROM research.strategy_experiments WHERE id = :id"
            ),
            {"id": base.id},
        )
    ).one()
    assert tuple(persisted) == base_hashes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_experiment_row_update_is_rejected_by_db(registry_tables) -> None:
    session = registry_tables
    exp = await reg.register_experiment(session, _identity())
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "UPDATE research.strategy_experiments "
                "SET hypothesis = 'tampered' WHERE id = :id"
            ),
            {"id": exp.id},
        )
    await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trial_row_update_is_rejected_by_db(registry_tables) -> None:
    session = registry_tables
    exp = await reg.register_experiment(session, _identity())
    await session.flush()
    trial = await reg.record_trial(
        session, experiment_id=exp.experiment_id, request=_trial("completed")
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text("UPDATE research.backtest_runs SET total_trades = 99 WHERE id = :id"),
            {"id": trial.id},
        )
    await session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_promotion_candidate_requires_matching_hashes(registry_tables) -> None:
    session = registry_tables
    exp = await reg.register_experiment(session, _identity())
    await session.flush()
    trial = await reg.record_trial(
        session, experiment_id=exp.experiment_id, request=_trial("completed")
    )
    await session.flush()

    good = PromotionLinkRequest(
        expected_experiment_id=exp.experiment_id,
        expected_config_hash=exp.frozen_config_hash,
        expected_data_hash=exp.dataset_manifest_hash,
        status="PASS",
        reason_code="OK",
    )
    candidate = await reg.link_promotion_candidate(
        session, backtest_run_id=trial.id, request=good
    )
    assert candidate.experiment_id == exp.experiment_id
    assert candidate.run_config_hash == exp.frozen_config_hash

    bad = PromotionLinkRequest(
        expected_experiment_id=exp.experiment_id,
        expected_config_hash="00" * 32,  # mismatched config hash
        expected_data_hash=exp.dataset_manifest_hash,
        status="PASS",
        reason_code="OK",
    )
    with pytest.raises(reg.PromotionHashMismatch):
        await reg.link_promotion_candidate(
            session, backtest_run_id=trial.id, request=bad
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_promotion_candidate_rejects_run_without_experiment(
    registry_tables,
) -> None:
    session = registry_tables
    # A legacy summary run (no experiment linkage) cannot back a promotion.
    legacy = ResearchBacktestRun(
        run_id="legacy-" + uuid.uuid4().hex[:8],
        strategy_name="NFIX",
        timeframe="5m",
        runner="mac",
    )
    session.add(legacy)
    await session.flush()

    req = PromotionLinkRequest(
        expected_experiment_id="ab" * 32,
        expected_config_hash="cd" * 32,
        expected_data_hash="ef" * 32,
        status="PASS",
        reason_code="OK",
    )
    with pytest.raises(reg.PromotionHashMismatch):
        await reg.link_promotion_candidate(
            session, backtest_run_id=legacy.id, request=req
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_race_never_creates_duplicate_trial(registry_tables) -> None:
    # Two concurrent record_trial calls (separate committed transactions) with
    # the same idempotency key must converge to one row (AC#2 race).
    from app.core.db import engine

    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with Session() as setup:
        exp = await reg.register_experiment(setup, _identity())
        await setup.commit()
        experiment_id = exp.experiment_id
        exp_pk = exp.id

    key = "race-" + uuid.uuid4().hex[:8]

    async def worker() -> int:
        async with Session() as s:
            row = await reg.record_trial(
                s,
                experiment_id=experiment_id,
                request=_trial("completed", idempotency_key=key),
            )
            index = row.trial_index
            await s.commit()
            return index

    left, right = await asyncio.gather(worker(), worker())
    assert left == right

    async with Session() as check:
        count = await check.scalar(
            select(func.count())
            .select_from(ResearchBacktestRun)
            .where(
                ResearchBacktestRun.strategy_experiment_id == exp_pk,
                ResearchBacktestRun.trial_idempotency_key == key,
            )
        )
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_distinct_trials_get_distinct_indices(
    registry_tables,
) -> None:
    # Different idempotency keys → two trials with distinct monotonic indices;
    # the trial_index UNIQUE constraint drives the allocation retry.
    from app.core.db import engine

    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with Session() as setup:
        exp = await reg.register_experiment(setup, _identity())
        await setup.commit()
        experiment_id = exp.experiment_id
        exp_pk = exp.id

    async def worker(tag: str) -> int:
        async with Session() as s:
            row = await reg.record_trial(
                s,
                experiment_id=experiment_id,
                request=_trial(
                    "completed", idempotency_key=f"{tag}-{uuid.uuid4().hex}"
                ),
            )
            index = row.trial_index
            await s.commit()
            return index

    indices = sorted(await asyncio.gather(worker("a"), worker("b")))
    assert indices == [1, 2]

    async with Session() as check:
        total = await check.scalar(
            select(func.count())
            .select_from(ResearchBacktestRun)
            .where(ResearchBacktestRun.strategy_experiment_id == exp_pk)
        )
    assert total == 2
