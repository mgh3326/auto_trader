"""Safety: pending reconciliation service must stay pure.

Modeled on tests/services/test_operator_decision_session_safety.py — runs
the import in a clean subprocess and inspects sys.modules to verify the
service does not transitively pull in broker, order-execution, watch-alert,
paper-order, fill-notification, KIS-websocket, DB, or Redis modules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_FORBIDDEN_PREFIXES = [
    "app.services.kis",
    "app.services.upbit",
    "app.services.brokers",
    "app.services.order_service",
    "app.services.orders",
    "app.services.watch_alerts",
    "app.services.paper_trading_service",
    "app.services.openclaw_client",
    "app.services.crypto_trade_cooldown_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.redis_token_manager",
    "app.services.kis_websocket",
    "app.services.screener_service",
    "app.services.n8n_pending_orders_service",
    "app.services.n8n_pending_review_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
    "app.core.db",
    "redis",
    "httpx",
    "sqlalchemy",
]


def _loaded_modules_after_import(module_name: str) -> set[str]:
    project_root = Path(__file__).resolve().parents[2]
    script = f"""
import importlib
import json
import sys

importlib.import_module({module_name!r})
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
    return set(json.loads(result.stdout))


@pytest.mark.unit
def test_pending_reconciliation_service_is_pure() -> None:
    loaded = _loaded_modules_after_import("app.services.pending_reconciliation_service")
    violations = sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")
