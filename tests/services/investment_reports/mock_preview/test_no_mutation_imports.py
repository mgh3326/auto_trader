"""ROB-373 Unit 5 — safety guards for the mock_preview report path.

Proves: (1) no broker/order/execution mutation imports under
app/services/investment_reports/mock_preview/, (2) the bridge never enables
order submission, (3) the snapshot-backed generator's live-only guard still
rejects account_scope='kis_mock' (ROB-373 did not relax it).
"""

import ast
import pathlib

import pytest

_PKG = pathlib.Path("app/services/investment_reports/mock_preview")

_BANNED_PREFIXES = (
    "app.services.order_service",
    "app.services.kis_trading_service",
    "app.services.kis_trading_contracts",
    "app.services.kis_websocket",
    "app.services.upbit_websocket",
    "app.services.fill_notification",
    "app.services.execution_event",
    "app.services.alpaca_paper_ledger_service",
    "app.services.brokers.kis.mock_scalping_exec",
    "app.tasks",
)


def _imports_in_file(py: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(py.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module.startswith(p) for p in _BANNED_PREFIXES):
                offenders.append(f"{py}: from {module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in _BANNED_PREFIXES):
                    offenders.append(f"{py}: import {alias.name}")
    return offenders


@pytest.mark.unit
def test_scanner_detects_a_synthetic_offender(tmp_path: pathlib.Path) -> None:
    """Meta-test: the AST scanner must actually flag a banned import."""
    bad = tmp_path / "bad.py"
    bad.write_text("from app.services.order_service import place_order\n")
    offenders = _imports_in_file(bad)
    assert offenders, "scanner failed to flag a known-banned import"
    # also catch the bare `import` form
    bad2 = tmp_path / "bad2.py"
    bad2.write_text("import app.tasks\n")
    assert _imports_in_file(bad2)


@pytest.mark.unit
def test_mock_preview_pkg_has_no_mutation_imports() -> None:
    offenders: list[str] = []
    for py in _PKG.rglob("*.py"):
        offenders.extend(_imports_in_file(py))
    assert offenders == [], f"mutation imports found: {offenders}"


@pytest.mark.unit
def test_bridge_never_enables_submit() -> None:
    """Static guarantee: the source asserts submit_enabled=False, never True."""
    src = (_PKG / "bridge.py").read_text()
    assert 'submit_enabled"] = False' in src or "submit_enabled'] = False" in src
    assert "submit_enabled=True" not in src


@pytest.mark.asyncio
async def test_generator_guard_still_rejects_kis_mock(monkeypatch) -> None:
    """ROB-373 must NOT relax the snapshot-backed generator's live-only guard."""
    from app.core.config import settings as _settings

    monkeypatch.setattr(
        _settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    from app.mcp_server.tooling.investment_reports_handlers import (
        investment_report_generate_from_bundle_impl,
    )

    result = await investment_report_generate_from_bundle_impl(
        market="us",
        account_scope="kis_mock",
        title="guard test",
        summary="guard test summary",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    assert result["success"] is False
    assert result["error"] == "unsupported_account_scope"
