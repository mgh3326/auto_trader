"""AST/static guards for ROB-850 paper evaluation.

Verifies that the paper_evaluation package:
* never imports concrete broker write services or live mutation clients,
* never calls broker write/mutation methods (record_*, claim_*, submit, etc.),
* never imports the ROB-848 state machine service (no promotion transitions),
* never performs USDT/USD conversion or assumes USDT=USD,
* never accesses live broker credentials or .env.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "app" / "services" / "paper_evaluation"

FORBIDDEN_IMPORT_FRAGMENTS = (
    "app.services.brokers.binance.demo.ledger.service",
    "app.services.brokers.binance.demo.ledger.repository",
    "app.services.brokers.binance.rest_client",
    "app.services.brokers.alpaca.service",
    "app.services.alpaca_paper_ledger_service",
    "app.services.alpaca_paper_submit_service",
    "app.services.alpaca_paper_order_application",
    "app.services.paper_validation.service",
    "app.services.paper_validation.state_machine",
    "app.services.brokers.binance.demo.credentials",
    "app.services.brokers.binance.demo.credential_identity",
    "app.services.brokers.alpaca.credentials",
)

FORBIDDEN_CALL_NAMES = frozenset(
    {
        "record_planned",
        "record_previewed",
        "record_validated",
        "record_submitted",
        "record_filled",
        "record_closed",
        "record_cancelled",
        "record_reconciled",
        "record_anomaly",
        "record_plan",
        "record_preview",
        "record_submit",
        "record_status",
        "record_cancel",
        "record_position_snapshot",
        "record_sell_validation",
        "record_close",
        "record_reconcile",
        "record_final_reconcile",
        "record_submit_failure",
        "claim_submit",
        "reserve_sell_and_claim",
        "acquire_sell_reservation_lock",
        "resolve_or_create_instrument",
        "submit_order",
        "place_order",
        "cancel_order",
        "transition",
        "confirm_promotion",
        "authorize_order_submit",
        "reject_or_abort",
    }
)

FORBIDDEN_NAME_PATTERNS = (
    "usdt_to_usd",
    "usd_to_usdt",
    "convert_usdt_usd",
    "convert_usd_usdt",
    "peg_usdt_usd",
    "usdt_equals_usd",
)
FORBIDDEN_CALL_PREFIXES = (
    "record_",
    "claim_",
    "reserve_",
    "submit_",
    "place_",
    "cancel_",
    "update_state",
    "transition_",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            names.add(node.name)
    return names


def _all_py_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_no_forbidden_broker_or_validation_imports() -> None:
    for path in _all_py_files():
        imports = _imports(path)
        for forbidden in FORBIDDEN_IMPORT_FRAGMENTS:
            assert forbidden not in imports, (
                f"{path.name} imports forbidden module: {forbidden}"
            )


def test_no_forbidden_broker_write_or_promotion_calls() -> None:
    for path in _all_py_files():
        calls = _call_names(path)
        violations = calls & FORBIDDEN_CALL_NAMES
        violations.update(
            name
            for name in calls
            if any(name.startswith(prefix) for prefix in FORBIDDEN_CALL_PREFIXES)
        )
        assert not violations, f"{path.name} calls forbidden methods: {violations}"


def test_no_environment_or_live_credential_access() -> None:
    for path in _all_py_files():
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "dotenv",
            'open(".env',
            "settings.api_key",
            "live_credential",
        ):
            assert forbidden not in source, f"{path.name} accesses {forbidden}"


def test_no_usdt_usd_conversion_functions() -> None:
    for path in _all_py_files():
        funcs = _function_names(path)
        for func_name in funcs:
            lower = func_name.lower()
            for pattern in FORBIDDEN_NAME_PATTERNS:
                assert pattern not in lower, (
                    f"{path.name} defines conversion function: {func_name}"
                )


def test_no_cross_view_nominal_aggregation() -> None:
    for path in _all_py_files():
        source = path.read_text(encoding="utf-8")
        forbidden_strings = (
            "total_nominal_pnl",
            "cross_view_pnl",
            "aggregate_nominal_pnl",
            "combined_pnl",
            "usdt_plus_usd",
        )
        for s in forbidden_strings:
            assert s not in source, f"{path.name} contains forbidden pattern: {s}"


def test_service_does_not_import_concrete_broker_services() -> None:
    service_path = PACKAGE / "service.py"
    if not service_path.exists():
        return
    forbidden = (
        "BinanceDemoLedgerService",
        "AlpacaPaperLedgerService",
        "AlpacaPaperBrokerService",
        "BinancePublicRestClient",
    )
    source = service_path.read_text(encoding="utf-8")
    for name in forbidden:
        assert name not in source, (
            f"service.py references concrete broker service: {name}"
        )
