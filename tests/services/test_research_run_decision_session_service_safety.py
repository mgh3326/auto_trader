import subprocess
import sys

import pytest

FORBIDDEN_PREFIXES = [
    "app.services.brokers",
    "app.services.kis",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_holdings_service",
    "app.services.manual_holdings_service",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit",
    "app.services.upbit_websocket",
    "app.services.market_data",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.paper_order_handler",
    "app.tasks",
]


@pytest.mark.unit
def test_research_run_decision_session_service_forbidden_imports():
    cmd = [
        sys.executable,
        "-c",
        "import app.services.research_run_decision_session_service; import sys; print('\\n'.join(sys.modules.keys()))",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    loaded = result.stdout.splitlines()
    violations = [
        mod for mod in loaded if any(mod.startswith(p) for p in FORBIDDEN_PREFIXES)
    ]
    assert not violations, f"Forbidden imports detected: {violations}"
