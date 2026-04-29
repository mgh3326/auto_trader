"""ROB-26 forbidden-import safety test."""

import importlib

import pytest

FORBIDDEN_PREFIXES = (
    "prefect",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.order_service",
    "app.services.orders",
    "app.services.paper_trading_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.crypto_trade_cooldown_service",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.upbit_market_websocket",
    "app.services.watch_alerts",
    "app.services.screener_service",
    "app.services.tradingagents_research_service",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.paper_order_handler",
    "app.mcp_server.tooling.watch_alerts_registration",
)

# Source-text check of the NEW PR modules only — we check the file source,
# not sys.modules, to avoid false positives from transitive imports in the
# live-refresh provider.
MODULES_UNDER_TEST = (
    "app.jobs.research_run_refresh_runner",
    "app.tasks.research_run_refresh_tasks",
)


@pytest.mark.parametrize("module_name", MODULES_UNDER_TEST)
def test_module_does_not_import_forbidden(module_name: str) -> None:
    module = importlib.import_module(module_name)
    src = open(module.__file__).read()
    for forbidden in FORBIDDEN_PREFIXES:
        assert f"import {forbidden}" not in src, f"{module_name} imports {forbidden}"
        assert f"from {forbidden}" not in src, f"{module_name} imports from {forbidden}"
