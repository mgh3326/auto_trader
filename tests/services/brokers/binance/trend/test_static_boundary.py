"""Static guards over the trend package: only ``executor.py`` can reach a broker."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[5]
PACKAGE = ROOT / "app/services/brokers/binance/trend"
PACKAGE_MODULE = "app.services.brokers.binance.trend"

# First-party imports every module may use: the package itself and its ORM models.
ALLOWED_FIRST_PARTY = ("app.models.binance_trend", PACKAGE_MODULE)
_BARS = "app.services.brokers.binance.demo_strategy_loop"
_FUTURES = "app.services.brokers.binance.futures_demo"
_ROB974 = "research.nautilus_scalping.rob974_features"
# Extra first-party imports per module. Everything not listed here is refused,
# so a new import of a broker, order or network module fails this test.
EXTRA_ALLOWED = {
    "strategy.py": (f"{_BARS}.strategy", _ROB974),
    "history.py": (f"{_BARS}.bars", _ROB974),
    "exposure.py": (f"{_FUTURES}.dto",),
    # CT-5a: alerting is the one module allowed to reach the ops webhook. It
    # sends a Discord embed and nothing else; no broker, account or order code.
    "alerting.py": (
        "app.core.config",
        "app.monitoring.trade_notifier.transports",
    ),
    "executor.py": (
        _ROB974,
        f"{_FUTURES}.dto",
        f"{_FUTURES}.execution_client",
        f"{_FUTURES}.signing",
        "app.services.brokers.binance.demo.errors",
        "app.services.brokers.binance.demo.quantity",
        "app.services.brokers.binance.demo.ledger.service",
    ),
}
# Client libraries only the alert sender may import (it posts one webhook).
NETWORK_ALLOWED = {"alerting.py": ("httpx",)}
EXECUTION_CLIENT_MODULES = (f"{_FUTURES}.execution_client", f"{_FUTURES}.signing")
# Anything that can open a socket or talk to a broker.
FORBIDDEN_THIRD_PARTY = (
    "httpx",
    "aiohttp",
    "requests",
    "websockets",
    "websocket",
    "urllib3",
    "urllib.request",
    "http.client",
    "socket",
    "asyncpg",
    "redis",
    "taskiq",
    "prefect",
)


def _imports(path: Path, *, in_package: bool = False) -> list[tuple[str, int]]:
    """Every imported module and ``module.name`` alias, relative imports resolved.

    ``from ..futures_demo import execution_client`` inside the package resolves
    to ``app.services.brokers.binance.futures_demo`` plus the alias
    ``...futures_demo.execution_client`` so the allowlists see the real target.
    """
    tree = ast.parse(path.read_text())
    package_parts = PACKAGE_MODULE.split(".")
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                if not in_package:
                    continue
                base = package_parts[: len(package_parts) - (node.level - 1)]
                module = ".".join([*base, module] if module else base)
            found.append((module, node.lineno))
            found += [(f"{module}.{alias.name}", node.lineno) for alias in node.names]
    return found


def _package_files() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert {p.name for p in files} >= {"__init__.py", "constants.py", "state.py"}
    return files


def test_every_module_imports_only_what_it_is_allowed_to() -> None:
    offenders: list[str] = []
    for path in _package_files():
        allowed = ALLOWED_FIRST_PARTY + EXTRA_ALLOWED.get(path.name, ())
        network = NETWORK_ALLOWED.get(path.name, ())
        for module, line in _imports(path, in_package=True):
            top = module.split(".")[0]
            # `from app.models import X` would hide a model import: refuse it too.
            first_party_ok = module.startswith(allowed) or any(
                a.startswith(module + ".") for a in allowed
            )
            research_ok = top == "research" and first_party_ok
            network_ok = any(
                module == allowed_module or module.startswith(allowed_module + ".")
                for allowed_module in network
            )
            if (top == "app" and not first_party_ok) or (
                top != "app"
                and (
                    (module.startswith(FORBIDDEN_THIRD_PARTY) and not network_ok)
                    or top == "scripts"
                    or (top == "research" and not research_ok)
                )
            ):
                offenders.append(f"{path.name}:{line} {module}")
    assert not offenders


def test_only_the_executor_imports_the_execution_client() -> None:
    offenders = []
    for path in _package_files():
        if path.name == "executor.py":
            continue
        for module, line in _imports(path, in_package=True):
            if module.startswith(EXECUTION_CLIENT_MODULES) or module.endswith(
                ".executor"
            ):
                offenders.append(f"{path.name}:{line} {module}")
    assert not offenders
    executor = _imports(PACKAGE / "executor.py", in_package=True)
    assert any(m == EXECUTION_CLIENT_MODULES[0] for m, _ in executor)


def test_only_the_executor_names_an_order_submission() -> None:
    banned = ("submit_order", "place_order", "new_order", "create_order", "newOrder")
    for path in _package_files():
        if path.name == "executor.py":
            continue
        source = path.read_text()
        for name in banned:
            assert name not in source, f"{path.name} mentions {name}"
    tree = ast.parse((PACKAGE / "executor.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit_order"
    ]
    # One send site (`self.client.submit_order`) and the gated super() call.
    assert len(calls) == 2


def test_only_the_trend_scripts_import_the_package() -> None:
    """No app module, task or scheduler reaches the trend package; only the
    operator CLI, watcher and truth-gate scripts do (CT-5a)."""
    offenders: list[str] = []
    for base in ("app", "scripts", "research"):
        for path in (ROOT / base).rglob("*.py"):
            if PACKAGE in path.parents:
                continue
            if base == "scripts" and path.name.startswith("binance_trend"):
                continue
            for module, _ in _imports(path):
                if module.startswith(PACKAGE_MODULE):
                    offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders


def test_the_state_service_is_the_only_writer_of_the_trend_tables() -> None:
    """Only the models, the migration, bootstrap and ``state.py`` name the tables."""
    markers = (
        "BinanceTrendSignal",
        "BinanceTrendIntent",
        "binance_trend_signals",
        "binance_trend_intents",
    )
    allowed = {
        PACKAGE / "state.py",
        ROOT / "app/models/binance_trend.py",
        ROOT / "app/models/__init__.py",
    }
    offenders = [
        str(path.relative_to(ROOT))
        for base in ("app", "scripts", "research")
        for path in (ROOT / base).rglob("*.py")
        if path not in allowed and any(m in path.read_text() for m in markers)
    ]
    assert not offenders


def test_only_the_executor_reads_the_environment_and_the_scripts_never_schedule() -> (
    None
):
    # ALERTING_READS_SETTINGS is the one exception: the ops webhook URL comes
    # from settings exactly like H5's alerting module.
    for path in _package_files():
        source = path.read_text()
        if path.name != "executor.py":
            for needle in ("os.environ", "getenv", "get_settings"):
                assert needle not in source, f"{path.name} reads {needle}"
            if path.name != "alerting.py":
                assert "settings." not in source, f"{path.name} reads settings."
        for needle in ("argparse", "__main__", "taskiq", "prefect", "crontab"):
            assert needle not in source, f"{path.name} wires {needle}"
    scripts = sorted(ROOT.glob("scripts/binance_trend*.py"))
    assert {p.name for p in scripts} == {
        "binance_trend_demo.py",
        "binance_trend_heartbeat_watch.py",
        "binance_trend_truth_gate.py",
    }
    for path in scripts:
        source = path.read_text()
        for needle in ("taskiq", "prefect", "crontab", "APScheduler", "apscheduler"):
            assert needle not in source, f"{path.name} wires {needle}"


def test_the_closed_exit_vocabulary_is_one_constant_used_by_gate_and_state() -> None:
    from app.services.brokers.binance.trend import constants, entry_gate, state

    assert constants.EXIT_REASONS == ("hard_stop", "signal_flip", "time_exit")
    assert entry_gate.EXIT_HARD_STOP is constants.EXIT_HARD_STOP
    assert state.EXIT_REASON_VOCABULARY is constants.EXIT_REASONS
    for name in ("hard_stop", "signal_flip", "time_exit"):
        # Literals live only in constants.py: everyone else uses the names.
        for path in _package_files():
            if path.name == "constants.py":
                continue
            assert f'"{name}"' not in path.read_text(), f"{path.name} hardcodes {name}"


def test_the_import_resolver_sees_relative_imports_and_aliases(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from ..futures_demo import execution_client\n"
        "from . import state\n"
        "from .state import TrendStateService\n"
        "from ...demo.ledger import service as ledger\n"
        "import httpx\n"
    )
    seen = {module for module, _ in _imports(sample, in_package=True)}
    assert "app.services.brokers.binance.futures_demo" in seen
    assert "app.services.brokers.binance.futures_demo.execution_client" in seen
    assert f"{PACKAGE_MODULE}.state" in seen
    assert f"{PACKAGE_MODULE}.state.TrendStateService" in seen
    assert "app.services.brokers.demo.ledger.service" in seen
    assert "httpx" in seen


def test_a_relative_import_of_the_execution_client_outside_the_executor_is_rejected(
    tmp_path,
) -> None:
    sample = tmp_path / "evil.py"
    sample.write_text("from ..futures_demo import execution_client\n")
    offenders = [
        m
        for m, _ in _imports(sample, in_package=True)
        if m.startswith(EXECUTION_CLIENT_MODULES)
    ]
    assert offenders == ["app.services.brokers.binance.futures_demo.execution_client"]
