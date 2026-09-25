"""Static closure checks for the #728 live sell and dormant-US surfaces."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

_SEND_TARGETS = frozenset(
    {
        "order_korea_stock",
        "order_overseas_stock",
        "sell_overseas_stock",
        "modify_korea_order",
        "modify_overseas_order",
        "place_sell_order",
        "place_market_sell_order",
        "cancel_orders",
        "cancel_and_reorder",
        "place_order",
        "modify_order",
    }
)
_SEND_TREES = ("app", "scripts")


def _send_callers(relative_path: str) -> Counter[str]:
    """Extract real AST callers instead of maintaining a scenario table."""

    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    stack: list[str] = []
    callers: Counter[str] = Counter()

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            if isinstance(node.func, ast.Attribute) and node.func.attr in _SEND_TARGETS:
                callers[f"{relative_path}:{'.'.join(stack)}:{node.func.attr}"] += 1
            if isinstance(node.func, ast.Name) and node.func.id in _SEND_TARGETS:
                callers[f"{relative_path}:{'.'.join(stack)}:{node.func.id}"] += 1
            # KIS order execution passes a broker method reference into the
            # generic _call_kis boundary rather than invoking it syntactically
            # at the call site. Include those concrete references so G6 covers
            # the real live seller rather than only direct Python calls.
            if isinstance(node.func, ast.Name) and node.func.id == "_call_kis":
                for argument in node.args:
                    if (
                        isinstance(argument, ast.Attribute)
                        and argument.attr in _SEND_TARGETS
                    ):
                        callers[
                            f"{relative_path}:{'.'.join(stack)}:{argument.attr}"
                        ] += 1
            self.generic_visit(node)

    Visitor().visit(tree)
    return callers


# Every actual caller of a real broker send/modify primitive is deliberately
# classified. A new AST caller fails this test until it is labelled guarded,
# mock-only, a broker-internal retry/delegate, or explicitly non-sell.
_CALLER_CLASSIFICATION = {
    "app/mcp_server/tooling/order_execution.py:_execute_crypto_order:place_market_sell_order": (
        "g1",
        1,
    ),
    "app/mcp_server/tooling/order_execution.py:_execute_crypto_order:place_sell_order": (
        "g1",
        1,
    ),
    "app/mcp_server/tooling/order_execution.py:_execute_kr_order:order_korea_stock": (
        "g1_or_mock",
        2,
    ),
    "app/mcp_server/tooling/order_execution.py:_execute_us_order:sell_overseas_stock": (
        "g1_or_mock",
        1,
    ),
    "app/mcp_server/tooling/orders_modify_cancel.py:_modify_upbit:cancel_and_reorder": (
        "g4",
        1,
    ),
    "app/mcp_server/tooling/orders_modify_cancel.py:_modify_kis_mock_domestic:modify_korea_order": (
        "mock_only",
        1,
    ),
    "app/mcp_server/tooling/orders_modify_cancel.py:_modify_kis_domestic:modify_korea_order": (
        "g4",
        1,
    ),
    "app/mcp_server/tooling/orders_modify_cancel.py:_modify_kis_overseas:modify_overseas_order": (
        "g4",
        1,
    ),
    "app/mcp_server/tooling/orders_toss_variants.py:_toss_place_order_impl.execute_order:place_order": (
        "g2",
        3,
    ),
    "app/mcp_server/tooling/orders_toss_variants.py:toss_modify_order.execute_modify:modify_order": (
        "g3",
        1,
    ),
    "app/services/kis_trading_service.py:DomesticOrderOps.place_order:order_korea_stock": (
        "g5",
        1,
    ),
    "app/services/kis_trading_service.py:OverseasOrderOps.place_order:order_overseas_stock": (
        "g5",
        1,
    ),
    "app/services/kis_trading_service.py:_place_legacy_guarded_sell_fragment:place_order": (
        "g5",
        1,
    ),
    "app/services/kis_trading_service.py:_process_buy_orders_impl:place_order": (
        "buy_out_of_scope",
        1,
    ),
    "app/services/brokers/upbit/orders.py:cancel_and_reorder:cancel_orders": (
        "g4",
        1,
    ),
    "app/services/brokers/upbit/orders.py:cancel_and_reorder:place_sell_order": (
        "g4",
        1,
    ),
    "app/mcp_server/tooling/orders_modify_cancel.py:_cancel_upbit:cancel_orders": (
        "cancel_only_safe",
        1,
    ),
    "app/services/brokers/kis/client.py:KISClient.order_korea_stock:order_korea_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/client.py:KISClient.modify_korea_order:modify_korea_order": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/client.py:KISClient.order_overseas_stock:order_overseas_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/client.py:KISClient.sell_overseas_stock:sell_overseas_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/client.py:KISClient.modify_overseas_order:modify_overseas_order": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/domestic_orders.py:DomesticOrderClient.order_korea_stock:order_korea_stock": (
        "same_order_retry",
        2,
    ),
    "app/services/brokers/kis/domestic_orders.py:DomesticOrderClient.sell_korea_stock:order_korea_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/domestic_orders.py:DomesticOrderClient.modify_korea_order:modify_korea_order": (
        "same_order_retry",
        1,
    ),
    "app/services/brokers/kis/overseas_orders.py:OverseasOrderClient.order_overseas_stock:order_overseas_stock": (
        "same_order_retry",
        2,
    ),
    "app/services/brokers/kis/overseas_orders.py:OverseasOrderClient.buy_overseas_stock:order_overseas_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/overseas_orders.py:OverseasOrderClient.sell_overseas_stock:order_overseas_stock": (
        "broker_delegate",
        1,
    ),
    "app/services/brokers/kis/overseas_orders.py:OverseasOrderClient.modify_overseas_order:modify_overseas_order": (
        "same_order_retry",
        1,
    ),
    "app/mcp_server/tooling/orders_kiwoom_us_variants.py:register.place:place_sell_order": (
        "mock_only",
        1,
    ),
    "app/mcp_server/tooling/orders_kiwoom_us_variants.py:register.modify:modify_order": (
        "mock_only",
        1,
    ),
    "app/mcp_server/tooling/orders_kiwoom_variants.py:_kiwoom_mock_place_order_impl:place_sell_order": (
        "mock_only",
        1,
    ),
    "app/mcp_server/tooling/orders_kiwoom_variants.py:_kiwoom_mock_modify_confirmed_impl:modify_order": (
        "mock_only",
        1,
    ),
    "app/routers/screener.py:screener_order:place_order": ("g1", 1),
    "app/services/trade_journal/mirror_counterfactual.py:execute_mirror_order_plans:place_order": (
        "g1",
        1,
    ),
    "scripts/b0x/kr/kiwoom.py:ReadOnlyKiwoomMockAccount.place_limit_sell:place_sell_order": (
        "mock_only",
        1,
    ),
    "scripts/kis_mock_overseas_holdings_delta_smoke.py:_cleanup_and_verify:sell_overseas_stock": (
        "mock_only",
        1,
    ),
    "scripts/kiwoom_mock_us_smoke.py:run_probe:place_sell_order": (
        "mock_only",
        1,
    ),
}


def _function_source(relative_path: str, name: str) -> str:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {relative_path}:{name}")


def test_g6_ast_sender_callers_are_completely_classified() -> None:
    actual: Counter[str] = Counter()
    # Derive the module inventory from source. A new send caller in a new
    # module must fail the exact classification below until reviewed.
    for tree in _SEND_TREES:
        for source_path in sorted((ROOT / tree).rglob("*.py")):
            actual.update(_send_callers(source_path.relative_to(ROOT).as_posix()))

    expected = Counter(
        {caller: count for caller, (_, count) in _CALLER_CLASSIFICATION.items()}
    )
    assert actual == expected
    assert {label for label, _ in _CALLER_CLASSIFICATION.values()} >= {
        "g1",
        "g2",
        "g3",
        "g4",
        "g5",
        "mock_only",
        "same_order_retry",
    }


def test_guards_remain_before_their_send_boundaries() -> None:
    g1 = _function_source(
        "app/mcp_server/tooling/order_execution.py", "_execute_and_record"
    )
    assert g1.index("prepare_live_sell_lease") < g1.index("OrderSendIntentService")
    assert g1.index("prepare_live_sell_lease") < g1.index("_send_to_broker")
    assert g1.index("resolve_attribution") < g1.index("prepare_live_sell_lease")

    g2 = _function_source(
        "app/mcp_server/tooling/orders_toss_variants.py", "_toss_place_order_impl"
    )
    assert (
        g2.index("_fresh_sellable_preflight")
        < g2.index("_prepare_toss_sell_protection")
        < g2.index("pre_send_hook")
    )

    g3 = _function_source(
        "app/mcp_server/tooling/orders_toss_variants.py", "toss_modify_order"
    )
    assert (
        g3.index("_fresh_sellable_preflight")
        < g3.index("_prepare_toss_sell_protection")
        < g3.index("client.modify_order")
    )

    toss_guard = _function_source(
        "app/mcp_server/tooling/orders_toss_variants.py",
        "_prepare_toss_sell_protection",
    )
    assert (
        toss_guard.index("prepare_live_sell_lease")
        < toss_guard.index("_fresh_sellable_preflight")
        < toss_guard.index("lease.evaluate")
    )

    g4_kr = _function_source(
        "app/mcp_server/tooling/orders_modify_cancel.py", "_modify_kis_domestic"
    )
    assert g4_kr.index("_prepare_kis_live_sell_modify_protection") < g4_kr.index(
        "kis.modify_korea_order"
    )

    g4_us = _function_source(
        "app/mcp_server/tooling/orders_modify_cancel.py", "_modify_kis_overseas"
    )
    assert g4_us.index("_prepare_kis_live_sell_modify_protection") < g4_us.index(
        "kis.modify_overseas_order"
    )

    g4_upbit = _function_source(
        "app/services/brokers/upbit/orders.py", "cancel_and_reorder"
    )
    assert g4_upbit.index("prepare_live_sell_lease") < g4_upbit.index("cancel_orders")
    assert g4_upbit.index("cancel_orders") < g4_upbit.index("place_sell_order")
    assert g4_upbit.count("_fresh_upbit_sell_position") >= 2

    g5 = _function_source(
        "app/services/kis_trading_service.py", "_place_legacy_guarded_sell_fragment"
    )
    assert g5.index("prepare_live_sell_lease") < g5.index("ops.place_order")


def _imports_target_module(relative_path: Path, target: str) -> bool:
    tree = ast.parse(relative_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == target:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == target for alias in node.names):
                return True
    return False


def test_c9_c10_dormant_us_surfaces_have_no_runtime_wiring() -> None:
    modules = {
        "app.services.action_report.us.action_classifier",
        "app.services.action_report.us.account_snapshot",
    }
    for module in modules:
        importers = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "app").rglob("*.py")
            if _imports_target_module(path, module)
        }
        assert importers == {"app/services/action_report/us/__init__.py"}
