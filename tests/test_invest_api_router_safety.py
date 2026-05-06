"""Safety: invest_api router and invest_home_service must not import mutation paths.

Read-only KIS/Upbit/manual *holdings* services are allowed; only mutation modules are forbidden.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_MUTATION_MODULES = [
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.alpaca_paper_ledger_service",
    "app.services.weekend_crypto_paper_cycle_runner",
    "app.tasks",
]

ROUTER_FORBIDDEN_DIRECT = [
    "app.services.kis",
    "app.services.upbit",
]


def _loaded(module: str, project_root: Path) -> set[str]:
    script = f"import importlib, json, sys; importlib.import_module({module!r}); print(json.dumps(sorted(sys.modules)))"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run([sys.executable, "-c", script], cwd=project_root,
                            env=env, check=True, capture_output=True, text=True)
    return set(json.loads(result.stdout))


def _violations(loaded: set[str], forbidden: list[str]) -> list[str]:
    return sorted(m for m in loaded for f in forbidden if m == f or m.startswith(f"{f}."))


@pytest.mark.unit
def test_invest_api_router_no_mutation_imports() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.routers.invest_api", root)
    v = _violations(loaded, FORBIDDEN_MUTATION_MODULES + ROUTER_FORBIDDEN_DIRECT)
    if v:
        pytest.fail(f"Forbidden imports in invest_api: {v}")


@pytest.mark.unit
def test_invest_home_service_no_mutation_imports() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.services.invest_home_service", root)
    v = _violations(loaded, FORBIDDEN_MUTATION_MODULES)
    if v:
        pytest.fail(f"Forbidden imports in invest_home_service: {v}")
