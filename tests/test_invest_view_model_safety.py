"""Safety: invest_view_model package must not import broker/order/mutation paths."""
from __future__ import annotations

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
    "app.routers.portfolio_actions",
    "app.routers.order_estimation",
    "app.routers.order_previews",
    "app.routers.pending_orders",
    "app.routers.watch_order_intent_ledger",
]


@pytest.mark.unit
def test_invest_view_model_does_not_import_execution_paths() -> None:
    project_root = Path(__file__).resolve().parent.parent
    script = """
import importlib, json, sys
import app.services.invest_view_model.relation_resolver
import app.services.invest_view_model.account_visual
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root, env=env, check=True, capture_output=True, text=True,
    )
    loaded = set(json.loads(result.stdout))
    violations = sorted(
        m for m in loaded for f in FORBIDDEN_PREFIXES if m == f or m.startswith(f"{f}.")
    )
    if violations:
        pytest.fail(f"Forbidden execution-path imports: {violations}")
