"""Assertion-RED mutants for every branch of the new H5 script guards.

Branches are counted from the three scripts ON DISK: every ``if`` inside the
functions listed in ``TARGETS``. Each mutant compiles a copy of the script with
ONE condition forced false and runs the scenario only that branch should
satisfy; the real script passes it, the mutant must fail it. A new branch
without a declared invariant fails ``test_every_branch_has_a_mutant``; sentences
and mutants must match (``test_invariant_sentences_match_the_mutants``).

Invariant sentences (one per mutant):
- WATCH_GATE: the watcher refuses to run unless the alert flag is exactly true.
- GATE_CONFIRM: the truth gate refuses to run without --confirm-demo.
- GATE_H5_FLAG: the truth gate refuses to run unless the H5 flag is exactly true.
- GATE_FUTURES_FLAG: the truth gate refuses to run unless the Futures Demo flag
  is exactly true.
- ONCE_EXITS: --once runs exactly one tick and returns its exit code.
- CANCEL_NOT_OURS: a cancellation this script did not request is never swallowed.
- NO_TASK_NO_HANDLERS: signal handlers are never installed outside a task.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {
    "watch": REPO_ROOT / "scripts/binance_h5_heartbeat_watch.py",
    "gate": REPO_ROOT / "scripts/binance_h5_truth_gate.py",
    "runner": REPO_ROOT / "scripts/binance_h5_demo.py",
}
TARGETS = {
    "watch": {"_guard"},
    "gate": {"_guard_cli"},
    "runner": {"_run_ticks", "_install_stop_signals"},
}


class Done(BaseException):
    pass


class Session:
    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def run_one_tick_scenario(m: types.ModuleType, results: list[Any]) -> Any:
    """Drive ``m._run_ticks`` with a scripted executor; ends with ``Done``."""
    queue = list(results)

    class Executor:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run_tick(self, *, now: Any, confirm: bool) -> Any:
            if not queue:
                raise Done
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    async def no_sleep(seconds: float) -> None:
        return None

    saved = (m.AsyncSessionLocal, m.BinanceDemoLedgerService, m.H5Executor)
    saved_sleep = m.asyncio.sleep
    m.AsyncSessionLocal = lambda: Session()
    m.BinanceDemoLedgerService = lambda db: object()
    m.H5Executor = Executor
    m.asyncio.sleep = no_sleep
    try:
        args = argparse.Namespace(once=True, loop=False, confirm_demo=True)
        monitor = m.H5RunMonitor(m.H5Alerter(channel=None, enabled=False))
        return asyncio.run(
            m._run_ticks(
                args,
                client=SimpleNamespace(),
                strategy=SimpleNamespace(),
                state=SimpleNamespace(),
                monitor=monitor,
                stop=m._StopState(),
            )
        )
    finally:
        m.AsyncSessionLocal, m.BinanceDemoLedgerService, m.H5Executor = saved
        m.asyncio.sleep = saved_sleep


def tick(m: types.ModuleType, event: str) -> Any:
    from app.services.brokers.binance.h5.executor import H5TickResult

    return H5TickResult(1, event)


def with_env(**values: str | None):
    class Ctx:
        def __enter__(self) -> None:
            self.saved = {k: os.environ.get(k) for k in values}
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def __exit__(self, *exc: object) -> None:
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return Ctx()


def sc_watch_gate(m: types.ModuleType) -> None:
    with with_env(BINANCE_H5_ALERT_ENABLED="false"):
        with pytest.raises(SystemExit):
            m._guard(argparse.Namespace())


def _gate(m: types.ModuleType, *, confirm: bool, h5: str, futures: str) -> None:
    with with_env(BINANCE_H5_DEMO_ENABLED=h5, BINANCE_FUTURES_DEMO_ENABLED=futures):
        with pytest.raises(SystemExit):
            m._guard_cli(argparse.Namespace(confirm_demo=confirm))


def sc_gate_confirm(m: types.ModuleType) -> None:
    _gate(m, confirm=False, h5="true", futures="true")


def sc_gate_h5(m: types.ModuleType) -> None:
    _gate(m, confirm=True, h5="false", futures="true")


def sc_gate_futures(m: types.ModuleType) -> None:
    _gate(m, confirm=True, h5="true", futures="false")


def sc_once_exits(m: types.ModuleType) -> None:
    assert run_one_tick_scenario(m, [tick(m, "no_entry")]) == 0


def sc_cancel_not_ours(m: types.ModuleType) -> None:
    with pytest.raises(asyncio.CancelledError):
        run_one_tick_scenario(m, [asyncio.CancelledError()])


def sc_no_task_no_handlers(m: types.ModuleType) -> None:
    stop = m._StopState()

    async def main() -> None:
        asyncio.get_running_loop().call_soon(m._install_stop_signals, stop)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert stop.installed is False


# (script, function, condition source) -> (key, scenario)
DECLARED: dict[tuple[str, str, str], tuple[str, Callable[[types.ModuleType], None]]] = {
    ("watch", "_guard", "not alert_enabled(os.environ)"): ("WATCH_GATE", sc_watch_gate),
    ("gate", "_guard_cli", "not args.confirm_demo"): ("GATE_CONFIRM", sc_gate_confirm),
    (
        "gate",
        "_guard_cli",
        "os.environ.get('BINANCE_H5_DEMO_ENABLED') != 'true'",
    ): ("GATE_H5_FLAG", sc_gate_h5),
    (
        "gate",
        "_guard_cli",
        "os.environ.get('BINANCE_FUTURES_DEMO_ENABLED') != 'true'",
    ): ("GATE_FUTURES_FLAG", sc_gate_futures),
    ("runner", "_run_ticks", "not args.loop"): ("ONCE_EXITS", sc_once_exits),
    ("runner", "_run_ticks", "not stop.installed"): (
        "CANCEL_NOT_OURS",
        sc_cancel_not_ours,
    ),
    ("runner", "_install_stop_signals", "task is None"): (
        "NO_TASK_NO_HANDLERS",
        sc_no_task_no_handlers,
    ),
}


def _branches(name: str, tree: ast.Module) -> list[tuple[tuple[str, str, str], ast.If]]:
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in TARGETS[name]
        ):
            for child in ast.walk(node):
                if isinstance(child, ast.If):
                    found.append(((name, node.name, ast.unparse(child.test)), child))
    return found


def _mutant(target: tuple[str, str, str]) -> types.ModuleType:
    name = target[0]
    tree = ast.parse(SCRIPTS[name].read_text("utf-8"))
    hits = [n for t, n in _branches(name, tree) if t == target]
    assert len(hits) == 1, target
    hits[0].test = ast.Constant(False)
    ast.fix_missing_locations(tree)
    module_name = f"h5_script_mutant_{name}_{abs(hash(target)) % 10**8}"
    module = types.ModuleType(module_name)
    module.__file__ = str(SCRIPTS[name])
    sys.modules[module_name] = module
    try:
        exec(compile(tree, str(SCRIPTS[name]), "exec"), module.__dict__)  # noqa: S102
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _real(name: str) -> types.ModuleType:
    import importlib

    return importlib.import_module(
        {
            "watch": "scripts.binance_h5_heartbeat_watch",
            "gate": "scripts.binance_h5_truth_gate",
            "runner": "scripts.binance_h5_demo",
        }[name]
    )


def test_every_branch_has_a_mutant():
    on_disk = {
        target
        for name in SCRIPTS
        for target, _ in _branches(name, ast.parse(SCRIPTS[name].read_text("utf-8")))
    }
    assert on_disk == set(DECLARED), {
        "undeclared": sorted(on_disk - set(DECLARED)),
        "stale": sorted(set(DECLARED) - on_disk),
    }


def test_invariant_sentences_match_the_mutants():
    keys = [
        line[2:].split(":", 1)[0]
        for line in (__doc__ or "").splitlines()
        if line.startswith("- ") and ": " in line
    ]
    declared = [key for key, _ in DECLARED.values()]
    assert sorted(keys) == sorted(declared)
    assert len(set(declared)) == len(declared) == 7


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_scenario_passes_on_the_real_script(target):
    DECLARED[target][1](_real(target[0]))


@pytest.mark.parametrize("target", sorted(DECLARED), ids=lambda t: DECLARED[t][0])
def test_mutant_is_killed_by_its_invariant(target):
    mutant = _mutant(target)
    try:
        with pytest.raises(BaseException) as killed:  # noqa: PT011
            DECLARED[target][1](mutant)
        assert not isinstance(killed.value, KeyboardInterrupt)
    finally:
        sys.modules.pop(mutant.__name__, None)
