"""Safety isolation for /invest/api/feed/research (ROB-179).

GET /feed/research is read-only. Assert it does not pull in KIS trading,
Upbit trading, or broker/order mutation modules — even indirectly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_FORBIDDEN_MUTATION_MODULES = [
    "app.services.order_service",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.kis_websocket_internal",
    "app.services.upbit_websocket",
    "app.services.alpaca_paper_ledger_service",
    "app.services.weekend_crypto_paper_cycle_runner",
    "app.tasks",
]

_ROUTER_FORBIDDEN_DIRECT = [
    "app.services.kis",
    "app.services.upbit",
]


def _loaded(module: str, project_root: Path) -> set[str]:
    script = (
        f"import importlib, json, sys; importlib.import_module({module!r}); "
        "print(json.dumps(sorted(sys.modules)))"
    )
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


def _violations(loaded: set[str], forbidden: list[str]) -> list[str]:
    return sorted(
        m for m in loaded for f in forbidden if m == f or m.startswith(f"{f}.")
    )


@pytest.mark.unit
def test_feed_research_service_no_kis_dependency() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.services.invest_view_model.feed_research_service", root)
    v = _violations(loaded, _FORBIDDEN_MUTATION_MODULES + _ROUTER_FORBIDDEN_DIRECT)
    if v:
        pytest.fail(f"feed_research_service pulls in forbidden modules: {v}")


@pytest.mark.unit
def test_feed_research_service_no_upbit_dependency() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.services.invest_view_model.feed_research_service", root)
    upbit_violations = [
        m
        for m in loaded
        if m == "app.services.upbit" or m.startswith("app.services.upbit.")
    ]
    if upbit_violations:
        pytest.fail(f"feed_research_service pulls in Upbit modules: {upbit_violations}")


@pytest.mark.unit
def test_feed_research_service_no_broker_order_dependency() -> None:
    root = Path(__file__).resolve().parent.parent
    loaded = _loaded("app.services.invest_view_model.feed_research_service", root)
    broker_modules = ["app.services.order_service", "app.services.kis_trading_service"]
    v = _violations(loaded, broker_modules)
    if v:
        pytest.fail(f"feed_research_service pulls in broker/order modules: {v}")
