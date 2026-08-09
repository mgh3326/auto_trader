"""AST guard — the safety line for wiring kis_mock submission (contract v1.3 ③).

Precedent: the Kiwoom live read-only guard (``CLAUDE.md`` "계좌번호 부재 (3중)"
② — a static AST check that fails the build/test suite rather than trusting
runtime discipline alone). This module applies the same shape to the newly
wired ``KisMockBroker`` submission surface in ``scripts/b0x/kr/**``.

Honesty about what this buys, matching how the Kiwoom precedent itself is
worded rather than overclaiming: this is **"우발 방지 + 정적 검출"** (accident
prevention + static detection), **not "구조적 불가능"** (structural
impossibility). A static AST scan over this package's own source files
cannot stop a determined rewrite of the guard itself, a change made outside
this package, or a genuinely dynamic construct this scanner does not model.
What it does reliably catch: an accidental ``is_mock=False``, an accidental
``import`` of a live-order module, or an accidental (or lazy) string-built
dynamic-attribute/import bypass introduced into *this* package without
someone consciously rewriting *this guard*.

Three prohibitions, scanned across ``scripts/b0x/kr/**`` +
``scripts/run_b0x_kr_cycle.py`` (the same file set
``test_no_live_kis_order_imports.py`` already covers for its narrower
denylist — this file is the broader, allowlist-based companion, not a
replacement; both keep passing):

1. **is_mock=False** — literal, variable/expression, or bypass-via-omitted-
   default — on any call carrying an ``is_mock`` keyword, or any call to a
   *known* is_mock-bearing KIS callable that omits the keyword entirely
   (several of those default to ``is_mock=False``, i.e. live).
2. **Live-order-module import** — enforced as an *allowlist* of the small
   reviewed read/mock surface under ``app.services.brokers.kis.*`` /
   ``app.mcp_server.tooling.*`` / ``app.services.kis_trading_service`` /
   ``app.services.kis_mock_runner.*``; anything else under those prefixes is
   presumed live-order-capable and rejected. ``FORBIDDEN_LIVE_MODULES`` below
   is the exhaustive enumeration of what is concretely known to be excluded
   (cited for review, and regression-tested at the bottom of this file so the
   list cannot silently drift from what the allowlist actually rejects).
3. **String-based bypass** — ``importlib.import_module``, bare
   ``__import__``, ``exec``/``eval`` anywhere, or ``getattr()`` called with a
   non-literal (variable, f-string, concatenation, ...) attribute-name
   argument. The one legitimate ``getattr`` in this package
   (``scripts/b0x/kr/cycle.py``'s ``client.close`` lookup) always uses a
   literal string and is unaffected.

The bottom half of this file is guard self-tests: each detector is fed
synthetic source (not the real package) with a known violation and asserted
to catch it, so "the guard exists" is never mistaken for "the guard fires" —
a regression here is caught the moment someone weakens a detector, without
needing to hand-inject and revert a mutant into production code every time
the suite runs. The hand-injected-mutant pass (inject into the real files,
confirm the parametrized scan above fails, revert) was performed manually
once while writing this guard — see the job report, not this file, for that
one-time empirical record.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(__file__).resolve().parents[4] / "scripts" / "b0x" / "kr"
RUNNER = Path(__file__).resolve().parents[4] / "scripts" / "run_b0x_kr_cycle.py"

#: The only modules under the guarded prefixes this package may import from.
#: An allowlist rather than a denylist: a future addition to the live-order
#: surface is rejected by default instead of silently falling through a list
#: someone forgot to update.
ALLOWED_KIS_SURFACE = {
    "app.core.config",
    "app.core.symbol",
    "app.mcp_server.tick_size",
    "app.services.brokers.kis.account",
    "app.services.brokers.kis.base",
    "app.services.brokers.kis.protocols",
    "app.services.brokers.kis.mock_scalping_exec.adapters",
    "app.services.brokers.kis.mock_scalping_ws.state",
    "app.services.kis_mock_runner.session",
    "app.services.kis_mock_runner.singleton",
}

#: Prefixes treated as "the KIS/order surface" for the allowlist check.
GUARDED_MODULE_PREFIXES = (
    "app.services.brokers.kis.",
    "app.mcp_server.tooling.",
    "app.services.kis_trading_service",
    "app.services.kis_mock_runner.",
)

#: Exhaustive enumeration of concretely known live/order-capable modules —
#: cited for review and regression-tested below (``test_documented_forbidden_
#: modules_are_all_actually_rejected``) so this list cannot drift from what
#: the allowlist gate above actually rejects. The allowlist, not this tuple,
#: is what the guard enforces at runtime.
FORBIDDEN_LIVE_MODULES = (
    "app.mcp_server.tooling.order_execution",
    "app.mcp_server.tooling.orders_kis_variants",
    "app.mcp_server.tooling.orders_modify_cancel",
    "app.mcp_server.tooling.orders_registration",
    "app.mcp_server.tooling.kis_live_ledger",
    "app.mcp_server.tooling.live_order_ledger",
    "app.mcp_server.tooling.live_order_evidence",
    "app.mcp_server.tooling.order_approval",
    "app.mcp_server.tooling.order_validation",
    "app.mcp_server.tooling.order_proposal_tools",
    "app.mcp_server.tooling.pending_orders_snapshot",
    "app.services.brokers.kis.client",
    "app.services.brokers.kis.domestic_orders",
    "app.services.brokers.kis.overseas_orders",
    "app.services.brokers.kis.live_order_expiry",
    "app.services.kis_trading_service",
)

#: Known KIS/account callables that accept an ``is_mock`` kwarg — a call to
#: any of these that omits the kwarg silently defaults to ``is_mock=False``
#: (live). Named by attribute/function name (static, no type inference).
#:
#: ``fetch_my_stocks`` is handled by the scoped receiver check below rather
#: than added here: this package's own ``ReadOnlyKISMockDomesticClient``
#: wrapper shares that name with the underlying ``AccountClient`` method it
#: calls, but the wrapper itself takes no ``is_mock`` parameter. A bare name
#: match would flag every caller of the mock-only wrapper as a false positive.
IS_MOCK_BEARING_CALLABLES = {
    "inquire_domestic_cash_balance",
    "fetch_domestic_balance_snapshot",
    "_create_kis_client",
    "inquire_korea_orders",
    "cancel_korea_order",
    "order_korea_stock",
    "sell_korea_stock",
    "modify_korea_order",
    "inquire_daily_order_domestic",
}


def _is_account_client_fetch_my_stocks(func: ast.expr) -> bool:
    """Identify the composed ``AccountClient`` call without matching our wrapper.

    The only AccountClient composition in ``scripts/b0x/kr`` is
    ``self._account = AccountClient(...)``. The wrapper call sites use
    ``client.fetch_my_stocks()``; their receiver is not the exact
    ``self._account`` shape below, so adding this targeted guard does not
    resurrect the old false positive.
    """

    return (
        isinstance(func, ast.Attribute)
        and func.attr == "fetch_my_stocks"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "_account"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "self"
    )


def _python_files() -> list[Path]:
    return sorted([*PACKAGE_ROOT.rglob("*.py"), RUNNER])


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def find_is_mock_violations(tree: ast.AST) -> list[str]:
    """Prohibition 1: ``is_mock=False`` literal/variable, or an omitted
    ``is_mock`` kwarg on a known is_mock-bearing callable."""

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node.func)
        is_mock_kw = next((kw for kw in node.keywords if kw.arg == "is_mock"), None)
        if is_mock_kw is not None:
            is_literal_true = (
                isinstance(is_mock_kw.value, ast.Constant)
                and is_mock_kw.value.value is True
            )
            if not is_literal_true:
                violations.append(
                    f"line {node.lineno}: is_mock kwarg is not the literal True "
                    f"({ast.dump(is_mock_kw.value)})"
                )
        elif callee in IS_MOCK_BEARING_CALLABLES or _is_account_client_fetch_my_stocks(
            node.func
        ):
            violations.append(
                f"line {node.lineno}: call to {callee or 'AccountClient.fetch_my_stocks'}"
                "(...) omits is_mock kwarg "
                "— its default is False (live)"
            )
    return violations


def find_forbidden_imports(tree: ast.AST) -> list[str]:
    """Prohibition 2: allowlist gate over the guarded KIS/order prefixes."""

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for module in names:
            if module in ALLOWED_KIS_SURFACE:
                continue
            guarded = any(
                module == prefix.rstrip(".") or module.startswith(prefix)
                for prefix in GUARDED_MODULE_PREFIXES
            )
            if guarded:
                violations.append(f"line {node.lineno}: forbidden import {module!r}")
    return violations


def find_string_bypass_violations(tree: ast.AST) -> list[str]:
    """Prohibition 3: importlib/dunder-import/exec/eval, or non-literal
    ``getattr`` attribute names."""

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"__import__", "exec", "eval"}:
            violations.append(f"line {node.lineno}: {func.id}() call")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        ):
            violations.append(f"line {node.lineno}: importlib.import_module() call")
        elif isinstance(func, ast.Name) and func.id == "getattr":
            has_dynamic_name = len(node.args) >= 2 and not (
                isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            )
            if has_dynamic_name:
                violations.append(
                    f"line {node.lineno}: getattr() with a non-literal attribute name"
                )
    return violations


def test_the_kr_package_actually_has_files() -> None:
    files = _python_files()
    assert len(files) >= 2, f"expected the kr package, found {files}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_is_mock_false_bypass(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = find_is_mock_violations(tree)
    assert violations == [], f"{path.name}: {violations}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_live_kis_module_import(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = find_forbidden_imports(tree)
    assert violations == [], f"{path.name}: {violations}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_no_string_based_import_or_attribute_bypass(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = find_string_bypass_violations(tree)
    assert violations == [], f"{path.name}: {violations}"


# ---------------------------------------------------------------------------
# Guard self-tests — prove each detector actually fires, on synthetic source.
# ---------------------------------------------------------------------------


def test_detector_catches_is_mock_false_literal() -> None:
    tree = ast.parse("f(is_mock=False)")
    assert find_is_mock_violations(tree) != []


def test_detector_catches_is_mock_variable() -> None:
    tree = ast.parse("flag = True\nf(is_mock=flag)")
    assert find_is_mock_violations(tree) != []


def test_detector_catches_is_mock_expression() -> None:
    tree = ast.parse("f(is_mock=(1 == 1))")
    assert find_is_mock_violations(tree) != []


def test_detector_catches_is_mock_omitted_on_known_callable() -> None:
    tree = ast.parse("client.inquire_domestic_cash_balance()")
    assert find_is_mock_violations(tree) != []


def test_detector_catches_account_client_fetch_my_stocks_omission() -> None:
    tree = ast.parse("self._account.fetch_my_stocks(is_overseas=False)")
    assert find_is_mock_violations(tree) != []


def test_detector_allows_mock_wrapper_fetch_my_stocks_without_is_mock() -> None:
    tree = ast.parse("client.fetch_my_stocks()")
    assert find_is_mock_violations(tree) == []


def test_detector_allows_is_mock_true_literal() -> None:
    tree = ast.parse("client.inquire_domestic_cash_balance(is_mock=True)")
    assert find_is_mock_violations(tree) == []


def test_detector_allows_unrelated_call_without_is_mock() -> None:
    tree = ast.parse("print('hello')")
    assert find_is_mock_violations(tree) == []


def test_detector_catches_forbidden_live_import() -> None:
    tree = ast.parse(
        "from app.mcp_server.tooling.order_execution import _place_order_impl"
    )
    assert find_forbidden_imports(tree) != []


def test_detector_catches_forbidden_live_import_submodule() -> None:
    tree = ast.parse("import app.services.brokers.kis.domestic_orders")
    assert find_forbidden_imports(tree) != []


def test_detector_allows_sanctioned_adapter_import() -> None:
    tree = ast.parse(
        "from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker"
    )
    assert find_forbidden_imports(tree) == []


def test_detector_allows_unrelated_third_party_import() -> None:
    tree = ast.parse("import json\nfrom decimal import Decimal")
    assert find_forbidden_imports(tree) == []


@pytest.mark.parametrize("module", FORBIDDEN_LIVE_MODULES)
def test_documented_forbidden_modules_are_all_actually_rejected(module: str) -> None:
    """Ties FORBIDDEN_LIVE_MODULES to the allowlist gate's real behavior —
    the enumeration cannot silently drift from what is actually rejected."""

    tree = ast.parse(f"import {module}")
    assert find_forbidden_imports(tree) != [], (
        f"{module} is listed as forbidden but the allowlist gate did not reject it"
    )


def test_detector_catches_importlib_import_module() -> None:
    tree = ast.parse("import importlib\nimportlib.import_module('x')")
    assert find_string_bypass_violations(tree) != []


def test_detector_catches_dunder_import() -> None:
    tree = ast.parse("__import__('os')")
    assert find_string_bypass_violations(tree) != []


def test_detector_catches_exec_and_eval() -> None:
    assert find_string_bypass_violations(ast.parse("exec('x = 1')")) != []
    assert find_string_bypass_violations(ast.parse("eval('1 + 1')")) != []


def test_detector_catches_getattr_with_dynamic_name() -> None:
    tree = ast.parse("name = 'x'\ngetattr(obj, name)")
    assert find_string_bypass_violations(tree) != []


def test_detector_catches_getattr_with_fstring_name() -> None:
    tree = ast.parse("i = 1\ngetattr(obj, f'attr_{i}')")
    assert find_string_bypass_violations(tree) != []


def test_detector_catches_getattr_with_concatenated_name() -> None:
    tree = ast.parse("getattr(obj, 'attr_' + 'name')")
    assert find_string_bypass_violations(tree) != []


def test_detector_allows_getattr_with_literal_name() -> None:
    tree = ast.parse("getattr(client, 'close', None)")
    assert find_string_bypass_violations(tree) == []
