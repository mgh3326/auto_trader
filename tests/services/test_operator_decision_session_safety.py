"""Safety checks for operator decision session orchestration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.watch_alerts_registration",
    "app.tasks",
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


def _forbidden_violations(loaded: set[str]) -> list[str]:
    return sorted(
        name
        for name in loaded
        for forbidden in _FORBIDDEN_PREFIXES
        if name == forbidden or name.startswith(f"{forbidden}.")
    )


@pytest.mark.unit
def test_service_module_does_not_import_forbidden_prefixes_in_subprocess():
    loaded = _loaded_modules_after_import(
        "app.services.operator_decision_session_service"
    )
    violations = _forbidden_violations(loaded)
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")


@pytest.mark.unit
def test_schema_module_does_not_import_forbidden_prefixes_in_subprocess():
    loaded = _loaded_modules_after_import("app.schemas.operator_decision_session")
    violations = _forbidden_violations(loaded)
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")


@pytest.mark.unit
def test_crypto_mapping_module_does_not_import_forbidden_prefixes_in_subprocess():
    loaded = _loaded_modules_after_import("app.services.crypto_execution_mapping")
    violations = _forbidden_violations(loaded)
    if violations:
        pytest.fail(f"forbidden modules transitively imported: {violations}")


@pytest.mark.unit
def test_url_helper_module_has_no_settings_or_db_imports_in_subprocess():
    loaded = _loaded_modules_after_import("app.services.trading_decision_session_url")
    forbidden = [
        "app.core.config",
        "app.core.db",
        "redis",
        "httpx",
        "sqlalchemy",
    ]
    violations = sorted(
        name
        for name in loaded
        for prefix in forbidden
        if name == prefix or name.startswith(f"{prefix}.")
    )
    if violations:
        pytest.fail(f"URL helper pulled in heavyweight imports: {violations}")


@pytest.mark.asyncio
async def test_orchestrator_invokes_only_allowlisted_helpers(monkeypatch):
    import app.services.operator_decision_session_service as svc
    from app.schemas.operator_decision_session import (
        OperatorCandidate,
        OperatorDecisionRequest,
    )

    allowed_create = AsyncMock(
        return_value=SimpleNamespace(
            id=1, session_uuid="x", status="open", market_brief={}
        )
    )
    allowed_add = AsyncMock(return_value=[SimpleNamespace(id=1)])
    monkeypatch.setattr(
        svc.trading_decision_service, "create_decision_session", allowed_create
    )
    monkeypatch.setattr(
        svc.trading_decision_service, "add_decision_proposals", allowed_add
    )

    forbidden_names = (
        "place_order",
        "_place_order_impl",
        "register_watch_alert",
        "register_watch_alert_tools",
        "create_order_intent",
        "submit_order",
    )
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("app."):
            continue
        for symbol in forbidden_names:
            if hasattr(mod, symbol):
                monkeypatch.setattr(
                    mod,
                    symbol,
                    AsyncMock(side_effect=AssertionError(f"forbidden: {symbol}")),
                    raising=False,
                )

    req = OperatorDecisionRequest(
        market_scope="kr",
        candidates=[
            OperatorCandidate(
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                confidence=50,
                proposal_kind="enter",
            )
        ],
    )
    await svc.create_operator_decision_session(
        SimpleNamespace(), user_id=1, request=req
    )

    allowed_create.assert_awaited_once()
    allowed_add.assert_awaited_once()
