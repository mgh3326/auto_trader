"""Safety: research_run_service must not import broker/order/watch/paper/fill modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import research_run_service

FORBIDDEN_PREFIXES = [
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


@pytest.mark.unit
def test_research_run_service_does_not_transitively_import_forbidden() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import json
import sys

importlib.import_module('app.services.research_run_service')
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
        for forbidden in FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )
    assert not violations, f"forbidden modules transitively imported: {violations}"


@pytest.mark.unit
def test_news_brief_candidate_payload_rejects_execution_keys() -> None:
    for forbidden_key in [
        "quantity",
        "price",
        "side",
        "order_type",
        "dry_run",
        "watch",
        "order_intent",
    ]:
        with pytest.raises(ValueError, match="forbidden execution keys"):
            research_run_service._validate_news_brief_candidate_payload(  # noqa: SLF001
                {"symbol": "005930", forbidden_key: True}
            )


@pytest.mark.unit
def test_news_brief_candidate_payload_allows_advisory_only_fields() -> None:
    research_run_service._validate_news_brief_candidate_payload(  # noqa: SLF001
        {
            "symbol": "005930",
            "name": "삼성전자",
            "sector": "반도체",
            "direction": "positive",
            "confidence": 60,
            "reasons": ["news evidence"],
            "warnings": ["news_stale"],
        }
    )
