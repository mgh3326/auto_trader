"""Safety test to verify research_run_decision_sessions router does not import mutation paths."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FORBIDDEN_MUTATION_PREFIXES = [
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.paper_order_handler",
    "app.tasks",
]


@pytest.mark.unit
def test_router_module_does_not_import_mutation_paths():
    """Import the router in a fresh process and check transitive imports."""
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib
import json
import sys

importlib.import_module("app.routers.research_run_decision_sessions")
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
    loaded_modules = set(json.loads(result.stdout))

    violations = sorted(
        module
        for module in loaded_modules
        for forbidden in FORBIDDEN_MUTATION_PREFIXES
        if module == forbidden or module.startswith(f"{forbidden}.")
    )

    if violations:
        pytest.fail(f"Found forbidden mutation-path imports: {violations}")
