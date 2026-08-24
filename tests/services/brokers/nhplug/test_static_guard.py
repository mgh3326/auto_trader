"""Static stage-one guard for the NHPLUG read-only broker boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.services.brokers.nhplug.contracts import DryRunConfirmContract

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_DIR = REPO_ROOT / "app" / "services" / "brokers" / "nhplug"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "nhplug_mock_smoke.py"
RUNTIME_SOURCES = tuple(sorted(RUNTIME_DIR.glob("*.py"))) + (SMOKE_SCRIPT,)

_PRODUCTION_HOST_RE = re.compile(r"(?<![\w.\-])api\.nhplug\.com(?![\w.\-])")
_VENDOR_IMPORT = "nhplug"
_FORBIDDEN_OVERRIDE_ENV = frozenset({"NHPLUG_BASE_URL", "NHPLUG_AUTH_URL"})
_FORBIDDEN_ORDER_TEXT = (
    "/krstock/order/",
    "cashBuy",
    "cashSell",
    "SCSOS61803A",
    "SCSOS61801A",
    "SCSOS61808A",
    "SCSOS61809A",
)


def _constant_string(node: ast.AST) -> str | None:
    """Evaluate only source-level string construction that cannot run code."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            pieces.append(value.value)
        return "".join(pieces)
    return None


def _literal_strings(tree: ast.AST) -> list[str]:
    """Return direct and statically concatenated strings for bypass detection."""

    return [
        value
        for node in ast.walk(tree)
        if (value := _constant_string(node)) is not None
    ]


def _vendor_sdk_imports(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(
                alias.name
                for alias in node.names
                if alias.name == _VENDOR_IMPORT
                or alias.name.startswith(f"{_VENDOR_IMPORT}.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _VENDOR_IMPORT or module.startswith(f"{_VENDOR_IMPORT}."):
                offenders.append(module)
    return offenders


def _assert_stage_one_source_safe(source: str, *, filename: str) -> None:
    """Fail with AssertionError for every unsafe source-level escape hatch.

    The order rule is intentionally stronger than a future gated-dispatch rule:
    this stage has no order dispatcher at all, so every known order endpoint/TR
    is forbidden.  A future stage must explicitly narrow this guard alongside
    an independently reviewed dry-run/confirm implementation.
    """

    tree = ast.parse(source, filename=filename)
    literals = _literal_strings(tree)
    assert _vendor_sdk_imports(tree) == [], "vendor SDK import is forbidden"
    assert "nhplug" not in literals, "dynamic vendor SDK import literal is forbidden"
    assert not (_FORBIDDEN_OVERRIDE_ENV & set(literals)), (
        "host override environment reference is forbidden"
    )
    if filename != "auth.py":
        assert not any(_PRODUCTION_HOST_RE.search(literal) for literal in literals), (
            "only auth.py may contain the production hostname"
        )
    assert not any(
        forbidden in literal
        for literal in literals
        for forbidden in _FORBIDDEN_ORDER_TEXT
    ), "stage-one source contains an out-of-scope order endpoint or TR"


@pytest.mark.parametrize("path", RUNTIME_SOURCES, ids=lambda path: path.name)
def test_runtime_sources_obey_the_stage_one_static_guard(path: Path) -> None:
    _assert_stage_one_source_safe(path.read_text(encoding="utf-8"), filename=path.name)


@pytest.mark.parametrize(
    ("label", "source", "filename"),
    (
        ("vendor SDK import", "import nhplug\n", "client.py"),
        (
            "split vendor SDK import literal",
            'importlib.import_module("nh" + "plug")\n',
            "client.py",
        ),
        (
            "production host literal",
            'HOST = "https://api.nhplug.com:8443"\n',
            "client.py",
        ),
        (
            "split production host literal",
            'HOST = "https://api." + "nhplug.com:8443"\n',
            "client.py",
        ),
        (
            "host override environment",
            'value = os.getenv("NHPLUG_BASE_URL")\n',
            "client.py",
        ),
        (
            "split host override environment",
            'value = os.getenv("NHPLUG_" + "AUTH_URL")\n',
            "client.py",
        ),
        (
            "ungated order endpoint and TR",
            'PATH = "/krstock/order/v1/cashBuy"\nTR = "SCSOS61803A"\n',
            "client.py",
        ),
        (
            "split ungated order endpoint",
            'PATH = "/krstock/" + "order/v1/cashBuy"\n',
            "client.py",
        ),
    ),
)
def test_static_guard_mutants_fail_with_assertion_error(
    label: str, source: str, filename: str
) -> None:
    """Each required mutant must make the build guard red, never false-green."""

    with pytest.raises(AssertionError, match="forbidden|only auth|order"):
        _assert_stage_one_source_safe(source, filename=filename)


def test_auth_is_the_only_runtime_production_host_owner() -> None:
    owners = [
        path.name
        for path in RUNTIME_SOURCES
        if any(
            _PRODUCTION_HOST_RE.search(literal)
            for literal in _literal_strings(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert owners == ["auth.py"]


def test_data_client_does_not_import_the_oauth_client() -> None:
    tree = ast.parse((RUNTIME_DIR / "client.py").read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "app.services.brokers.nhplug.auth" not in imported_modules


@pytest.mark.parametrize("path", (RUNTIME_DIR / "auth.py", RUNTIME_DIR / "client.py"))
def test_every_httpx_dispatch_pins_follow_redirects_false(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
    ]
    assert calls, f"{path.name} unexpectedly has no HTTP client construction"
    for call in calls:
        assert any(
            keyword.arg == "follow_redirects"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        ), f"{path.name}:{call.lineno} does not pin follow_redirects=False"


def test_no_mutation_method_is_exposed_by_the_data_client() -> None:
    tree = ast.parse((RUNTIME_DIR / "client.py").read_text(encoding="utf-8"))
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    forbidden_fragments = ("order", "buy", "sell", "modify", "cancel", "submit")
    assert not {
        method
        for method in method_names
        if any(fragment in method.lower() for fragment in forbidden_fragments)
    }


def test_dry_run_confirm_contract_is_typed_but_has_no_dispatch_consumer() -> None:
    default = DryRunConfirmContract()
    assert default.dry_run is True
    assert default.confirm is False
    default.assert_dispatch_allowed()
    with pytest.raises(ValueError):
        DryRunConfirmContract(dry_run=False, confirm=False).assert_dispatch_allowed()
    DryRunConfirmContract(dry_run=False, confirm=True).assert_dispatch_allowed()
