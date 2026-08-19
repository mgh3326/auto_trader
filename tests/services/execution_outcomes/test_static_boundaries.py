from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "app"
PACKAGE_ROOT = APP_ROOT / "services" / "execution_outcomes"
PACKAGE_IMPORT = "app.services.execution_outcomes"

# Compact AST snapshots preserve every parameter, order, and default without
# importing any runtime tool module.  In particular, OD-3 keeps today's generic
# and KIS cancel/modify signatures unchanged while double-gated surfaces retain
# dry_run=True / confirm=False.
EXPECTED_SIGNATURES = {
    "app/mcp_server/tooling/orders_registration.py::place_order": "symbol,side,order_type='limit',quantity=None,price=None,amount=None,dry_run=True,reason='',exit_reason=None,thesis=None,strategy=None,target_price=None,stop_loss=None,min_hold_days=None,notes=None,indicators_snapshot=None,defensive_trim=False,approval_issue_id=None,exit_intent=None,retrospective_id=None,account_mode=None,account_type=None,paper_account=None,report_item_uuid=None,approval_hash=None,rung=None",
    "app/mcp_server/tooling/orders_registration.py::cancel_order": "order_id,symbol=None,market=None,account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_registration.py::modify_order": "order_id,symbol,market=None,new_price=None,new_quantity=None,dry_run=True,reason='',account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_live_place_order": "symbol,side,order_type='limit',quantity=None,price=None,amount=None,dry_run=True,reason='',exit_reason=None,thesis=None,strategy=None,target_price=None,stop_loss=None,min_hold_days=None,notes=None,indicators_snapshot=None,defensive_trim=False,approval_issue_id=None,exit_intent=None,retrospective_id=None,venue=None,order_validity=None,reserved_time=None,account_mode=None,account_type=None,report_item_uuid=None,approval_hash=None,rung=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_live_cancel_order": "order_id,symbol=None,market=None,account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_live_modify_order": "order_id,symbol,market=None,new_price=None,new_quantity=None,dry_run=True,reason='',account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_mock_place_order": "symbol,side,order_type='limit',quantity=None,price=None,amount=None,dry_run=True,reason='',exit_reason=None,thesis=None,strategy=None,target_price=None,stop_loss=None,min_hold_days=None,notes=None,indicators_snapshot=None,defensive_trim=False,approval_issue_id=None,account_mode=None,account_type=None,report_item_uuid=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_mock_cancel_order": "order_id,symbol=None,market=None,account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_kis_variants.py::kis_mock_modify_order": "order_id,symbol,market=None,new_price=None,new_quantity=None,dry_run=True,reason='',account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_kiwoom_variants.py::kiwoom_mock_place_order": "symbol,side,quantity,price,market='kr',exchange='KRX',dry_run=True,confirm=False",
    "app/mcp_server/tooling/orders_kiwoom_variants.py::kiwoom_mock_cancel_order": "order_id,symbol=None,cancel_quantity=None,dry_run=True,confirm=False",
    "app/mcp_server/tooling/orders_kiwoom_variants.py::kiwoom_mock_modify_order": "order_id,symbol,new_price=None,new_quantity=None,dry_run=True,confirm=False",
    "app/mcp_server/tooling/orders_toss_variants.py::toss_place_order": "symbol,side,order_type='limit',quantity=None,price=None,order_amount=None,market=None,time_in_force='DAY',dry_run=True,confirm=False,confirm_high_value_order=False,reason=None,exit_intent=None,exit_reason=None,retrospective_id=None,approval_issue_id=None,thesis=None,strategy=None,target_price=None,stop_loss=None,min_hold_days=None,notes=None,indicators_snapshot=None,report_item_uuid=None,account_mode=None,account_type=None,approval_hash=None,rung=None",
    "app/mcp_server/tooling/orders_toss_variants.py::toss_modify_order": "order_id,new_price=None,new_quantity=None,market=None,dry_run=True,confirm=False,confirm_high_value_order=False,account_mode=None,account_type=None",
    "app/mcp_server/tooling/orders_toss_variants.py::toss_cancel_order": "order_id,dry_run=True,confirm=False,account_mode=None,account_type=None",
    "app/mcp_server/tooling/alpaca_paper_orders.py::alpaca_paper_submit_order": "symbol,side,type,quote_snapshot_id=None,qty=None,notional=None,time_in_force=None,limit_price=None,asset_class='us_equity',confirm=False,account_mode=ALPACA_PAPER_ACCOUNT_MODE",
    "app/mcp_server/tooling/alpaca_paper_orders.py::alpaca_paper_cancel_order": "order_id,confirm=False,account_mode=ALPACA_PAPER_ACCOUNT_MODE",
    "app/services/brokers/binance/spot_demo/execution_client.py::submit_order": "self,*symbol,*side,*order_type,*qty,*client_order_id=None,*price=None,*time_in_force=None,*confirm=False",
    "app/services/brokers/binance/spot_demo/execution_client.py::cancel_order": "self,*symbol,*client_order_id,*confirm=False",
    # ROB-1288 — deliberately widened, once. D2 contract v2 §4.3 requires a
    # Futures close to state its `positionSide` explicitly and forbids
    # recovering it from the quantity sign, so the submit surface has to carry
    # it; the approving issue is ROB-1288. The widening is `position_side`
    # alone, it is keyword-only and defaults to None, and every other frozen
    # signature below (including Spot's `submit_order` and this file's
    # `cancel_order`) is byte-identical to what it was. Anything beyond that
    # single parameter is a separate approval, which is what this exact
    # comparison exists to force.
    "app/services/brokers/binance/futures_demo/execution_client.py::submit_order": "self,*symbol,*side,*order_type,*qty,*client_order_id=None,*price=None,*time_in_force=None,*reduce_only=False,*position_side=None,*confirm=False",
    "app/services/brokers/binance/futures_demo/execution_client.py::cancel_order": "self,*symbol,*client_order_id",
}

EXPECTED_MUTATION_TOOL_NAMES = {
    "app/mcp_server/tooling/orders_registration.py": {
        "place_order",
        "cancel_order",
        "modify_order",
    },
    "app/mcp_server/tooling/orders_kis_variants.py": {
        "kis_live_place_order",
        "kis_live_cancel_order",
        "kis_live_modify_order",
        "kis_mock_place_order",
        "kis_mock_cancel_order",
        "kis_mock_modify_order",
    },
    "app/mcp_server/tooling/orders_kiwoom_variants.py": {
        "kiwoom_mock_place_order",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_modify_order",
    },
    "app/mcp_server/tooling/orders_toss_variants.py": {
        "toss_place_order",
        "toss_modify_order",
        "toss_cancel_order",
    },
    "app/mcp_server/tooling/alpaca_paper_orders.py": {
        "alpaca_paper_submit_order",
        "alpaca_paper_cancel_order",
    },
}


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _signature_text(path: Path, function_name: str) -> str:
    matches = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function_name
    ]
    assert len(matches) == 1, (path, function_name, len(matches))
    node = matches[0]
    positional = [*node.args.posonlyargs, *node.args.args]
    positional_defaults = [None] * (len(positional) - len(node.args.defaults)) + list(
        node.args.defaults
    )
    chunks = [
        arg.arg + ("" if default is None else f"={ast.unparse(default)}")
        for arg, default in zip(positional, positional_defaults, strict=True)
    ]
    if node.args.vararg:
        chunks.append(f"*{node.args.vararg.arg}")
    chunks.extend(
        f"*{arg.arg}" + ("" if default is None else f"={ast.unparse(default)}")
        for arg, default in zip(
            node.args.kwonlyargs, node.args.kw_defaults, strict=True
        )
    )
    if node.args.kwarg:
        chunks.append(f"**{node.args.kwarg.arg}")
    return ",".join(chunks)


def _registered_tool_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "tool":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                names.add(keyword.value.value)
    return names


def test_leaf_package_has_no_mcp_broker_db_or_network_imports() -> None:
    allowed_prefixes = {
        "__future__",
        "dataclasses",
        "enum",
        "typing",
        PACKAGE_IMPORT,
    }
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{module}"
        for path in PACKAGE_ROOT.glob("*.py")
        for module in _imports(path)
        if not any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in allowed_prefixes
        )
    ]

    assert offenders == []


def test_rob1189_is_consumed_without_reimplementing_writer_cardinality() -> None:
    source = (PACKAGE_ROOT / "contract.py").read_text()

    assert "ROB-1189" in source
    assert "designated_writer_ref" in source
    assert "research_contracts" not in _imports(PACKAGE_ROOT / "contract.py")
    assert "validate_manifest" not in source


def test_no_existing_app_caller_or_registry_is_cut_over_to_leaf_contract() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in APP_ROOT.rglob("*.py")
        if not path.is_relative_to(PACKAGE_ROOT)
        and any(
            module == PACKAGE_IMPORT or module.startswith(f"{PACKAGE_IMPORT}.")
            for module in _imports(path)
        )
    ]

    assert offenders == []


def test_public_mutation_signatures_and_gate_defaults_are_unchanged() -> None:
    actual = {}
    for locator in EXPECTED_SIGNATURES:
        relative_path, function_name = locator.split("::", 1)
        actual[locator] = _signature_text(REPO_ROOT / relative_path, function_name)

    assert actual == EXPECTED_SIGNATURES


def test_existing_mutation_tool_names_remain_registered() -> None:
    for relative_path, expected_names in EXPECTED_MUTATION_TOOL_NAMES.items():
        assert expected_names <= _registered_tool_names(REPO_ROOT / relative_path)
