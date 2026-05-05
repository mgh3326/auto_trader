"""ROB-107 forbidden-import safety test for committee decision modules.

The committee MVP must remain a read/preview surface only — it must not
import broker, KIS, watch, scheduler, or live trading modules. If any
committee module starts pulling those in, this test fails to surface it
before any safety boundary erodes silently.
"""

import importlib

import pytest

FORBIDDEN_PREFIXES = (
    "prefect",
    "app.kis",
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.alpaca_trading_service",
    "app.services.alpaca_paper_ledger_service",
    "app.services.order_service",
    "app.services.orders",
    "app.services.paper_trading_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.scheduler",
    "app.services.upbit_websocket",
    "app.services.upbit_market_websocket",
    "app.services.watch_alerts",
    "app.services.crypto_trade_cooldown_service",
    "app.services.weekend_crypto_paper_cycle_runner",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_history",
    "app.mcp_server.tooling.paper_order_handler",
    "app.mcp_server.tooling.watch_alerts_registration",
)

# Modules that implement the committee workflow surface and must remain
# free of any broker / scheduler / live-trading imports.
MODULES_UNDER_TEST = (
    "app.services.trading_decisions.committee_service",
    "app.schemas.trading_decisions",
)


@pytest.mark.unit
@pytest.mark.parametrize("module_name", MODULES_UNDER_TEST)
def test_committee_module_does_not_import_forbidden(module_name: str) -> None:
    module = importlib.import_module(module_name)
    src = open(module.__file__).read()
    for forbidden in FORBIDDEN_PREFIXES:
        assert f"import {forbidden}" not in src, (
            f"{module_name} imports forbidden module {forbidden}"
        )
        assert f"from {forbidden}" not in src, (
            f"{module_name} imports from forbidden module {forbidden}"
        )
