"""Safety test: the SPA router must not import execution paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.tasks",
]


@pytest.mark.unit
def test_spa_router_module_does_not_import_execution_paths() -> None:
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib
import json
import sys

importlib.import_module("app.routers.trading_decisions_spa")
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        module
        for module in loaded
        for forbidden in FORBIDDEN_PREFIXES
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"Forbidden execution-path imports: {violations}")
