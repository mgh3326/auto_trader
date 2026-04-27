from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

_FORBIDDEN_PREFIXES = [
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
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.tasks",
]


@pytest.mark.integration
def test_service_module_does_not_import_execution_paths() -> None:
    project_root = str(pathlib.Path(__file__).parent.parent.parent)
    service_file = str(
        pathlib.Path(__file__).parent.parent.parent
        / "app"
        / "services"
        / "tradingagents_research_service.py"
    )

    script = f"""
import sys
import types
import json
import importlib.util
import pathlib

project_root = {project_root!r}
service_file = {service_file!r}
sys.path.insert(0, project_root)

svc_stub = types.ModuleType("app.services")
svc_stub.__path__ = [str(pathlib.Path(project_root) / "app" / "services")]
svc_stub.__package__ = "app.services"
sys.modules.setdefault("app.services", svc_stub)

spec = importlib.util.spec_from_file_location(
    "app.services.tradingagents_research_service", service_file
)
mod = importlib.util.module_from_spec(spec)
sys.modules["app.services.tradingagents_research_service"] = mod
spec.loader.exec_module(mod)

print(json.dumps(sorted(sys.modules.keys())))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Subprocess import of tradingagents_research_service failed:\n{result.stderr}"
    )

    loaded: list[str] = json.loads(result.stdout)
    violations = [
        m
        for prefix in _FORBIDDEN_PREFIXES
        for m in loaded
        if m == prefix or m.startswith(prefix + ".")
    ]

    assert not violations, (
        "Forbidden module(s) loaded as a transitive consequence of importing "
        "tradingagents_research_service:\n" + "\n".join(violations)
    )
