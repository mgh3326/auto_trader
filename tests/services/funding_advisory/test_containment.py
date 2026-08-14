from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROTECTED_ORDER_FILES = (
    ROOT / "app/services/order_proposals/buying_power.py",
    ROOT / "app/services/order_proposals/revalidation.py",
    ROOT / "app/services/order_proposals/auto_approve.py",
    ROOT / "app/services/support_reserve_net_consumer.py",
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_external_cash_cannot_enter_order_decision_surfaces() -> None:
    for path in PROTECTED_ORDER_FILES:
        imported = imported_modules(path)
        assert not any(
            module.startswith("app.services.funding_advisory")
            or module == "app.models.funding_advisory"
            for module in imported
        ), path


def test_private_repository_is_only_imported_by_external_cash_service() -> None:
    offenders: list[Path] = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "_external_cash_repository.py":
            continue
        if (
            "app.services.funding_advisory._external_cash_repository"
            in imported_modules(path)
            and path != ROOT / "app/services/funding_advisory/external_cash.py"
        ):
            offenders.append(path)
    assert offenders == []


def test_funding_foundation_has_no_router_or_runtime_consumer() -> None:
    main_text = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "invest_funding" not in main_text
    assert not (ROOT / "app/routers/invest_funding.py").exists()
