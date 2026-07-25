"""ROB-1060 H2 — DB integration: the 16 sealed AP-A1/AP-A2 identities
register cleanly into the REAL ROB-846 immutable experiment registry.

This is the app-side half of the "is the ROB-846 registry reachable"
question research/alpaca_track_seal/registry_cli.py answers structurally:
here we prove the pure identity-component plan it builds actually produces
16 distinct, idempotent, ROB-846-schema-valid experiments when fed through
the real ``register_experiment`` service — against the local disposable
test_db only (``db_session``/``registry_tables``, same fixture pattern as
``tests/services/research/test_strategy_experiment_registry.py``). No real
network, no broker/order/fill access anywhere in this file.

Scope note (see ROB-1060 H2 completion report): this test exercises
``register_experiment`` directly with the CLI's OWN plan-building code
(imported from research/alpaca_track_seal), not the CLI's ``register``
subcommand end-to-end (which opens its own ``AsyncSessionLocal()`` outside
the test's rollback-wrapped transaction — running that here would either
require a second, unmanaged real DB connection or non-trivial monkeypatching
of session construction). The CLI's own gating (env opt-in, --confirm,
module-scope import boundary) is separately proven by
``research/alpaca_track_seal/tests/test_registry_cli_import_guard.py`` via
real subprocess invocations.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

# Make the pure research/alpaca_track_seal package importable from this
# app-side test tree (mirrors the sys.path pattern its own conftest.py uses).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEAL_PKG = _REPO_ROOT / "research" / "alpaca_track_seal"
_NAUTILUS_SCALPING = _REPO_ROOT / "research" / "nautilus_scalping"
for _p in (str(_SEAL_PKG), str(_NAUTILUS_SCALPING), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.schemas.research_backtest import StrategyExperimentIdentity  # noqa: E402
from app.services import strategy_experiment_registry as reg  # noqa: E402


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


def _unique_plan():
    """The real sealed plan, with a per-test-run unique strategy_version
    suffix so repeated test runs against a shared test_db don't collide with
    earlier runs' rows (mirrors test_strategy_experiment_registry.py's
    uuid-suffixed strategy_key/version pattern)."""
    import registry_cli as cli

    plan = cli.build_registration_plan()
    suffix = uuid.uuid4().hex[:8]
    for spec in plan["specs"]:
        spec["strategy_version"] = f"{spec['strategy_version']}-{suffix}"
    return plan


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_16_sealed_identities_register_as_16_distinct_experiments(
    registry_tables,
) -> None:
    session = registry_tables
    plan = _unique_plan()
    assert len(plan["specs"]) == 16

    experiment_ids = []
    for spec in plan["specs"]:
        identity = StrategyExperimentIdentity(
            strategy_key=spec["strategy_key"],
            strategy_version=spec["strategy_version"],
            hypothesis=f"ROB-1060 H2 seal: {spec['config_id']}",
            **spec["components"],
        )
        row = await reg.register_experiment(session, identity)
        await session.flush()
        experiment_ids.append(row.experiment_id)

    assert len(experiment_ids) == len(set(experiment_ids)) == 16


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registering_the_same_plan_twice_is_idempotent(registry_tables) -> None:
    session = registry_tables
    plan = _unique_plan()
    spec = plan["specs"][0]
    identity = StrategyExperimentIdentity(
        strategy_key=spec["strategy_key"],
        strategy_version=spec["strategy_version"],
        hypothesis=f"ROB-1060 H2 seal: {spec['config_id']}",
        **spec["components"],
    )
    first = await reg.register_experiment(session, identity)
    await session.flush()
    second = await reg.register_experiment(session, identity)
    assert first.experiment_id == second.experiment_id
    assert first.id == second.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ap_a1_config_cannot_supersede_an_ap_a2_experiment(
    registry_tables,
) -> None:
    """ROB-846 SupersedesStrategyMismatch lineage, exercised through the REAL
    registry against two actually-registered sealed identities (stronger
    than configs.py's pure assert_valid_supersedes unit check alone)."""
    session = registry_tables
    plan = _unique_plan()
    a1_spec = next(s for s in plan["specs"] if s["family"] == "AP-A1")
    a2_spec = next(s for s in plan["specs"] if s["family"] == "AP-A2")

    a1_identity = StrategyExperimentIdentity(
        strategy_key=a1_spec["strategy_key"],
        strategy_version=a1_spec["strategy_version"],
        hypothesis="seed",
        **a1_spec["components"],
    )
    a1_row = await reg.register_experiment(session, a1_identity)
    await session.flush()

    a2_identity = StrategyExperimentIdentity(
        strategy_key=a2_spec["strategy_key"],
        strategy_version=a2_spec["strategy_version"],
        hypothesis="seed",
        supersedes_experiment_id=a1_row.experiment_id,
        **a2_spec["components"],
    )
    with pytest.raises(reg.SupersedesStrategyMismatch):
        await reg.register_experiment(session, a2_identity)
