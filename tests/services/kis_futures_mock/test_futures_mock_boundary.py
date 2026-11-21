"""hk 5946 — 정적 경계: 이 경로가 어디에 닿고 어디에 닿지 않는가 (AST·텍스트, I/O 없음)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "app/services/kis_futures_mock"
ORDER_NAMES = ("futures_place_order", "futures_cancel_order", "futures_orders")


def py_files(base: Path):
    return [p for p in base.rglob("*.py") if ".venv" not in p.parts]


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_order_methods_are_referenced_only_by_the_futures_mock_lane():
    allowed = {
        ROOT / "app/services/brokers/kis/client.py",
        ROOT / "app/services/brokers/kis/futures_orders.py",
        ROOT / "scripts/kis_futures_mock_order.py",
        *PKG.glob("*.py"),
    }
    offenders = []
    for base in (ROOT / "app", ROOT / "scripts"):
        for path in py_files(base):
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if any(name in text for name in ORDER_NAMES):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_t1_read_view_still_cannot_reach_the_order_methods():
    from app.services.kis_futures_read.view import ALLOWED_METHODS

    assert not {"futures_place_order", "futures_cancel_order"} & set(ALLOWED_METHODS)
    from app.services.kis_futures_read.view import read_only_view

    class Target:
        async def futures_place_order(self, **_):
            raise AssertionError("must be unreachable")

    with pytest.raises(AttributeError):
        read_only_view(Target()).futures_place_order


def test_lane_never_touches_execution_ledger_mcp_or_schedulers():
    forbidden = re.compile(
        r"execution_ledger|ExecutionLedger|taskiq|prefect|app\.mcp_server|apscheduler|"
        r"app\.tasks|app\.flows|crontab",
        re.IGNORECASE,
    )
    for path in [
        *PKG.glob("*.py"),
        ROOT / "scripts/kis_futures_mock_order.py",
        ROOT / "app/services/brokers/kis/futures_orders.py",
    ]:
        text = path.read_text(encoding="utf-8")
        # 설계 문서 문구(주석·docstring)가 아니라 import 를 본다.
        bad = [m for m in imports_of(path) if forbidden.search(m)]
        assert bad == [], (path.name, bad)
        assert "ExecutionLedgerRepository" not in text.replace(
            "execution_ledger 에는", ""
        )


def test_no_mcp_registration_or_scheduler_wires_the_lane():
    for base in (ROOT / "app/mcp_server", ROOT / "app/tasks", ROOT / "app/flows"):
        if not base.exists():
            continue
        for path in py_files(base):
            assert "kis_futures_mock" not in path.read_text(encoding="utf-8"), path


def test_order_ledger_models_are_written_only_by_the_ledger_services():
    writers = {
        PKG / "ledger_service.py",
        PKG / "attribution.py",
        ROOT / "app/models/kis_futures_mock.py",
        ROOT / "app/models/__init__.py",
    }
    offenders = []
    for path in py_files(ROOT / "app"):
        if path in writers:
            continue
        text = path.read_text(encoding="utf-8")
        if "KISFuturesMockOrderLedger" in text or "KISFuturesMockSignalLedger" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_only_the_ledger_service_touches_the_order_send_intent_scope():
    for path in py_files(PKG):
        if path.name == "ledger_service.py":
            continue
        assert "OrderSendIntentService" not in path.read_text(encoding="utf-8").replace(
            "DuplicateOrderIntent", ""
        ), path.name


@pytest.mark.parametrize("module", ["rules.py", "session_calendar.py", "constants.py"])
def test_pure_modules_import_no_io(module):
    imported = imports_of(PKG / module)
    banned = (
        "sqlalchemy",
        "httpx",
        "requests",
        "asyncio",
        "redis",
        "app.core.db",
        "app.services.brokers",
    )
    assert [m for m in imported if m.startswith(banned)] == []


def test_the_futures_package_reads_env_only_for_the_gate_and_account():
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"os\.environ(?:\.get)?\(\s*([^),\]]+)", text):
            assert match.group(1).strip() in {"C.ENABLED_ENV", "C.ACCOUNT_ENV"}, (
                path.name,
                match.group(1),
            )
