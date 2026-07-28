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

import copy
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
from app.services.research_canonical_hash import (  # noqa: E402
    compute_identity_hashes,
    derive_experiment_id,
)


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


def _unique_supersession_pair():
    """One exact H2 parent plus its H3-code-only child, namespaced per test."""
    import artifact as art
    import registry_cli as cli

    plan = cli.build_registration_plan()
    spec = copy.deepcopy(plan["specs"][0])
    config_id = spec["config_id"]
    parent_components = copy.deepcopy(
        art.build_sealed_artifact().to_dict()["identity_components"][config_id]
    )
    suffix = uuid.uuid4().hex[:8]
    version = f"{spec['strategy_version']}-{suffix}"
    spec["strategy_version"] = version
    spec["components"]["strategy"]["strategy_version"] = version
    parent_components["strategy"]["strategy_version"] = version
    spec["supersedes_experiment_id"] = derive_experiment_id(
        spec["strategy_key"],
        version,
        compute_identity_hashes(parent_components),
    )
    return {"specs": [spec]}, parent_components


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_h3_code_only_supersession_path_calls_guard_and_creates_new_row(
    registry_tables,
    monkeypatch,
) -> None:
    import registry_cli as cli

    session = registry_tables
    plan, parent_components = _unique_supersession_pair()
    spec = plan["specs"][0]
    parent_identity = StrategyExperimentIdentity(
        strategy_key=spec["strategy_key"],
        strategy_version=spec["strategy_version"],
        hypothesis=f"ROB-1060 H2 seal: {spec['config_id']}",
        **parent_components,
    )
    parent = await reg.register_experiment(session, parent_identity)
    await session.flush()
    assert parent.experiment_id == spec["supersedes_experiment_id"]

    guard_calls = []
    real_guard = cli.ident.assert_supersession_preserves_sealed_components

    def guard_spy(*, child_components, parent_components):
        guard_calls.append((child_components, parent_components))
        real_guard(
            child_components=child_components,
            parent_components=parent_components,
        )

    monkeypatch.setattr(
        cli.ident, "assert_supersession_preserves_sealed_components", guard_spy
    )
    registered = await cli._register_supersession_plan(
        session=session,
        plan=plan,
        registry=reg,
        identity_type=StrategyExperimentIdentity,
    )
    await session.flush()

    child = await reg._get_experiment(session, registered[0]["experiment_id"])
    assert child is not None
    assert child.supersedes_experiment_id == parent.experiment_id
    assert child.experiment_id != parent.experiment_id
    assert len(guard_calls) == 1
    assert child.code_hash != parent.code_hash
    for name in (
        "strategy",
        "params",
        "dataset_manifest",
        "universe",
        "pit",
        "frozen_config",
        "policy",
        "benchmark",
        "cost",
        "mdd",
    ):
        assert getattr(child, f"{name}_hash") == getattr(parent, f"{name}_hash")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersession_path_rejects_changed_sealed_component_before_register(
    registry_tables,
    monkeypatch,
) -> None:
    import identity as identity_module
    import registry_cli as cli

    session = registry_tables
    plan, parent_components = _unique_supersession_pair()
    spec = plan["specs"][0]
    parent = await reg.register_experiment(
        session,
        StrategyExperimentIdentity(
            strategy_key=spec["strategy_key"],
            strategy_version=spec["strategy_version"],
            hypothesis=f"ROB-1060 H2 seal: {spec['config_id']}",
            **parent_components,
        ),
    )
    await session.flush()
    assert parent.experiment_id == spec["supersedes_experiment_id"]
    spec["components"]["cost"] = {
        **spec["components"]["cost"],
        "primary": "C50",
    }

    register_calls = 0
    real_register = reg.register_experiment

    async def register_spy(session, identity):
        nonlocal register_calls
        register_calls += 1
        return await real_register(session, identity)

    monkeypatch.setattr(reg, "register_experiment", register_spy)
    with pytest.raises(
        identity_module.SupersessionSealedComponentDivergenceError,
        match="cost",
    ):
        await cli._register_supersession_plan(
            session=session,
            plan=plan,
            registry=reg,
            identity_type=StrategyExperimentIdentity,
        )
    assert register_calls == 0
