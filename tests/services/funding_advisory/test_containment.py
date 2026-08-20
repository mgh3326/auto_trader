from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROTECTED_ORDER_FILES = (
    ROOT / "app/services/order_proposals/buying_power.py",
    ROOT / "app/services/order_proposals/revalidation.py",
    ROOT / "app/services/order_proposals/auto_approve.py",
    ROOT / "app/services/support_reserve_net_consumer.py",
    ROOT / "app/services/order_proposals/dispatch.py",
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


def test_private_advisory_repository_is_only_imported_by_advisory_service() -> None:
    offenders: list[Path] = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "_repository.py":
            continue
        if (
            "app.services.funding_advisory._repository" in imported_modules(path)
            and path != ROOT / "app/services/funding_advisory/service.py"
        ):
            offenders.append(path)
    assert offenders == []


def test_funding_router_is_registered_without_order_or_broker_imports() -> None:
    main_text = (ROOT / "app/main.py").read_text(encoding="utf-8")
    router_path = ROOT / "app/routers/invest_funding.py"
    assert "app.include_router(invest_funding.router)" in main_text
    assert router_path.exists()
    imported = imported_modules(router_path)
    assert not any(
        module.startswith("app.services.order_proposals")
        or module.startswith("app.services.brokers")
        or module.startswith("app.mcp_server.tooling")
        for module in imported
    )


def test_funding_package_has_no_proposal_create_or_broker_dependency() -> None:
    offenders: list[tuple[Path, str]] = []
    for path in (ROOT / "app/services/funding_advisory").rglob("*.py"):
        for module in imported_modules(path):
            if (
                module.startswith("app.services.order_proposals")
                or module.startswith("app.services.brokers")
                or module.startswith("app.mcp_server.tooling")
            ):
                offenders.append((path, module))
    assert offenders == []

    for path in (ROOT / "app/services/funding_advisory").rglob("*.py"):
        assert "create_proposal(" not in path.read_text(encoding="utf-8"), path


def test_provenance_identifier_is_absent_from_decision_classifiers() -> None:
    for path in PROTECTED_ORDER_FILES:
        assert "source_funding_advisory_id" not in path.read_text(encoding="utf-8")


def test_provenance_table_has_no_classification_or_sizing_columns() -> None:
    model_text = (ROOT / "app/models/funding_advisory.py").read_text(encoding="utf-8")
    link_model = model_text.split("class FundingAdvisoryProposalLink", 1)[1]
    assert "rationale" not in link_model
    assert "quantity" not in link_model
    assert "notional" not in link_model
    assert "eligibility" not in link_model


JIT_VOCABULARY = (
    "jit_funding",
    "deferred_with_condition",
    "declared_total",
    "build_jit_funding",
)


def test_jit_vocabulary_is_absent_from_order_decision_surfaces() -> None:
    """The JIT disposition is a notification, never a buying-power input."""

    for path in PROTECTED_ORDER_FILES:
        text = path.read_text(encoding="utf-8")
        for token in JIT_VOCABULARY:
            assert token not in text, (path, token)


def test_jit_module_is_pure_and_has_no_persistence_or_mutation() -> None:
    path = ROOT / "app/services/funding_advisory/jit.py"
    text = path.read_text(encoding="utf-8")
    imported = imported_modules(path)

    assert not any(
        module.startswith(
            (
                "app.services.order_proposals",
                "app.services.brokers",
                "app.mcp_server",
                "app.models",
                "app.core.db",
                "sqlalchemy",
            )
        )
        for module in imported
    ), imported
    for forbidden in ("session", "commit(", "insert", "update", "await "):
        assert forbidden not in text, forbidden


def test_declared_total_never_reaches_the_shortfall_expression() -> None:
    """``shortfall`` is required_cash - target_buying_power and nothing else."""

    service_text = (ROOT / "app/services/funding_advisory/service.py").read_text(
        encoding="utf-8"
    )

    assert 'shortfall = max(required - target, Decimal("0"))' in service_text, (
        "shortfall formula changed; re-verify the no-auto-add contract"
    )
    assert (
        "operational_gap = max(\n"
        "            required\n"
        "            + assessment.other_pending_required\n"
        "            + assessment.reserved_cash\n"
        "            - target,\n"
        '            Decimal("0"),\n'
        "        )" in service_text
    ), "operational gap formula changed; re-verify the no-auto-add contract"
    for line in service_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("shortfall =", "operational_gap =", "required =")):
            assert "declared" not in stripped, stripped
