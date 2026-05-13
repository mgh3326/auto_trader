from __future__ import annotations

from pathlib import Path

SERVICE_DIR = Path("app/services/execution_ledger")
FORBIDDEN_SNIPPETS = (
    "place_order",
    "place_stock_order",
    "cancel_order",
    "modify_order",
    "order_cash",
    "order_overseas_stock",
    "submit_order",
)


def test_execution_ledger_service_has_no_broker_mutation_imports() -> None:
    offenders: list[str] = []
    for path in SERVICE_DIR.rglob("*.py"):
        text = path.read_text()
        for snippet in FORBIDDEN_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path}:{snippet}")
    assert offenders == []


def test_execution_ledger_repository_is_only_insert_surface() -> None:
    offenders: list[str] = []
    for path in SERVICE_DIR.rglob("*.py"):
        if path.name == "repository.py":
            continue
        text = path.read_text()
        if "insert(" in text or ".add(ExecutionLedger" in text:
            offenders.append(str(path))
    assert offenders == []
