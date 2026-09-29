# ruff: noqa: F811
# Imported pytest fixtures intentionally share names with test parameters.
"""Assertion-RED mutants for every real call site of the #849 guards.

Call sites are counted from ``operations.py`` on disk. Each mutant removes one
call site from a compiled copy of the module, runs the same scenario the real
module passes, and must make the invariant assertion fail. A new call site
without a declared mutant fails ``test_every_guard_call_site_has_a_mutant``.

Invariant sentences:
- ACCOUNT: a configured account the broker does not list as acct_type=03
  never reaches a ledger write and never reaches an order request.
- LIMIT: a non-limit order type is refused and never reaches an order request.
"""

from __future__ import annotations

import ast
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.nhplug_mock import operations
from tests.services.nhplug_mock.test_dispatch_state_machine import (  # noqa: F401
    nhplug_engine,
)
from tests.services.nhplug_mock.test_nh_mock_operations import (  # noqa: F401
    ACCT,
    install,
    key,
    ledger_rows,
    ops_engine,
    order_row,
    stage2_env,
)

pytestmark = pytest.mark.integration

OPERATIONS_SOURCE = Path(operations.__file__)
ACCOUNT_GUARD = "_require_verified_mock_account"
LIMIT_GUARD = "_require_limit_order"
DECLARED_MUTANTS: dict[str, tuple[str, ...]] = {
    ACCOUNT_GUARD: ("_verified_session",),
    LIMIT_GUARD: ("modify_order", "place_order", "preview_order"),
}


def _enclosing_calls(tree: ast.AST, guard: str) -> Iterator[str]:
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == guard
            ):
                yield function.name


def call_sites(guard: str) -> list[str]:
    tree = ast.parse(OPERATIONS_SOURCE.read_text("utf-8"))
    return sorted(_enclosing_calls(tree, guard))


def test_every_guard_call_site_has_a_mutant() -> None:
    for guard, declared in DECLARED_MUTANTS.items():
        assert call_sites(guard) == sorted(declared), guard


class _RemoveCall(ast.NodeTransformer):
    """Remove one guard call inside one function; keep everything else."""

    def __init__(self, guard: str, site: str) -> None:
        self.guard = guard
        self.site = site
        self.inside = False
        self.removed = 0

    def _is_guard(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == self.guard
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._visit_function(node)

    def _visit_function(self, node: Any) -> ast.AST:
        previous = self.inside
        self.inside = node.name == self.site
        self.generic_visit(node)
        self.inside = previous
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        if self.inside and self._is_guard(value):
            self.removed += 1
            return ast.Pass()
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if self.inside and self._is_guard(node):
            # Value-returning guard: pass the checked argument through unchecked.
            self.removed += 1
            return node.args[-1]
        return self.generic_visit(node)


@pytest.fixture
def mutant(request: pytest.FixtureRequest) -> Iterator[types.ModuleType]:
    guard, site = request.param
    tree = ast.parse(OPERATIONS_SOURCE.read_text("utf-8"))
    remover = _RemoveCall(guard, site)
    tree = ast.fix_missing_locations(remover.visit(tree))
    assert remover.removed == 1, (guard, site, remover.removed)
    name = f"nh_mock_operations_mutant_{guard}_{site}"
    module = types.ModuleType(name)
    module.__file__ = str(OPERATIONS_SOURCE)
    sys.modules[name] = module
    try:
        exec(compile(tree, str(OPERATIONS_SOURCE), "exec"), module.__dict__)  # noqa: S102
        yield module
    finally:
        sys.modules.pop(name, None)


async def _live_account_place(
    module: Any, monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, suffix: str
) -> tuple[Any, dict[str, Any]]:
    fake = install(
        monkeypatch,
        engine,
        suffix,
        module=module,
        acct_rows=[{"acct_no": "MOCK-" + suffix, "acct_type": "01"}],
    )
    result = await module.place_order(
        symbol="005930",
        side="buy",
        quantity=1,
        price=50000,
        idempotency_key=key(suffix),
        dry_run=False,
        confirm=True,
    )
    return fake, result


async def _assert_account_invariant(
    fake: Any, engine: AsyncEngine, account_no: str
) -> None:
    assert await ledger_rows(engine, account_no) == []
    assert fake.orders == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutant",
    [(ACCOUNT_GUARD, site) for site in DECLARED_MUTANTS[ACCOUNT_GUARD]],
    indirect=True,
)
async def test_account_guard_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mutant: types.ModuleType,
) -> None:
    fake, result = await _live_account_place(
        operations, monkeypatch, ops_engine, "acctctl"
    )
    assert result["error"] == "mock_account_rejected"
    await _assert_account_invariant(fake, ops_engine, "MOCK-acctctl")

    fake, result = await _live_account_place(mutant, monkeypatch, ops_engine, "acctmut")
    with pytest.raises(AssertionError):
        await _assert_account_invariant(fake, ops_engine, "MOCK-acctmut")


async def _market_attempt(
    module: Any,
    site: str,
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    suffix: str,
) -> tuple[Any, int, dict[str, Any]]:
    fake = install(monkeypatch, engine, suffix, module=module)
    if site == "preview_order":
        result = await module.preview_order(
            symbol="005930", side="buy", quantity=1, price=50000, order_type="market"
        )
        return fake, 0, result
    if site == "place_order":
        result = await module.place_order(
            symbol="005930",
            side="buy",
            quantity=1,
            price=50000,
            order_type="market",
            idempotency_key=key(suffix),
            dry_run=False,
            confirm=True,
        )
        return fake, 0, result
    # modify_order: bind an open order through the real path first.
    fake.order_numbers = ["1000700", "1000701"]
    await module.place_order(
        symbol="005930",
        side="buy",
        quantity=1,
        price=50000,
        idempotency_key=key(suffix),
        dry_run=False,
        confirm=True,
    )
    fake.rows[1000700] = order_row(1000700)
    await module.reconcile_orders(dry_run=False, confirm=True)
    before = len(fake.orders)
    result = await module.modify_order(
        order_id="1000700",
        new_price=49000,
        new_quantity=1,
        order_type="market",
        idempotency_key=key(suffix, 2),
        dry_run=False,
        confirm=True,
    )
    return fake, before, result


def _assert_limit_invariant(fake: Any, before: int, result: dict[str, Any]) -> None:
    assert result.get("error") == "limit_order_only"
    assert len(fake.orders) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutant",
    [(LIMIT_GUARD, site) for site in DECLARED_MUTANTS[LIMIT_GUARD]],
    indirect=True,
    ids=list(DECLARED_MUTANTS[LIMIT_GUARD]),
)
async def test_limit_only_mutant_is_assertion_red(
    ops_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    mutant: types.ModuleType,
    request: pytest.FixtureRequest,
) -> None:
    site = request.node.callspec.id
    tag = site.split("_")[0][:4]
    fake, before, result = await _market_attempt(
        operations, site, monkeypatch, ops_engine, "limctl" + tag
    )
    _assert_limit_invariant(fake, before, result)

    fake, before, result = await _market_attempt(
        mutant, site, monkeypatch, ops_engine, "limmut" + tag
    )
    with pytest.raises(AssertionError):
        _assert_limit_invariant(fake, before, result)
