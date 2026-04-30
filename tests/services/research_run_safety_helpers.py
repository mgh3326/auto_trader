"""Shared safety-test helpers for research run import boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RESEARCH_RUN_FORBIDDEN_PREFIXES = [
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
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_holdings_service",
    "app.services.upbit_websocket",
    "app.services.redis_token_manager",
    "app.services.n8n_pending_orders_service",
    "app.services.n8n_pending_review_service",
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
    "redis",
]

NEWS_BRIEF_FORBIDDEN_PREFIXES = [
    prefix
    for prefix in RESEARCH_RUN_FORBIDDEN_PREFIXES
    if prefix
    not in {
        "app.services.crypto_trade_cooldown_service",
        "app.services.kis_websocket_internal",
        "app.services.kis_trading_contracts",
        "app.services.kis_holdings_service",
        "app.services.n8n_pending_orders_service",
        "app.services.n8n_pending_review_service",
        "app.mcp_server.tooling.order_execution",
        "app.mcp_server.tooling.orders_history",
        "app.mcp_server.tooling.orders_modify_cancel",
        "app.mcp_server.tooling.orders_registration",
        "app.mcp_server.tooling.watch_alerts_registration",
    }
]


def assert_module_does_not_import_forbidden(
    module_name: str,
    forbidden_prefixes: list[str],
) -> None:
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
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        name
        for name in loaded
        for forbidden in forbidden_prefixes
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"
