"""ROB-1069 — executable proof of the register-path supersession wiring."""

from __future__ import annotations

import argparse
import copy
import sys
import types


def test_cmd_register_fetches_parent_calls_guard_then_registers(
    monkeypatch,
    capsys,
):
    """Exercise the real ``_cmd_register`` loop without a DB or any write.

    Deferred app modules are replaced with in-memory ports.  The emitted proof
    lines come from a spy around the actual preservation authority and occur
    only after the fake stored-parent fetch, immediately before the fake
    ``register_experiment`` call.
    """
    import artifact as art
    import identity as ident
    import registry_cli as cli

    import app.core
    import app.schemas
    import app.services
    from research_contracts.canonical_hash import (
        compute_identity_hashes,
        derive_experiment_id,
    )

    sealed = art.build_sealed_artifact()
    parent_by_id = {}
    for config in sealed.configs:
        components = ident.build_components_for_config(config, sealed.params)
        parent_id = derive_experiment_id(
            components["strategy"]["strategy_key"],
            components["strategy"]["strategy_version"],
            compute_identity_hashes(components),
        )
        parent_by_id[parent_id] = components

    events = []
    pending_parent = {"id": None}
    real_guard = ident.assert_supersession_preserves_sealed_components

    def guard_spy(*, child_components, parent_components):
        parent_id = pending_parent["id"]
        if parent_id is not None:
            print(f"ROB-1069_GUARD_CALLED parent={parent_id}")
            events.append(("guard", parent_id))
            pending_parent["id"] = None
        real_guard(
            child_components=child_components,
            parent_components=parent_components,
        )

    monkeypatch.setattr(
        cli.ident, "assert_supersession_preserves_sealed_components", guard_spy
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def commit(self):
            events.append(("commit", None))

    class FakeIdentity:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeRow:
        def __init__(self, experiment_id):
            self.experiment_id = experiment_id

    fake_registry = types.ModuleType("app.services.strategy_experiment_registry")

    async def get_experiment_identity_components(_session, experiment_id):
        events.append(("fetch", experiment_id))
        pending_parent["id"] = experiment_id
        return copy.deepcopy(parent_by_id[experiment_id])

    async def register_experiment(_session, identity):
        parent_id = identity.kwargs["supersedes_experiment_id"]
        events.append(("register", parent_id))
        return FakeRow(f"child-{parent_id}")

    fake_registry.get_experiment_identity_components = (
        get_experiment_identity_components
    )
    fake_registry.register_experiment = register_experiment

    fake_db = types.ModuleType("app.core.db")
    fake_db.AsyncSessionLocal = FakeSession
    fake_schema = types.ModuleType("app.schemas.research_backtest")
    fake_schema.StrategyExperimentIdentity = FakeIdentity
    fake_write_guard = types.ModuleType("app.services.research_db_write_guard")
    fake_write_guard.research_write_opt_in_enabled = lambda _value: True
    fake_write_guard.resolve_research_db_target = lambda _session: object()
    fake_write_guard.default_research_db_policy = object
    fake_write_guard.assert_research_write_authorized = lambda **_kwargs: None

    monkeypatch.setitem(sys.modules, "app.core.db", fake_db)
    monkeypatch.setitem(sys.modules, "app.schemas.research_backtest", fake_schema)
    monkeypatch.setitem(
        sys.modules, "app.services.strategy_experiment_registry", fake_registry
    )
    monkeypatch.setitem(
        sys.modules, "app.services.research_db_write_guard", fake_write_guard
    )
    monkeypatch.setattr(app.core, "db", fake_db, raising=False)
    monkeypatch.setattr(app.schemas, "research_backtest", fake_schema, raising=False)
    monkeypatch.setattr(
        app.services, "strategy_experiment_registry", fake_registry, raising=False
    )
    monkeypatch.setattr(
        app.services, "research_db_write_guard", fake_write_guard, raising=False
    )
    monkeypatch.setenv(cli.ENV_WRITE_OPT_IN, "true")

    assert cli._cmd_register(argparse.Namespace(confirm=True)) == 0

    proof = capsys.readouterr().out
    assert proof.count("ROB-1069_GUARD_CALLED parent=") == 16
    path_events = [event for event in events if event[0] != "commit"]
    assert len(path_events) == 48
    for offset in range(0, len(path_events), 3):
        fetch, guard, register = path_events[offset : offset + 3]
        assert fetch[0] == "fetch"
        assert guard == ("guard", fetch[1])
        assert register == ("register", fetch[1])
    assert events[-1] == ("commit", None)
    with capsys.disabled():
        print(
            "ROB-1069_GUARD_CALL_PROOF "
            "count=16 order=fetch_parent_ast->guard->register_experiment"
        )
